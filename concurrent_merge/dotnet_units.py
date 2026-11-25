"""Run a .NET test project's classes against a store of each class's last passing program map.

`dotnet test` reads every assembly the tests load, so a file-level trace of it says only "everything". The program
map (dotnet/Program.cs) looks inside the assemblies instead: for each test class it walks the IL of every method
the class can reach across the repository's own projects and hashes it. A class whose hash, external packages,
runtime and data files all match its last passing record runs the same code on the same inputs, so it is reused.

The map is conservative (see Program.cs): virtual calls count every override of a created type, types named as
data count whole, string lookups of types make the class depend on the whole program. What it cannot see is data:
files the tests read at run time. So main's runs (refresh, sweep) run each class it records alone, under strace,
and keep exactly what that process opened, listed or looked for in the repository and the output folder: a
class reruns when one of its own files changes, a directory it listed gains or loses an entry, or a path it found
missing appears. Untracked files outside bin/ and obj/ are not inputs.

Modes, as cached-units: enforce runs the classes whose inputs changed, classes with no record, and an audit sample,
in one `dotnet test --filter` process, and writes no record (merge groups and pull requests). refresh runs the
same set on main, one class per process, and records each class that passed. sweep does that for every class.
plan runs nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import re
import shlex
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .engine import write_json

MODES = ("enforce", "refresh", "sweep", "plan")
TOOL_DIR = Path(__file__).resolve().parent / "dotnet"
RECORD_VERSION = 2
TRACE_CALLS = "open,openat,openat2,creat,stat,lstat,newfstatat,statx,access,faccessat,faccessat2,readlink,readlinkat"
TRACE_LINE = re.compile(
    r'^\d+\s+(open|openat|openat2|creat|stat|lstat|newfstatat|statx|access|faccessat|faccessat2|readlink|readlinkat)'
    r'\((?:(?:AT_FDCWD|-?\d+), )?"((?:[^"\\]|\\.)*)".*\)\s+=\s+(-?\d+.*)$')


def file_sha(path):
    h = sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def text_sha(text):
    return sha256(text.encode("utf8")).hexdigest()


def tool(store, log):
    """Build the program map tool once per version of its source, inside the store."""
    source = sorted(p for p in TOOL_DIR.iterdir() if p.suffix in (".cs", ".csproj"))
    version = text_sha("".join(p.name + file_sha(p) for p in source))[:16]
    out = Path(store) / "tools" / f"litilmap-{version}"
    dll = out / "LitIlMap.dll"
    if not dll.exists():
        with tempfile.TemporaryDirectory(prefix="litilmap-") as scratch:
            for p in source:
                (Path(scratch) / p.name).write_bytes(p.read_bytes())
            built = subprocess.run(
                ["dotnet", "build", scratch, "-c", "Release", "-o", str(out), "-v", "q", "-nologo"],
                capture_output=True, text=True,
            )
            if log:
                log.write(built.stdout + built.stderr)
            if built.returncode != 0 or not dll.exists():
                raise RuntimeError("Could not build the .NET program map tool:\n" + built.stdout[-4000:])
    return dll


def own_assemblies(output_dir, tests_dll):
    """The repository's own projects in a test output folder, from the test project's deps.json."""
    deps = Path(output_dir) / (Path(tests_dll).stem + ".deps.json")
    data = json.loads(deps.read_text(encoding="utf8"))
    names = [k.split("/")[0] for k, v in data.get("libraries", {}).items() if v.get("type") == "project"]
    found = [str(Path(output_dir) / f"{n}.dll") for n in names if (Path(output_dir) / f"{n}.dll").exists()]
    packages = sorted(k for k, v in data.get("libraries", {}).items() if v.get("type") != "project")
    return found, packages


def output_inputs(output_dir, own, tests_dll):
    """Every file of the output folder that is not an own assembly: package assemblies, native libraries and the
    runtime config (the code the map does not walk), and content files (data the tests may read)."""
    skip = {Path(p).name for p in own} | {Path(p).stem + ".pdb" for p in own} | {Path(tests_dll).stem + ".deps.json"}
    external, content = {}, {}
    base = Path(output_dir)
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name in skip:
            continue
        rel = p.relative_to(base).as_posix()
        if p.suffix in (".pdb", ".xml"):
            continue  # symbols and API docs of packages
        if p.suffix in (".dll", ".so", ".dylib", ".exe") or rel.startswith("runtimes/") or p.name.endswith(".json") and ".runtimeconfig." in p.name:
            external[rel] = file_sha(p)
        else:
            content[rel] = file_sha(p)
    return external, content


def run_map(dll, tests_dll, own, names, out_path):
    argv = ["dotnet", str(dll), "--tests", str(tests_dll), "--out", str(out_path)]
    for o in own:
        argv += ["--own", o]
    for n in sorted(set(names)):
        argv += ["--content", n]
    done = subprocess.run(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError("The .NET program map failed:\n" + done.stderr[-4000:])
    return json.loads(Path(out_path).read_text(encoding="utf8"))


def runtime_identity():
    try:
        sdk = subprocess.run(["dotnet", "--version"], capture_output=True, text=True).stdout.strip()
        runtimes = subprocess.run(["dotnet", "--list-runtimes"], capture_output=True, text=True).stdout.strip()
    except OSError:
        sdk, runtimes = "?", "?"
    return text_sha(f"{sdk}\n{runtimes}\n{os.uname().machine}")


def class_filter(names):
    # A class's tests are named "<class>.<method>"; the trailing dot keeps Foo from selecting FooBar.
    return "|".join(f"FullyQualifiedName~{n}." for n in sorted(names))


def traced_inputs(trace_path, workdir, output_dir, tracked, content):
    """What one test process read, as {key: digest}, from an strace log of file syscalls.

    repo:<path>  a tracked file it opened, by content hash
    dir:<path>   a tracked directory it opened (to list it), by the hash of its tracked entries
    out:<path>   a content file of the test output folder it opened, by content hash
    stat:<path>  a repository path it only looked at (exists, stat), by what is there
    absent:<path> a repository path it tried to open that did not exist

    Paths in bin/ and obj/ are build output, which the program map and the output folder hashes already cover.
    A relative path is resolved against the output folder (the test host's working folder) when something is
    there, else the repository root."""
    found = {}
    root = Path(workdir).resolve()
    out = Path(output_dir).resolve()
    dirs = {""}
    for t in tracked:
        parts = t.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    try:
        lines = Path(trace_path).read_text(encoding="utf8", errors="replace").splitlines()
    except OSError:
        return found
    for line in lines:
        m = TRACE_LINE.match(line)
        if not m:
            continue
        call, raw, result = m.group(1), m.group(2), m.group(3)
        try:
            raw = raw.encode("latin1").decode("unicode_escape").encode("latin1").decode("utf8")
        except (UnicodeError, ValueError):
            pass
        path = Path(raw)
        if not path.is_absolute():
            path = out / raw if (out / raw).exists() else root / raw
        path = Path(os.path.normpath(path))
        failed = result.startswith("-1")
        try:
            rel_out = path.relative_to(out).as_posix()
        except ValueError:
            rel_out = None
        if rel_out is not None:
            if rel_out in content and not failed:
                found["out:" + rel_out] = content[rel_out]
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        rel = "" if rel == "." else rel
        if "/bin/" in f"/{rel}/" or "/obj/" in f"/{rel}/" or rel == ".git" or rel.startswith(".git/"):
            continue
        opened = call in ("open", "openat", "openat2", "creat")
        if failed:
            if "ENOENT" in result or "ENOTDIR" in result:
                found.setdefault("absent:" + rel, "missing")
        elif opened and rel in tracked:
            found["repo:" + rel] = "file"
        elif opened and rel in dirs:
            found["dir:" + rel] = "dir"
        elif rel in tracked or rel in dirs:
            found.setdefault("stat:" + rel, "seen")
    for key in [k for k in found if k.startswith("stat:")]:
        rel = key[5:]
        if "repo:" + rel in found or "dir:" + rel in found:
            del found[key]  # what it opened says more than that it looked
    return {k: current_digest(k, root, tracked, content) if v in ("file", "dir", "seen") else v
            for k, v in found.items()}


def current_digest(key, workdir, tracked, content):
    """What a recorded input is now, in the same terms the record wrote it."""
    kind, _, rel = key.partition(":")
    if kind == "out":
        return content.get(rel, "missing")
    p = Path(workdir) / rel
    if kind == "repo":
        return file_sha(p) if p.is_file() else "missing"
    if kind == "dir":
        if not p.is_dir():
            return "missing"
        prefix = f"{rel}/" if rel else ""
        entries = sorted({t[len(prefix):].split("/")[0] for t in tracked if t.startswith(prefix)})
        return text_sha("\n".join(entries))
    if kind == "stat":
        return "dir" if p.is_dir() else "file" if p.exists() else "missing"
    if kind == "absent":
        return "present" if p.exists() else "missing"
    return "unknown"


def tracked_files(workdir):
    out = subprocess.run(["git", "-C", str(workdir), "ls-files", "-z"], capture_output=True, text=True, check=True).stdout
    return {p for p in out.split("\0") if p}


def run_tests(template, names, everything, cwd, log, trace=None):
    argv = []
    for part in template:
        if part == "{filter}":
            if not everything:
                argv += ["--filter", class_filter(names)]
        else:
            argv.append(part)
    if trace:
        argv = ["strace", "-f", "-qq", "--seccomp-bpf", "-e", "trace=" + TRACE_CALLS, "-o", str(trace)] + argv
    started = time.monotonic()
    if log:
        log.write("$ " + shlex.join(argv) + "\n")
        log.flush()
    done = subprocess.run(argv, cwd=cwd, stdout=log or None, stderr=subprocess.STDOUT if log else None)
    return done.returncode, round(time.monotonic() - started, 1)


def record_classes(template, names, workdir, output_dir, tracked, content, log, jobs, done=None, slowest=()):
    """Run each class alone under strace, so what each process read is exactly that class's data inputs.
    done(name, exit code, inputs, seconds) is called as each class finishes; slowest (names) start first.
    Returns the seconds the whole recording took."""
    started = time.monotonic()
    first = {n: i for i, n in enumerate(slowest)}
    order = sorted(names, key=lambda n: (first.get(n, len(first)), n))

    def one(name):
        with tempfile.TemporaryDirectory(prefix="lit-dotnet-class-") as scratch:
            own_log = Path(scratch) / "run.log"
            with open(own_log, "w", encoding="utf8") as handle:
                code, seconds = run_tests(template, [name], False, workdir, handle, Path(scratch) / "strace.log")
            inputs = traced_inputs(Path(scratch) / "strace.log", workdir, output_dir, tracked, content)
            text = own_log.read_text(encoding="utf8", errors="replace")
        return name, code, inputs, seconds, text

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for future in as_completed([pool.submit(one, n) for n in order]):
            name, code, inputs, seconds, text = future.result()
            if log:
                log.write(f"--- {name}: exit {code} in {seconds}s, {len(inputs)} data inputs\n")
                if code != 0:
                    log.write(text[-20000:])
                log.flush()
            if done:
                done(name, code, inputs, seconds)
    return round(time.monotonic() - started, 1)


def run_dotnet_units(store, job, workdir, tests_dll, template, *, mode="enforce", audit_rate=0.0, log=None,
                     receipt=None, listing=None, rng=None, jobs=None):
    if mode not in MODES:
        raise ValueError("mode is enforce, refresh, sweep or plan")
    if "{filter}" not in template:
        raise ValueError("The test command needs {filter} where lit puts --filter and the classes to run")
    workdir = Path(workdir).resolve()
    tests_dll = Path(tests_dll).resolve()
    output_dir = tests_dll.parent
    jobs = jobs or max(1, (os.cpu_count() or 2) // 2)
    commit = subprocess.run(["git", "-C", str(workdir), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    rng = rng or random.Random(commit).random
    started = datetime.now(timezone.utc)
    directory = Path(store) / job
    record_path = directory / "dotnet-record.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf8"))
        if record.get("version") != RECORD_VERSION:
            record = {}
    except (OSError, ValueError):
        record = {}

    dll = tool(store, log)
    own, packages = own_assemblies(output_dir, tests_dll)
    external, content = output_inputs(output_dir, own, tests_dll)
    with tempfile.TemporaryDirectory(prefix="lit-dotnet-") as scratch:
        mapped = run_map(dll, tests_dll, own, [], Path(scratch) / "map.json")
    units = mapped["units"]
    tracked = tracked_files(workdir)
    globals_key = text_sha(json.dumps(dict(packages=packages, external=external, runtime=runtime_identity()),
                                      sort_keys=True))

    old = record.get("classes", {}) if record.get("globals") == globals_key else {}
    old_types = record.get("types", {})
    now_types = dict(zip(mapped["type_names"], mapped["type_hashes"]))
    changed_types = {t for t, h in now_types.items() if old_types.get(t) != h}
    now_inputs = {}

    def moved_inputs(files):
        moved = []
        for key, digest in files.items():
            if key not in now_inputs:
                now_inputs[key] = current_digest(key, workdir, tracked, content)
            if now_inputs[key] != digest:
                moved.append(key)
        return moved

    decisions = {}
    for name, unit in units.items():
        prior = old.get(name)
        if prior is None:
            reason = dict(kind="globals" if record.get("classes", {}).get(name) else "new")
        elif prior["key"] != unit["key"]:
            reached = [mapped["type_names"][i] for i in unit["types"]]
            reason = dict(kind="code", types=sorted(t for t in reached if t in changed_types)[:5])
        else:
            moved = moved_inputs(prior.get("files", {}))
            reason = dict(kind="files", files=sorted(moved)[:5]) if moved else dict(kind="reusable")
        decisions[name] = reason
    reusable = [n for n, d in decisions.items() if d["kind"] == "reusable"]
    audited = {n for n in reusable if rng() < audit_rate} if mode in ("enforce", "refresh") else set()
    if mode == "sweep":
        to_run = sorted(units)
    elif mode == "plan":
        to_run, audited = [], set()
    else:
        to_run = sorted(n for n, d in decisions.items() if d["kind"] != "reusable" or n in audited)

    exit_code, seconds = 0, 0.0
    recording = mode in ("refresh", "sweep")
    recorded, failed = [], []
    if to_run and not recording:
        exit_code, seconds = run_tests(template, to_run, len(to_run) == len(units), workdir, log)
    elif to_run:
        classes = {n: e for n, e in old.items() if n in units and n not in to_run and e["key"] == units[n]["key"]}

        def save():
            write_json(record_path, dict(version=RECORD_VERSION, commit=commit, globals=globals_key,
                                         classes=classes, types=now_types))

        def done(name, code, inputs, took):
            nonlocal exit_code
            if code == 0:
                classes[name] = dict(key=units[name]["key"], files=inputs, commit=commit, seconds=took)
                recorded.append(name)
            else:
                failed.append(name)
                exit_code = exit_code or code
            if len(recorded) % 10 == 0:
                save()  # a run cut short keeps what it recorded

        prior_seconds = {n: e.get("seconds", 0) for n, e in record.get("classes", {}).items()}
        slowest = sorted(prior_seconds, key=lambda n: -prior_seconds[n])
        seconds = record_classes(template, to_run, workdir, output_dir, tracked, content, log, jobs, done, slowest)
        save()

    reasons = {}
    for d in decisions.values():
        reasons[d["kind"]] = reasons.get(d["kind"], 0) + 1
    row = dict(
        job=job, mode=mode, commit=commit, started=started.isoformat(),
        units=len(units), predicted_reusable=len(reusable),
        ran=len(to_run), reused=len(units) - len(to_run), audited=len(audited),
        exit_code=exit_code, seconds_ran=seconds, reasons=reasons,
        run=sorted(to_run)[:200],
        why={n: decisions[n] for n in sorted(to_run)[:50] if decisions[n]["kind"] != "reusable"},
        dynamic=sorted(n for n, u in units.items() if u.get("dynamic")),
        recorded=len(recorded), failed=sorted(failed),
    )
    if receipt:
        write_json(Path(receipt), row)
    if listing:
        Path(listing).write_text("".join(n + "\n" for n in to_run), encoding="utf8")
    return row
