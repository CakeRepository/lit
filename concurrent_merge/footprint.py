"""Record what a test job consumed and predict which later commits could reuse its result.

A trace runs the job under strace in a fresh detached checkout. Every path the
job opened, executed, inspected or listed before writing it becomes an input.
Inputs inside the checkout are recorded as Git tree entries of the traced
commit, so a candidate commit can be compared without running anything.
Inputs outside the checkout are hashed so a runner can check its own toolchain.

The result is an observation of one execution. It does not stop the job from
reading other data, and a prediction alone cannot authorize a protected status.
"""

from datetime import datetime, timezone
from fnmatch import fnmatchcase
from hashlib import sha256
import json
import copy
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time

from .engine import Repo, shared_clone

SYSCALLS = (
    "%file,getdents,getdents64,connect,clone,clone3,fork,vfork,fchdir,io_uring_setup"
)
# Runner bookkeeping that changes on every CI run. A job that branches on one
# of these needs --strict-env, which records every variable.
RUN_SPECIFIC_ENV = (
    "GITHUB_RUN_*",
    "GITHUB_JOB",
    "GITHUB_ACTION*",
    "GITHUB_OUTPUT",
    "GITHUB_STATE",
    "GITHUB_ENV",
    "GITHUB_PATH",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_SHA",
    "GITHUB_REF*",
    "GITHUB_HEAD_REF",
    "GITHUB_BASE_REF",
    "GITHUB_EVENT_PATH",
    "GITHUB_TRIGGERING_ACTOR",
    "GITHUB_RETENTION_DAYS",
    "RUNNER_TRACKING_ID",
    "ACTIONS_*",
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    "OLDPWD",
    "PWD",  # the launching shell's directory, not the test's: os.getcwd() is the checkout
    "SHLVL",
    "_",
)
# The kernel's own interfaces and per-run scratch space change between runs.
# They are listed in each manifest so the policy travels with the record.
EPHEMERAL = ("/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp")
# Files each container or machine writes for itself: the network plumbing a
# runner sets up per container, and caches rebuilt from inputs that are
# themselves recorded (fontconfig's cache of the font files a run reads).
# They differ from runner to runner without changing what a test sees, so they
# are neither recorded nor compared, which also clears records that hold them.
MACHINE_LOCAL = (
    "/etc/hosts",
    "/etc/hostname",
    "/etc/resolv.conf",
    "/var/cache/fontconfig",
)
WRITES = {
    "creat",
    "mkdir",
    "mkdirat",
    "rmdir",
    "unlink",
    "unlinkat",
    "truncate",
    "chmod",
    "fchmodat",
    "fchmodat2",
    "chown",
    "lchown",
    "fchownat",
    "utime",
    "utimes",
    "utimensat",
    "futimesat",
    "setxattr",
    "lsetxattr",
    "removexattr",
    "lremovexattr",
    "mknod",
    "mknodat",
}
# For these calls only the final path argument is written. Earlier paths are read.
LAST_WRITTEN = {
    "rename",
    "renameat",
    "renameat2",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
}
MOVED = {"rename", "renameat", "renameat2"}
LINE = re.compile(rb"^(\d+)\s+(?:(\d+\.\d+)\s+)?(.*)$")
EXITED = re.compile(rb"^\+\+\+ (?:exited with (\d+)|killed by SIG\w+)")
CALL = re.compile(rb"^([a-z0-9_]+)\((.*)\)\s+=\s+(.*)$", re.S)
TOKEN = re.compile(
    rb'(?:(AT_FDCWD|\d+)<((?:\\x[0-9a-f]{2})*)>,\s*)?"((?:\\x[0-9a-f]{2})*)"'
)
FD = re.compile(rb"^(\d+)<((?:\\x[0-9a-f]{2})*)>")


def unhex(text):
    return os.fsdecode(bytes.fromhex(text.replace(b"\\x", b"").decode("ascii")))


def strace_version(strace):
    out = subprocess.run([strace, "-V"], capture_output=True, text=True).stdout
    return out.splitlines()[0].strip() if out else "unknown"


def tree_map(repo, commit):
    """Map each path in a commit to (mode, type, object ID). '' is the root."""
    data = repo.run("ls-tree", "-r", "-t", "-z", "--full-tree", commit).stdout
    entries = {"": ("040000", "tree", repo.out("rev-parse", commit + "^{tree}"))}
    for record in data.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        entries[os.fsdecode(path)] = (mode, kind, oid)
    return entries


def children(entries, directory):
    prefix = directory + "/" if directory else ""
    return sorted(
        path[len(prefix) :]
        for path in entries
        if path
        and path.startswith(prefix)
        and "/" not in path[len(prefix) :]
    )


