"""Signed test results that other machines can check and reuse.

A machine that ran a job's test files with cached-units (in enforce or shadow
mode) packs the passing records it wrote for one commit into a proof, signs it
with ssh-keygen, and pushes it next to the commit. Another machine fetches the
proof, checks the signature against an allowed-signers file read from the
target branch, checks that it is for this repository, an expected commit and
the expected container image, and is fresh, then unpacks its records into a
store that cached-units --proof-store reads. A file whose inputs here match a
proof's record counts as passing there; anything else runs as usual.

The signature says which machine made the claim, not that it is true. Callers
decide how much to trust one: require agreement between several signers, and
rerun a share of what proofs cover (see the README).
"""

from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .engine import write_json
from .unit_store import job_dir, symbols_path, unit_path

KIND = "lit-proof"
VERSION = 1
NAMESPACE = "lit-proof"
REF_PREFIX = "refs/heads/lit/proof/"
SIGNERS_FILE = ".lit/allowed_signers"
HEX = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class Rejected(ValueError):
    """A proof that must not be used. The caller runs the files instead."""


def _utc(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise Rejected("Proof time has no time zone")
    return moment.astimezone(timezone.utc)


def pack(store, jobs, *, repository, commit, tree, image, out, now=None):
    """Write a gzip JSON proof of every passing record each job wrote for commit.

    Only records made on this exact commit go in: a store also holds records
    from older commits, which say nothing about this one."""
    if not HEX.fullmatch(commit) or not HEX.fullmatch(tree):
        raise ValueError("pack needs full commit and tree IDs")
    if not image:
        raise ValueError("pack needs the container image the tests ran in")
    packed = {}
    for job in jobs:
        directory = job_dir(store, job)
        units, symbols = {}, {}
        for path in sorted((directory / "units").glob("*.json")):
            try:
                bundle = json.loads(path.read_text(encoding="utf8"))
            except (OSError, ValueError):
                continue
            if bundle.get("kind") != "lit-unit-footprints" or bundle.get("commit") != commit:
                continue
            for name, unit in bundle["units"].items():
                if unit.get("exit_code") != 0 or unit.get("incomplete"):
                    continue
                units[name] = bundle
                for tracked in (unit.get("symbols") or {}):
                    oid = (bundle["tracked"].get(tracked) or [None, None])[1]
                    table = symbols_path(directory, oid) if oid else None
                    if table and table.exists():
                        symbols[oid] = json.loads(table.read_text(encoding="utf8"))
        if units:
            packed[job] = dict(units=units, symbols=symbols)
    if not packed:
        raise ValueError("No passing records for this commit in the given jobs")
    payload = dict(
        kind=KIND,
        version=VERSION,
        repository=repository,
        commit=commit,
        tree=tree,
        image=image,
        created=(now or datetime.now(timezone.utc)).isoformat(),
        jobs=packed,
    )
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 so the same records always make the same bytes.
    out.write_bytes(gzip.compress(json.dumps(payload, sort_keys=True).encode("utf8"), mtime=0))
    return out


def sign(proof, key):
    """Sign proof with ssh-keygen. key may be a public key whose private half
    is held by ssh-agent (a hardware or TPM key): lit never reads a private key."""
    sig = Path(str(proof) + ".sig")
    sig.unlink(missing_ok=True)
    subprocess.run(
        ["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(proof)],
        check=True, stdin=subprocess.DEVNULL, capture_output=True,
    )
    return sig


def _signer(proof, sig, allowed_signers):
    """(principal, key fingerprint) when sig is a valid signature by an allowed signer."""
    found = subprocess.run(
        ["ssh-keygen", "-Y", "find-principals", "-s", str(sig), "-f", str(allowed_signers),
         "-n", NAMESPACE],
        capture_output=True, text=True,
    )
    principals = found.stdout.split()
    if found.returncode != 0 or not principals:
        raise Rejected("The proof is not signed by an allowed signer")
    with open(proof, "rb") as data:
        checked = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", principals[0],
             "-n", NAMESPACE, "-s", str(sig)],
            stdin=data, capture_output=True, text=True,
        )
    if checked.returncode != 0:
        raise Rejected("The proof's signature does not match its contents")
    match = re.search(r"key (SHA256:\S+)", checked.stdout + checked.stderr)
    return principals[0], match.group(1) if match else ""


