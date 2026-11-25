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


def push(proof, sig, remote, repo, *, prefix=REF_PREFIX):
    """Push the proof and its signature as one commit under prefix<commit>/<id>.
    The lease makes the push fail rather than replace an existing ref."""
    payload = json.loads(gzip.decompress(Path(proof).read_bytes()))
    blobs = [git(repo, "hash-object", "-w", str(path)) for path in (proof, sig)]
    tree = git(repo, "mktree", data=(
        f"100644 blob {blobs[0]}\tproof.json.gz\n100644 blob {blobs[1]}\tproof.json.gz.sig\n"
    ).encode())
    env = dict(GIT_AUTHOR_NAME="lit", GIT_AUTHOR_EMAIL="lit@localhost",
               GIT_COMMITTER_NAME="lit", GIT_COMMITTER_EMAIL="lit@localhost")
    made = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-m", f"lit proof for {payload['commit']}"],
        check=True, capture_output=True, env=dict(os.environ, **env),
    ).stdout.decode().strip()
    ref = proof_ref(payload["commit"], sig, prefix)
    git(repo, "push", "--quiet", f"--force-with-lease={ref}:", remote, f"{made}:{ref}")
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


def take(remote, repo, commits, store, *, repository, allowed_signers, image=None,
         max_age=timedelta(hours=72), revoked=(), log=print):
    """Fetch, verify and unpack every usable proof for commits into store.
    Returns the accepted proofs' signers; a rejected proof is reported and skipped."""
    accepted = []
    with tempfile.TemporaryDirectory(prefix="lit-proofs-") as scratch:
        for proof, sig in fetch(remote, repo, commits, scratch):
            try:
                payload = verify(proof, sig, allowed_signers, repository=repository, commits=commits,
                                 image=image, max_age=max_age, revoked=revoked)
            except Rejected as reason:
                log(f"Skipped a proof for {proof.parent.name[:12]}: {reason}")
                continue
            count = unpack(payload, store)
            accepted.append(dict(commit=payload["commit"], signer=payload["signer"],
                                 fingerprint=payload["fingerprint"], records=count))
            log(f"Took {count} records for {payload['commit'][:12]} signed by {payload['signer']}")
    return accepted


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
    args = p.parse_args(argv)
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
                        allowed_signers=allowed, image=args.image, max_age=max_age, revoked=revoked)
        print(json.dumps(accepted, indent=2))
    return 0