def listing_digest(names):
    return sha256("\0".join(names).encode("utf8", "surrogateescape")).hexdigest()


def stem(name):
    return name.split(".")[0] or name


# Extensions a JS/TS module resolver tries after a bare import name ("./x" -> x.ts, x.tsx, ...).
RESOLVED = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".json", ".node", ".d.ts")


# Tool outputs that no module resolver probes for: a tsc -b in the same checkout can add
# tsconfig.server.tsbuildinfo next to tsconfig.json without changing what "./tsconfig" means.
INERT = (".tsbuildinfo", ".log", ".map", ".lock", ".orig", ".rej", ".swp", ".tmp", ".md")

# Names a resolver looks for in every directory on its way up, found through the listing
# rather than a syscall, so adding or removing one matters wherever it is listed.
RESOLVER_FILES = frozenset((
    "package.json", "tsconfig.json", "jsconfig.json", "node_modules", "__init__.py", "conftest.py",
))


def resolution_key(name):
    """The import name that could resolve to `name`, or None if no resolver would probe it.

    For a JS/TS module that is the name without its one resolved extension, so
    x.postgres.test.ts answers only "./x.postgres.test" and can never shadow
    x.ts for "./x". Inert tool outputs have none. Anything else keeps the
    conservative stem (up to the first dot), which covers Python's
    x.cpython-313-....so shadowing x.py."""
    for ext in RESOLVED:
        if name.endswith(ext) and len(name) > len(ext):
            return name[: -len(ext)]
    if name.endswith(INERT):
        return None
    return stem(name)


class Listings(dict):
    """Directory listing digests of one entries map, indexed once and memoized."""

    def __init__(self, entries):
        super().__init__()
        self.kids = {}
        for path in entries:
            if path:
                parent, _, name = path.rpartition("/")
                self.kids.setdefault(parent, []).append(name)

    def __missing__(self, directory):
        self[directory] = listing_digest(sorted(self.kids.get(directory, ())))
        return self[directory]


class Events:
    """Accumulate the first use of each path from a strace -f -xx -y log."""

    def __init__(self, record=False):
        self.record = record
        self.first = {}  # path -> "read" | "write"
        self.missing = set()  # paths whose first read failed with ENOENT/ENOTDIR
        # Paths first opened with O_CREAT and without O_TRUNC: when such a path was not in the checkout, this
        # open made it (bun writes every file this way), so the run read nothing there that existed before.
        self.created = set()
        self.listed = set()
        self.cwd = {}
        self.network = []
        self.unresolved = []
        self.written = set()
        self.io_uring = False
        # Ordered per-process record, so a trace can be split into units later.
        self.log = []
        self.parent = {}
        self.exits = {}
        self.pid = None
        self.now = None

    def touch(self, path, kind, missing=False, created=False):
        path = os.path.normpath(path)
        if self.record:
            self.log.append(("touch", self.pid, path, kind, missing, created))
        if kind == "write":
            self.written.add(path)
        if path not in self.first:
            self.first[path] = kind
            if kind == "read" and missing:
                self.missing.add(path)
            if kind == "read" and created:
                self.created.add(path)

    def note(self, kind, *rest):
        if self.record:
            self.log.append((kind, self.pid, *rest))

    def resolve(self, pid, raw):
        path = unhex(raw)
        if os.path.isabs(path):
            return path
        cwd = self.cwd.get(pid)
        if cwd is None:
            self.unresolved.append(path)
            self.note("unresolved", path)
            return None
        return os.path.join(cwd, path)

    def feed(self, pid, name, args, result, now=None):
        self.pid, self.now = pid, now
        failed = result.startswith(b"-1")
        missing = failed and (b"ENOENT" in result or b"ENOTDIR" in result)
        if b"AT_FDCWD<" in args:
            cwd = re.search(rb"AT_FDCWD<((?:\\x[0-9a-f]{2})*)>", args)
            self.cwd[pid] = unhex(cwd.group(1))
        if name in ("clone", "clone3", "fork", "vfork") and not failed:
            child = re.match(rb"(\d+)", result)
            if child:
                self.parent[child.group(1)] = pid
                self.note("clone", child.group(1))
                if pid in self.cwd:
                    self.cwd.setdefault(child.group(1), self.cwd[pid])
            return
        if name in ("getdents", "getdents64"):
            fd = FD.match(args)
            if fd:
                path = unhex(fd.group(2))
                self.touch(path, "read")
                self.listed.add(os.path.normpath(path))
                self.note("list", os.path.normpath(path))
            return
        if name == "connect":
            if b"AF_INET" in args:
                self.network.append(args.decode("ascii", "replace")[:300])
                self.note("net", self.network[-1])
            return
        if name == "io_uring_setup" and not failed:
            # Reads submitted through io_uring never appear as syscalls.
            self.io_uring = True
            self.note("uring")
            return
        if name == "fchdir":
            fd = FD.match(args)
            if fd and not failed:
                self.cwd[pid] = unhex(fd.group(2))
            return
        # Path strings in argument order. A string after a descriptor is
        # relative to it; strace -y decodes the descriptor to its path.
        paths = []
        for match in TOKEN.finditer(args):
            if match.group(2) is not None:
                paths.append(os.path.join(unhex(match.group(2)), unhex(match.group(3))))
            else:
                path = self.resolve(pid, match.group(3))
                if path is None:
                    return
                paths.append(path)
            if name not in LAST_WRITTEN:
                break  # later strings are argv, link targets or attribute names
        if not paths:
            return
        if name == "execve" and not failed:
            argv = [unhex(m.group(3)) for m in TOKEN.finditer(args)][1:]
            self.note("exec", argv, now)
        if name == "chdir" and not failed:
            self.cwd[pid] = os.path.normpath(paths[0])
        if name in ("open", "openat", "openat2"):
            write = any(
                flag in args for flag in (b"O_WRONLY", b"O_RDWR", b"O_CREAT", b"O_TRUNC")
            )
            truncate = b"O_TRUNC" in args or b"O_EXCL" in args
            created = b"O_CREAT" in args and not failed
            if write and truncate:
                self.touch(paths[0], "write")
            else:
                self.touch(paths[0], "read", missing, created)
                if write:
                    self.touch(paths[0], "write")
            opened = FD.match(result)
            if opened and not failed:
                real = unhex(opened.group(2))
                self.touch(real, "write" if write and truncate else "read", created=created)
            return
        if name in WRITES:
            for path in paths:
                self.touch(path, "write")
            return
        if name in LAST_WRITTEN:
            if name in MOVED:
                self.touch(paths[0], "read", missing)
                self.touch(paths[0], "write")
            elif name in ("link", "linkat"):
                self.touch(paths[0], "read", missing)
            # symlink's first string is link text, not a path that was read
            self.touch(paths[-1], "write")
            return
        for path in paths:
            self.touch(path, "read", missing)