def verify(proof, sig, allowed_signers, *, repository, commits, image=None,
           max_age=timedelta(hours=72), revoked=(), now=None):
    """The proof's payload, with signer and fingerprint added, or Rejected."""
    principal, fingerprint = _signer(proof, sig, allowed_signers)
    if fingerprint and fingerprint in set(revoked):
        raise Rejected(f"{principal}'s key is revoked")
    try:
        payload = json.loads(gzip.decompress(Path(proof).read_bytes()))
    except (OSError, ValueError, EOFError) as error:
        raise Rejected(f"Unreadable proof: {error}") from None
    if payload.get("kind") != KIND or payload.get("version") != VERSION:
        raise Rejected("Not a lit proof this version understands")
    if payload.get("repository") != repository:
        raise Rejected(f"Proof is for {payload.get('repository')}, not {repository}")
    if payload.get("commit") not in set(commits):
        raise Rejected("Proof is for a commit this run did not ask about")
    if image is not None and payload.get("image") != image:
        raise Rejected(f"Proof ran in {payload.get('image')}, not {image}")
    now = now or datetime.now(timezone.utc)
    created = _utc(payload.get("created", ""))
    if created > now + timedelta(minutes=5):
        raise Rejected("Proof is dated in the future")
    if now - created > max_age:
        raise Rejected("Proof is too old")
    for job, part in payload.get("jobs", {}).items():
        job_dir(".", job)  # rejects job names that could leave the store
        for name, bundle in part.get("units", {}).items():
            unit = (bundle.get("units") or {}).get(name)
            if (bundle.get("kind") != "lit-unit-footprints" or bundle.get("commit") != payload["commit"]
                    or bundle.get("tree", payload.get("tree")) != payload.get("tree")
                    or not unit or unit.get("exit_code") != 0 or unit.get("incomplete")):
                raise Rejected(f"Proof record for {job} {name} is not a passing record of its commit")
        for oid in part.get("symbols", {}):
            if not HEX.fullmatch(oid):
                raise Rejected("Proof holds a function table with a bad name")
    return dict(payload, signer=principal, fingerprint=fingerprint)


def unpack(payload, store):
    """Write a verified proof's records into store, one directory per job.
    File names are recomputed from each unit's name, never taken from the proof."""
    written = 0
    for job, part in payload["jobs"].items():
        directory = job_dir(store, job)
        for name, bundle in part["units"].items():
            write_json(unit_path(directory, name), bundle)
            written += 1
        for oid, table in part.get("symbols", {}).items():
            write_json(symbols_path(directory, oid), table)
    return written


def proof_ref(commit, sig, prefix=REF_PREFIX):
    return f"{prefix}{commit}/{sha256(Path(sig).read_bytes()).hexdigest()[:16]}"


def git(repo, *args, data=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, input=data,
    ).stdout.decode().strip()


LIT_IDENTITY = dict(GIT_AUTHOR_NAME="lit", GIT_AUTHOR_EMAIL="lit@localhost",
                    GIT_COMMITTER_NAME="lit", GIT_COMMITTER_EMAIL="lit@localhost")


def _commit_tree(repo, tree, message, parent=None, *, date=None):
    identity = dict(os.environ, **LIT_IDENTITY)
    if date is not None:
        identity.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-m", message] + (["-p", parent] if parent else []),
        check=True, capture_output=True, env=identity,
    ).stdout.decode().strip()