def parse(log, record=False):
    events, pending = Events(record), {}
    for raw in log.splitlines():
        line = LINE.match(raw)
        if not line:
            continue
        pid, stamp, text = line.groups()
        now = float(stamp) if stamp else None
        exited = EXITED.match(text)
        if exited:
            events.exits[pid] = (int(exited.group(1)) if exited.group(1) else -1, now)
            continue
        if text.startswith(b"---") or text.startswith(b"+++"):
            continue
        if text.endswith(b"<unfinished ...>"):
            pending[pid] = text[: -len(b"<unfinished ...>")]
            continue
        if text.startswith(b"<... "):
            head = pending.pop(pid, None)
            if head is None:
                continue
            text = head + text.split(b"resumed>", 1)[1]
        call = CALL.match(text)
        if call:
            events.feed(
                pid, call.group(1).decode(), call.group(2), call.group(3), now
            )
    return events


def split_units(events, pattern):
    """Divide one trace into units, each started by an exec whose argv matches pattern.

    A unit is that process and every process it starts. Group 1 of the pattern,
    or the whole match, names the unit. Reads outside every unit are dropped.
    """
    regex = re.compile(pattern)
    unit, roots, units = {}, {}, {}

    def owner(pid):
        seen = set()
        while pid is not None and pid not in seen:
            if pid in unit:
                return unit[pid]
            seen.add(pid)
            pid = events.parent.get(pid)
        return None

    for entry in events.log:
        kind, pid = entry[0], entry[1]
        if kind == "exec":
            match = regex.search(" ".join(entry[2]))
            if match and owner(pid) is None:
                name = match.group(1) if match.groups() else match.group(0)
                unit[pid] = name
                roots[name] = (pid, entry[3])
                units.setdefault(name, Events())
            continue
        if kind == "clone":
            if entry[2] not in unit and owner(pid) is not None:
                unit[entry[2]] = owner(pid)
            continue
        name = owner(pid)
        if name is None:
            continue
        target = units[name]
        if kind == "touch":
            target.touch(*entry[2:])
        elif kind == "list":
            target.listed.add(entry[2])
        elif kind == "net":
            target.network.append(entry[2])
        elif kind == "unresolved":
            target.unresolved.append(entry[2])
        elif kind == "uring":
            target.io_uring = True
    for name, target in units.items():
        pid, started = roots[name]
        code, ended = events.exits.get(pid, (None, None))
        target.exit_code = code
        target.seconds = (
            round(ended - started, 3) if ended is not None and started is not None else None
        )
    return units


def under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


# Persist digests only after the filesystem clock has advanced beyond a file's
# last change. A same-size rewrite within one timestamp tick can share every stat field.
DIGESTS = {}


def file_digest(path):
    try:
        if os.path.isdir(path):
            return "dir"
        if not os.path.exists(path):
            return "absent"
        if not os.path.isfile(path):
            return "other"
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            # Include the device to distinguish mount points and retire entries
            # written by older versions that cached freshly changed files.
            key = f"{path}\0{st.st_dev}\0{st.st_ino}\0{st.st_size}\0{st.st_mtime_ns}\0{st.st_ctime_ns}"
            stable = time.time_ns() - max(st.st_mtime_ns, st.st_ctime_ns) > 2_000_000_000
            if stable and key in DIGESTS:
                return DIGESTS[key]
            h = sha256()
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
            digest = h.hexdigest()
            end = os.fstat(f.fileno())
            if stable and (end.st_size, end.st_ctime_ns, end.st_mtime_ns) == (
                st.st_size, st.st_ctime_ns, st.st_mtime_ns
            ):
                DIGESTS[key] = digest
            return digest
    except OSError:
        return "unreadable"


def environment(env, ignore):
    return {
        name: sha256(value.encode("utf8", "surrogateescape")).hexdigest()
        for name, value in sorted(env.items())
        if not any(fnmatchcase(name, pattern) for pattern in ignore)
    }


def workspace(repo, commit, workdir=None):
    """Paths visible to the job: the commit's tree plus files already in an existing checkout."""
    entries = tree_map(repo, commit)
    if workdir is None:
        return entries
    work = Repo(workdir)
    if work.out("rev-parse", "HEAD") != commit:
        raise ValueError("The checkout is not at the traced commit")
    if work.run("status", "--porcelain", "-z", "--untracked-files=no").stdout:
        raise ValueError("Tracked files differ from the commit; use a clean checkout")
    others = work.run("ls-files", "--others", "-z").stdout.split(b"\0")
    for raw in others:
        if not raw:
            continue
        path = os.fsdecode(raw)
        entries.setdefault(path, ("untracked", "blob", None))
        parent = os.path.dirname(path)
        while parent and parent not in entries:
            entries[parent] = ("untracked", "tree", None)
            parent = os.path.dirname(parent)
    return entries


# Parts of the Git directory that do not change with the commit checked out. Tools
# look at them to find the repository root or to honor its ignore rules: what they
# hold is the input, not the revision.
GIT_STATIC = frozenset({".git", ".git/info", ".git/info/exclude"})