def push(proof, sig, remote, repo, *, prefix=REF_PREFIX):
    """Push the proof and its signature as one commit under prefix<commit>/<id>.
    The lease makes the push fail rather than replace an existing ref."""
    payload = json.loads(gzip.decompress(Path(proof).read_bytes()))
    blobs = [git(repo, "hash-object", "-w", str(path)) for path in (proof, sig)]
    tree = git(repo, "mktree", data=(
        f"100644 blob {blobs[0]}\tproof.json.gz\n100644 blob {blobs[1]}\tproof.json.gz.sig\n"
    ).encode())
    date = f"@{int(datetime.fromisoformat(payload['created']).timestamp())} +0000"
    made = _commit_tree(repo, tree, f"lit proof for {payload['commit']}", date=date)
    ref = proof_ref(payload["commit"], sig, prefix)
    try:
        git(repo, "push", "--quiet", f"--force-with-lease={ref}:", remote, f"{made}:{ref}")
    except subprocess.CalledProcessError:
        # Older clients timestamped the envelope at push time. Accept that ref
        # only when its complete tree is the exact proof and signature we hold.
        listed = git(repo, "ls-remote", "--refs", remote, ref).split()
        if len(listed) != 2 or listed[1] != ref or not HEX.fullmatch(listed[0]):
            raise
        git(repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", remote, ref)
        if git(repo, "rev-parse", "--verify", listed[0] + "^{tree}") != tree:
            raise
    return ref


def fetch(remote, repo, commits, into, *, prefix=REF_PREFIX):
    """Download every proof pushed for commits. Yields (proof path, signature path)."""
    wanted = [prefix + c + "/*" for c in commits if HEX.fullmatch(c)]
    if not wanted:
        return
    listed = git(repo, "ls-remote", remote, *wanted)
    for line in listed.splitlines():
        oid, ref = line.split()
        if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{40,64}/[0-9a-f]{16}", ref):
            continue
        git(repo, "fetch", "--quiet", "--no-tags", remote, ref)
        folder = Path(into) / ref[len(prefix):].replace("/", "-")
        folder.mkdir(parents=True, exist_ok=True)
        files = {}
        for name in ("proof.json.gz", "proof.json.gz.sig"):
            try:
                content = subprocess.run(
                    ["git", "-C", str(repo), "cat-file", "blob", f"{oid}:{name}"],
                    check=True, capture_output=True,
                ).stdout
            except subprocess.CalledProcessError:
                break
            files[name] = folder / name
            files[name].write_bytes(content)
        if len(files) == 2:
            yield files["proof.json.gz"], files["proof.json.gz.sig"]


def agreement_key(bundle, name):
    """What two machines must report alike for one file: every repository input it
    read with its content, the directories it listed, the files it found missing,
    and its pass. Timing, host paths and the runner's kernel are left out."""
    unit = bundle["units"][name]
    reads = dict(
        tracked={path: bundle["tracked"].get(path) for path in unit.get("tracked", [])},
        listings={d: bundle.get("listing_children", {}).get(d) for d in unit.get("listings", [])},
        absent=sorted(unit.get("absent") or []),
        exit_code=unit.get("exit_code"),
    )
    return sha256(json.dumps(reads, sort_keys=True).encode("utf8")).hexdigest()


class Book:
    """Each signer's audit record, kept by CI where signers cannot write it.

    A signer starts with every file its proofs cover rerun (share 1). Each
    clean streak of `step` audits halves the share, down to `floor`. A failed
    audit resets the streak and drops every proof that signer made before it."""

    def __init__(self, data=None, *, step=20, floor=0.05, parent=None):
        self.data = data or {}
        self.step, self.floor, self.parent = step, floor, parent

    @classmethod
    def load(cls, path, **kw):
        try:
            saved = json.loads(Path(path).read_text(encoding="utf8"))
        except (OSError, ValueError):
            return cls(**kw)
        if saved.get("kind") != "lit-audit-book":
            return cls(**kw)
        return cls(saved.get("signers", {}), parent=saved.get("parent"), **kw)

    def save(self, path):
        write_json(Path(path), dict(kind="lit-audit-book", version=1, parent=self.parent,
                                    signers=self.data))

    def share(self, principal):
        clean = self.data.get(principal, {}).get("clean", 0)
        return max(self.floor, 0.5 ** (clean // self.step))

    def dropped_before(self, principal):
        reset = self.data.get(principal, {}).get("reset")
        return _utc(reset) if reset else None

    def record(self, principal, ok, now=None):
        row = self.data.setdefault(principal, dict(clean=0, failed=0, reset=None))
        if ok:
            row["clean"] += 1
        else:
            row.update(clean=0, failed=row.get("failed", 0) + 1,
                       reset=(now or datetime.now(timezone.utc)).isoformat())


SIGNERS_INDEX = "proof-signers.json"
BOOK_REF = "refs/heads/lit/audits"


def book_pull(remote, repo, out, *, ref=BOOK_REF):
    """Fetch the audit book kept at ref into out (an empty book if there is none
    yet), remembering which commit it came from so book_push cannot overwrite a
    newer one."""
    listed = git(repo, "ls-remote", remote, ref)
    if not listed:
        Book().save(out)
        return Book.load(out)
    oid = listed.split()[0]
    git(repo, "fetch", "-q", remote, f"+{ref}:refs/lit-book/fetched")
    text = git(repo, "cat-file", "blob", f"{oid}:book.json")
    Path(out).write_text(text + "\n", encoding="utf8")
    book = Book.load(out)
    book.parent = oid
    book.save(out)
    return book


def book_push(path, remote, repo, *, ref=BOOK_REF):
    """Push the book at path to ref, only if ref still points where book_pull
    found it. Keep the jobs that write it in one concurrency group, and let only
    the CI token push ref: whoever can write it can clear a signer's record."""
    book = Book.load(path)
    parent, book.parent = book.parent, None
    blob = git(repo, "hash-object", "-w", "--stdin",
               data=json.dumps(dict(kind="lit-audit-book", version=1, signers=book.data),
                               indent=2, sort_keys=True).encode())
    tree = git(repo, "mktree", data=f"100644 blob {blob}\tbook.json\n".encode())
    commit = _commit_tree(repo, tree, "lit audit book", parent)
    lease = f"--force-with-lease={ref}:{parent or ''}"
    git(repo, "push", "-q", lease, remote, f"{commit}:{ref}")
    book.parent = commit
    book.save(path)
    return commit


def take(remote, repo, commits, store, *, repository, allowed_signers, image=None,
         max_age=timedelta(hours=72), revoked=(), quorum=1, book=None, log=print):
    """Fetch and verify every proof for commits, then unpack each file's record
    that at least `quorum` different signers reported alike (see agreement_key).
    Writes store/<job>/proof-signers.json naming who vouched for each file, for
    the audit share cached-units applies. Returns the accepted proofs."""
    accepted, votes = [], {}
    with tempfile.TemporaryDirectory(prefix="lit-proofs-") as scratch:
        for proof, sig in fetch(remote, repo, commits, scratch):
            try:
                payload = verify(proof, sig, allowed_signers, repository=repository, commits=commits,
                                 image=image, max_age=max_age, revoked=revoked)
                dropped = book.dropped_before(payload["signer"]) if book else None
                if dropped and _utc(payload["created"]) <= dropped:
                    raise Rejected(f"{payload['signer']} failed an audit after making it")
            except Rejected as reason:
                log(f"Skipped a proof for {proof.parent.name[:12]}: {reason}")
                continue
            accepted.append(dict(commit=payload["commit"], signer=payload["signer"],
                                 fingerprint=payload["fingerprint"]))
            for job, part in payload["jobs"].items():
                for name, bundle in part["units"].items():
                    slot = votes.setdefault((job, name, agreement_key(bundle, name)), dict(signers=set()))
                    slot["signers"].add(payload["signer"])
                    slot.setdefault("bundle", bundle)
                    slot.setdefault("symbols", part.get("symbols", {}))
    agreed = {}
    for (job, name, _), slot in votes.items():
        if len(slot["signers"]) < quorum:
            continue
        held = agreed.setdefault(job, {})
        # Two different agreeing groups for one file is a disagreement: use neither.
        held[name] = None if name in held else slot
    index = {}
    for job, files in agreed.items():
        directory = job_dir(store, job)
        for name, slot in files.items():
            if slot is None:
                log(f"Signers disagree about {job} {name}; it runs")
                continue
            write_json(unit_path(directory, name), slot["bundle"])
            for oid, table in slot["symbols"].items():
                write_json(symbols_path(directory, oid), table)
            index.setdefault(job, {})[name] = sorted(slot["signers"])
        write_json(directory / SIGNERS_INDEX, index.get(job, {}))
    for job, files in index.items():
        log(f"Took {len(files)} {job} files that {quorum} or more signers agree on")
    return accepted


def vouchers(proof_store, job, name):
    """The signers whose agreeing proofs put name's record in proof_store, if any."""
    try:
        index = json.loads((job_dir(proof_store, job) / SIGNERS_INDEX).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return []
    return index.get(name, [])


def signers_at(repo, rev, out):
    """Copy the allowed-signers file as it is at rev (the target branch, never a
    PR's own head, so a change cannot add the key that vouches for it)."""
    content = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{SIGNERS_FILE}"], capture_output=True,
    )
    if content.returncode != 0:
        raise Rejected(f"{rev} has no {SIGNERS_FILE}")
    Path(out).write_bytes(content.stdout)
    return Path(out)


def main(argv):
    import argparse

    p = argparse.ArgumentParser(prog="concurrent-merge proof", description=__doc__.split("\n\n")[0])
    cmd = p.add_subparsers(dest="action", required=True)
    pk = cmd.add_parser("pack", help="Pack the passing records a run wrote for HEAD")
    pk.add_argument("--store", type=Path, required=True)
    pk.add_argument("--job", action="append", required=True, help="Job whose records go in (repeat)")
    pk.add_argument("--repository", required=True, help="owner/name the commit belongs to")
    pk.add_argument("--image", required=True, help="Container image the tests ran in, with its digest")
    pk.add_argument("--workdir", type=Path, default=Path("."), help="Checkout of the tested commit")
    pk.add_argument("--out", type=Path, required=True)
    sg = cmd.add_parser("sign", help="Sign a proof with ssh-keygen (the key may live in ssh-agent)")
    sg.add_argument("proof", type=Path)
    sg.add_argument("--key", type=Path, required=True, help="Key file, or the public key of an agent-held key")
    ps = cmd.add_parser("push", help="Push a signed proof next to its commit")
    ps.add_argument("proof", type=Path)
    ps.add_argument("--remote", required=True)
    ps.add_argument("--repo", type=Path, default=Path("."), help="Any local Git repository to build the push in")
    ps.add_argument("--prefix", default=REF_PREFIX)
    for name, help_text in (("verify", "Check one signed proof and unpack it into a store"),
                            ("take", "Fetch, check and unpack every proof pushed for the commits")):
        c = cmd.add_parser(name, help=help_text)
        if name == "verify":
            c.add_argument("proof", type=Path)
        else:
            c.add_argument("--remote", required=True)
            c.add_argument("--prefix", default=REF_PREFIX)
        c.add_argument("--repository", required=True)
        c.add_argument("--commit", action="append", required=True, help="Commit a proof may be for (repeat)")
        signers = c.add_mutually_exclusive_group(required=True)
        signers.add_argument("--allowed-signers", type=Path)
        signers.add_argument("--signers-at", metavar="REV",
                             help=f"Read {SIGNERS_FILE} at REV (the target branch) from --repo")
        c.add_argument("--repo", type=Path, default=Path("."))
        c.add_argument("--image", help="Require proofs made in this container image")
        c.add_argument("--max-age-hours", type=float, default=72)
        c.add_argument("--revoked", type=Path, help="File of revoked key fingerprints, one per line")
        c.add_argument("--into", type=Path, required=True, help="Store for cached-units --proof-store")
        if name == "take":
            c.add_argument("--quorum", type=int, default=1,
                           help="Different signers who must report a file alike before it counts")
            c.add_argument("--audit-book", type=Path,
                           help="Skip proofs a signer made before failing an audit")
    for name, help_text in (("book-pull", "Fetch the signers' audit book"),
                            ("book-push", "Push the audit book back if nobody changed it meanwhile")):
        c = cmd.add_parser(name, help=help_text)
        c.add_argument("book", type=Path)
        c.add_argument("--remote", required=True)
        c.add_argument("--repo", type=Path, default=Path("."))
        c.add_argument("--ref", default=BOOK_REF)
    args = p.parse_args(argv)
    if args.action == "book-pull":
        book_pull(args.remote, args.repo, args.book, ref=args.ref)
        print(args.book)
        return 0
    if args.action == "book-push":
        print(book_push(args.book, args.remote, args.repo, ref=args.ref))
        return 0
    if args.action == "pack":
        commit = git(args.workdir, "rev-parse", "HEAD")
        tree = git(args.workdir, "rev-parse", "HEAD^{tree}")
        print(pack(args.store, args.job, repository=args.repository, commit=commit, tree=tree,
                   image=args.image, out=args.out))
        return 0
    if args.action == "sign":
        print(sign(args.proof, args.key))
        return 0
    if args.action == "push":
        print(push(args.proof, Path(str(args.proof) + ".sig"), args.remote, args.repo, prefix=args.prefix))
        return 0
    revoked = args.revoked.read_text(encoding="utf8").split() if args.revoked else ()
    max_age = timedelta(hours=args.max_age_hours)
    with tempfile.TemporaryDirectory(prefix="lit-signers-") as scratch:
        allowed = args.allowed_signers or signers_at(args.repo, args.signers_at, Path(scratch) / "allowed")
        if args.action == "verify":
            try:
                payload = verify(args.proof, Path(str(args.proof) + ".sig"), allowed,
                                 repository=args.repository, commits=args.commit, image=args.image,
                                 max_age=max_age, revoked=revoked)
            except Rejected as reason:
                print(f"Rejected: {reason}")
                return 1
            print(f"Took {unpack(payload, args.into)} records signed by {payload['signer']}")
            return 0
        accepted = take(args.remote, args.repo, args.commit, args.into, repository=args.repository,
                        allowed_signers=allowed, image=args.image, max_age=max_age, revoked=revoked,
                        quorum=args.quorum, book=Book.load(args.audit_book) if args.audit_book else None)
        print(json.dumps(accepted, indent=2))
    return 0