def summarize(events, checkout, entries, ignore_env, env, listed=None, produced=(), scratch=()):
    """produced holds paths other units of the same recording wrote before this one.

    scratch holds absolute directories of runtime data (see trace_units): what a
    unit reads or writes there is never an input.
    """
    listed = Listings(entries) if listed is None else listed
    shared = set()
    checkout = os.path.realpath(checkout)
    if scratch:
        events = _without(events, scratch, checkout, entries)
    tracked, directories, absent, listings = {}, set(), set(), {}
    git_metadata, untracked, unknown, changed = set(), {}, set(), set()
    git_static = {}
    external = set()
    for path, kind in events.first.items():
        if kind != "read":
            continue
        if under(path, checkout):
            rel = os.path.relpath(path, checkout)
            rel = "" if rel == "." else rel
            if rel in GIT_STATIC:
                git_static[rel] = "absent" if path in events.missing else file_digest(path)
                continue
            if rel == ".git" or rel.startswith(".git/"):
                git_metadata.add(rel)
                continue
            entry = entries.get(rel)
            if entry and entry[1] == "tree":
                directories.add(rel)
                if path in events.listed:
                    listings[rel] = listed[rel]
            elif entry and entry[0] == "untracked":
                if path in events.written:
                    changed.add(rel)
                untracked[rel] = file_digest(path)
            elif entry:
                tracked[rel] = [entry[0], entry[2]]
            elif path in events.missing:
                absent.add(rel)
            elif path in produced:
                shared.add(rel)
            elif path in events.created:
                absent.add(rel)  # this run's own O_CREAT made it, so it must not exist beforehand
            else:
                unknown.add(rel)
        elif path in produced:
            shared.add(path)
        elif not any(under(path, prefix) for prefix in EPHEMERAL + MACHINE_LOCAL):
            external.add(path)
    # Per listed directory: the name stems this run read or probed in it, and
    # whether it opened most of the directory, which looks like enumeration.
    lookups, keys, enumerated = {}, {}, []
    touched = {}
    for path in events.first:
        if under(path, checkout):
            parent, _, name = os.path.relpath(path, checkout).rpartition("/")
            touched.setdefault(parent, set()).add(name)
    for rel in listings:
        names = touched.get(rel, set())
        lookups[rel] = sorted({stem(n) for n in names})
        keys[rel] = sorted(names | {k for k in map(resolution_key, names) if k is not None})
        size = len(listed.kids.get(rel, ()))
        if len(names) > 5 and len(names) * 2 >= size:
            enumerated.append(rel)
    incomplete = []
    if unknown:
        incomplete.append("read checkout paths that existed before neither the run nor the commit")
    if changed:
        incomplete.append("modified untracked files after reading them")
    if events.unresolved:
        incomplete.append("could not resolve relative paths")
    if events.io_uring:
        incomplete.append("used io_uring, whose file reads strace cannot see")
    return dict(
        tracked=dict(sorted(tracked.items())),
        directories=sorted(directories),
        listings=dict(sorted(listings.items())),
        listing_lookups=lookups,
        listing_keys=keys,
        enumerated=sorted(enumerated),
        absent=sorted(absent),
        untracked=dict(sorted(untracked.items())),
        unknown=sorted(unknown)[:50],
        # Untracked files the run read and then wrote, which make it incomplete.
        rewritten=sorted(changed)[:50],
        # Written by an earlier unit, for example a bytecode or transpiler cache.
        shared_state=sorted(shared)[:50],
        git_metadata=sorted(git_metadata)[:50],
        reads_git_metadata=bool(git_metadata),
        git_static=dict(sorted(git_static.items())),
        external={
            path: file_digest(path)
            for path in sorted(external)
            if path not in events.written
        },
        environment=environment(env, ignore_env),
        ignored_environment=list(ignore_env),
        ephemeral_prefixes=list(EPHEMERAL),
        network=events.network[:50],
        unresolved=events.unresolved[:50],
        incomplete=incomplete,
    )


def checkout(repo, commit, directory):
    proc = subprocess.run(
        shared_clone(repo.path, directory),
        capture_output=True,
    )
    if proc.returncode:
        raise RuntimeError("git clone: " + proc.stderr.decode(errors="replace").strip())
    work = Repo(directory)
    work.run("remote", "remove", "origin")
    work.run("checkout", "-q", "--detach", commit)


def run_traced(strace, command, cwd, env, log, record=False, append=False):
    with tempfile.TemporaryDirectory(prefix="footprint-log-") as scratch:
        events_file = Path(scratch) / "strace.log"
        clock = time.monotonic()
        with open(log, "ab" if append else "wb") if log else open(os.devnull, "rb") as out:
            rc = subprocess.run(
                [
                    strace,
                    "-f",
                    # Stop the job only at the traced calls, not at every syscall.
                    "--seccomp-bpf",
                    "-q",
                    "-ttt",
                    "-xx",
                    "-y",
                    "-s",
                    "65535",
                    "-e",
                    "signal=none",
                    "-e",
                    "trace=" + SYSCALLS,
                    "-o",
                    str(events_file),
                    "--",
                    *command,
                ],
                cwd=cwd,
                env=env,
                stdout=out if log else None,
                stderr=subprocess.STDOUT if log else None,
            ).returncode
        seconds = round(time.monotonic() - clock, 3)
        return rc, seconds, parse(events_file.read_bytes(), record)


def trace(
    repo, commit, command, *, log=None, ignore_env=(), strace=None, workdir=None
):
    """Trace the command in a fresh checkout, or in workdir when it is already checked out."""
    strace = strace or shutil.which("strace")
    if not strace:
        raise RuntimeError("Recording a job footprint requires strace on PATH")
    commit = repo.out("rev-parse", "--verify", commit + "^{commit}")
    env = dict(os.environ)
    started = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="footprint-") as scratch_root:
        if workdir is None:
            work = Path(scratch_root) / "checkout"
            checkout(repo, commit, str(work))
        else:
            work = Path(workdir)
        entries = workspace(repo, commit, None if workdir is None else work)
        rc, seconds, events = run_traced(strace, command, work, env, log)
        footprint = summarize(events, work, entries, ignore_env, env)
    return dict(
        kind="lit-job-footprint",
        version=1,
        command=list(command),
        commit=commit,
        tree=entries[""][2],
        in_place=workdir is not None,
        exit_code=rc,
        started=started.isoformat(),
        seconds=seconds,
        runner=dict(
            platform=platform.platform(),
            machine=platform.machine(),
            tracer=strace_version(strace),
        ),
        **footprint,
    )


def predict(
    repo,
    manifest,
    candidate,
    *,
    env=None,
    host=False,
    allow_network=False,
    workdir=None,
):
    """Compare one candidate commit with a recorded footprint. Nothing is executed.

    With workdir, the candidate is that existing checkout, including its untracked files.
    """
    commit = repo.out("rev-parse", "--verify", candidate + "^{commit}")
    entries = workspace(repo, commit, workdir)
    reasons, checked = compare(
        manifest, commit, entries, env=env, host=host,
        allow_network=allow_network, workdir=workdir,
    )
    return dict(
        candidate=commit,
        tree=entries[""][2],
        footprint_commit=manifest["commit"],
        verdict="reusable" if not reasons else "run",
        expected_exit_code=manifest["exit_code"] if not reasons else None,
        checked=checked,
        reasons=reasons[:200],
        reason_count=len(reasons),
    )


def listing_matters(manifest, path, listed):
    """In names mode, whether a changed listing can affect this run.

    A name added or removed matters when an import that the run made in that
    directory could resolve to it (so a new a.tsx can shadow a.ts for an import
    of ./a, while a new a.postgres.test.ts cannot), or when the run opened most
    of the directory, which looks like it enumerated it. Records from before
    listing_keys match by stem. Without the recorded names, every change matters.
    """
    before = manifest.get("listing_children", {}).get(path)
    keys = manifest.get("listing_keys", {}).get(path)
    lookups = manifest.get("listing_lookups", {}).get(path)
    if before is None or (keys is None and lookups is None) or path in manifest.get("enumerated", ()):
        return True
    changed = set(before) ^ set(listed.kids.get(path, ()))
    if changed & RESOLVER_FILES:
        return True
    if keys is None:
        return bool({stem(n) for n in changed if resolution_key(n) is not None} & set(lookups))
    keys = set(keys)
    return any(name in keys or resolution_key(name) in keys for name in changed)


def environment_changes(manifest, env, ignore=()):
    """Variables whose value differs from the record. A variable either side
    ignores does not count: a record written before a variable was declared
    run-specific still holds it, and must not fail every check after."""
    current = environment(env, [*manifest["ignored_environment"], *ignore])
    recorded = {
        name: digest for name, digest in manifest["environment"].items()
        if not any(fnmatchcase(name, pattern) for pattern in ignore)
    }
    return [
        dict(kind="environment", name=name)
        for name in sorted(set(current) | set(recorded))
        if current.get(name) != recorded.get(name)
    ]


def compare(
    manifest, commit, entries, *, env=None, host=False, allow_network=False,
    workdir=None, listed=None, listings="exact",
):
    """Every recorded input of manifest that differs in the candidate's entries."""
    reasons = []
    for reason in manifest["incomplete"]:
        reasons.append(dict(kind="incomplete", detail=reason))
    if manifest["network"] and not allow_network:
        reasons.append(dict(kind="network", detail="the job opened network connections"))
    if manifest["reads_git_metadata"] and commit != manifest["commit"]:
        reasons.append(
            dict(kind="revision", detail="the job read Git metadata of its own checkout",
                 paths=list(manifest.get("git_metadata") or [])[:5])
        )
    for path, digest in (manifest.get("git_static") or {}).items():
        if workdir is not None:
            if file_digest(os.path.join(workdir, path)) != digest:
                reasons.append(dict(kind="changed", path=path))
        elif commit != manifest["commit"]:
            reasons.append(dict(kind="revision", detail="the job looked at the checkout's Git directory",
                                paths=[path]))
            break
    for path, (mode, oid) in manifest["tracked"].items():
        entry = entries.get(path)
        if not entry or entry[0] != mode or entry[2] != oid:
            reasons.append(dict(kind="changed", path=path))
    for path in manifest["directories"]:
        entry = entries.get(path)
        if not entry or entry[1] != "tree":
            reasons.append(dict(kind="changed", path=path))
    for path, digest in manifest["listings"].items():
        if listed is None:
            listed = Listings(entries)
        if path in entries and listed[path] != digest:
            if listings == "names" and not listing_matters(manifest, path, listed):
                continue
            reasons.append(dict(kind="listing", path=path))
    for path in manifest["absent"]:
        if path in entries:
            reasons.append(dict(kind="appeared", path=path))
    for path, digest in manifest["untracked"].items():
        if workdir is None:
            reasons.append(dict(kind="untracked", path=path))
        elif file_digest(os.path.join(workdir, path)) != digest:
            reasons.append(dict(kind="changed", path=path))
    checked = ["checkout"]
    if env is not None:
        checked.append("environment")
        reasons += environment_changes(manifest, env)
    if host:
        checked.append("host")
        for path, digest in manifest["external"].items():
            if any(under(path, prefix) for prefix in MACHINE_LOCAL):
                continue
            if file_digest(path) != digest:
                reasons.append(dict(kind="host", path=path))
    return reasons, checked


SHARED = ("tracked", "external")


def scratch_dirs(entries, scratch):
    """Validate repo-relative scratch directories: plain paths inside the checkout."""
    out = []
    for rel in scratch:
        rel = rel.strip("/")
        parts = rel.split("/")
        if not rel or any(p in ("", ".", "..") for p in parts) or parts[0] == ".git":
            raise ValueError(f"A scratch directory is a plain path inside the repository, got {rel!r}")
        out.append(rel)
    return out


def _without(events, scratch, checkout, entries):
    """A copy of events with every path under the scratch directories dropped,
    except files the commit tracks there (a .gitkeep, a checked-in fixture),
    which stay inputs."""

    def keep(path):
        if not any(under(path, root) for root in scratch):
            return True
        entry = entries.get(os.path.relpath(path, checkout))
        return bool(entry) and entry[0] != "untracked" and entry[1] == "blob"

    clone = copy.copy(events)
    for name, value in vars(events).items():
        if isinstance(value, dict):
            setattr(clone, name, {k: v for k, v in value.items() if not isinstance(k, str) or keep(k)})
        elif isinstance(value, (set, frozenset)):
            setattr(clone, name, {k for k in value if not isinstance(k, str) or keep(k)})
    return clone


def trace_units(
    repo, commit, command=None, pattern=None, *, each=None, cwd="", log=None,
    ignore_env=(), strace=None, workdir=None, scratch=(),
):
    """Keep a footprint for each unit of a test job.

    Either trace one command that starts many units, where a unit starts at a process
    whose argv matches pattern (group 1 names it) and includes its descendants; or
    pass each, a mapping of unit name to argv, and every unit runs under its own
    trace in cwd, one after another, so no single trace grows large.

    scratch: repo-relative, untracked directories where the units keep runtime
    data (a server's data/ folder, say). Nothing read or written under them is
    part of any footprint. That is sound when, as with a fresh CI checkout, the
    directories start empty and no test's result depends on what other tests
    left there, the same isolation the store already assumes of a database.
    """
    strace = strace or shutil.which("strace")
    if not strace:
        raise RuntimeError("Recording unit footprints requires strace on PATH")
    if (each is None) == (command is None or pattern is None):
        raise ValueError("Give either a command and a pattern, or each")
    commit = repo.out("rev-parse", "--verify", commit + "^{commit}")
    env = dict(os.environ)
    started = datetime.now(timezone.utc)
    shared = {key: {} for key in SHARED}
    units = {}
    with tempfile.TemporaryDirectory(prefix="footprint-") as scratch_root:
        if workdir is None:
            work = Path(scratch_root) / "checkout"
            checkout(repo, commit, str(work))
        else:
            work = Path(workdir)
        entries = workspace(repo, commit, None if workdir is None else work)
        listed = Listings(entries)
        scratch = scratch_dirs(entries, scratch)
        absolute = [os.path.join(os.path.realpath(work), rel) for rel in scratch]

        produced = set()

        def keep(name, unit_events, exit_code, seconds):
            summary = summarize(
                unit_events, work, entries, ignore_env, env, listed, produced, absolute
            )
            produced.update(
                p for p in unit_events.written if not any(under(p, r) for r in absolute)
            )
            for key in ("environment", "ignored_environment", "ephemeral_prefixes"):
                summary.pop(key)
            for key in SHARED:
                shared[key].update(summary[key])
                summary[key] = sorted(summary[key])
            units[name] = dict(exit_code=exit_code, seconds=seconds, **summary)

        clock = time.monotonic()
        if each is None:
            rc, _, events = run_traced(strace, command, work, env, log, record=True)
            for name, unit_events in split_units(events, pattern).items():
                keep(name, unit_events, unit_events.exit_code, unit_events.seconds)
        else:
            rc = 0
            for name, argv in each.items():
                code, seconds, events = run_traced(
                    strace, argv, work / cwd, env, log, append=True
                )
                keep(name, events, code, seconds)
                rc = rc or code
        seconds = round(time.monotonic() - clock, 3)
    return dict(
        kind="lit-unit-footprints",
        version=1,
        command=list(command) if each is None else None,
        pattern=pattern,
        each={name: list(argv) for name, argv in each.items()} if each else None,
        cwd=cwd,
        commit=commit,
        tree=entries[""][2],
        in_place=workdir is not None,
        exit_code=rc,
        started=started.isoformat(),
        seconds=seconds,
        runner=dict(
            platform=platform.platform(),
            machine=platform.machine(),
            tracer=strace_version(strace),
        ),
        environment=environment(env, ignore_env),
        ignored_environment=list(ignore_env),
        ephemeral_prefixes=list(EPHEMERAL),
        scratch=list(scratch),
        tracked=dict(sorted(shared["tracked"].items())),
        external=dict(sorted(shared["external"].items())),
        listing_children={
            directory: sorted(listed.kids.get(directory, ()))
            for directory in sorted({d for u in units.values() for d in u["listings"]})
        },
        units=dict(sorted(units.items())),
    )


def unit_manifest(bundle, name):
    """One unit of a lit-unit-footprints bundle, shaped like a job footprint."""
    unit = bundle["units"][name]
    view = dict(unit, commit=bundle["commit"], command=bundle["command"] or bundle["each"][name])
    for key in SHARED:
        view[key] = {path: bundle[key][path] for path in unit[key]}
    view["environment"] = bundle["environment"]
    view["listing_children"] = bundle.get("listing_children", {})
    view["ignored_environment"] = bundle["ignored_environment"]
    return view


def select(
    repo,
    bundle,
    candidate,
    *,
    env=None,
    host=False,
    allow_network=False,
    workdir=None,
    discover=None,
    strip="",
    listings="exact",
):
    """Split the candidate's units into those to run and those a passing footprint covers.

    discover is a list of fnmatch patterns for unit files in the candidate tree; a match
    with no footprint is new and runs. Units whose file is gone are dropped. Without
    discover, the units are exactly those in the bundle.
    """
    commit = repo.out("rev-parse", "--verify", candidate + "^{commit}")
    entries = workspace(repo, commit, workdir)
    names = set(bundle["units"])
    new = []
    if discover:
        found = {
            path[len(strip):]
            for path, entry in entries.items()
            if entry[1] == "blob"
            and entry[0] != "untracked"  # dependencies are not units
            and path.startswith(strip)
            and any(fnmatchcase(path, pattern) for pattern in discover)
        }
        new = sorted(found - names)
        names &= found
    environment_reasons = [] if env is None else environment_changes(bundle, env)
    listed = Listings(entries)
    run, reusable = {}, []
    for name in sorted(names):
        unit = bundle["units"][name]
        if unit["exit_code"] != 0:
            run[name] = [dict(kind="failed", detail=f"exited {unit['exit_code']}")]
            continue
        reasons, _ = compare(
            unit_manifest(bundle, name), commit, entries,
            host=host, allow_network=allow_network, workdir=workdir, listed=listed,
            listings=listings,
        )
        reasons += environment_reasons
        if reasons:
            run[name] = reasons
        else:
            reusable.append(name)
    for name in new:
        run[name] = [dict(kind="new", detail="no footprint for this unit")]
    seconds = {name: bundle["units"][name]["seconds"] or 0 for name in bundle["units"]}
    return dict(
        candidate=commit,
        footprint_commit=bundle["commit"],
        run=sorted(run),
        reusable=reusable,
        reasons={name: reasons[:20] for name, reasons in sorted(run.items())},
        seconds_recorded_run=round(sum(seconds.get(n, 0) for n in run), 1),
        seconds_recorded_reusable=round(sum(seconds[n] for n in reusable), 1),
    )


def load_units(path):
    bundle = json.loads(Path(path).read_text(encoding="utf8"))
    if bundle.get("kind") != "lit-unit-footprints" or bundle.get("version") != 1:
        raise ValueError("Not a lit unit footprint bundle")
    return bundle


def verify(repo, manifest, candidate, **kw):
    """Run the job on the candidate as well and score the prediction against it."""
    prediction = predict(repo, manifest, candidate, **kw)
    actual = trace(
        repo,
        prediction["candidate"],
        manifest["command"],
        ignore_env=manifest["ignored_environment"],
        log=os.devnull,
    )
    sound = (
        prediction["verdict"] != "reusable"
        or prediction["expected_exit_code"] == actual["exit_code"]
    )
    return dict(prediction, actual_exit_code=actual["exit_code"], sound=sound), actual


def load(path):
    manifest = json.loads(Path(path).read_text(encoding="utf8"))
    if manifest.get("kind") != "lit-job-footprint" or manifest.get("version") != 1:
        raise ValueError("Not a lit job footprint")
    return manifest
