"""Test failure splitting on archived two-parent PR merges using a real lit binary.

python -m benchmarks.replay_failures ARCHIVE.git --branch main --lit /path/to/lit
Each size ends at the same selected merge. --out writes private per-window receipts.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def git(repo, *args, data=None, env=None):
    return subprocess.run(["git", "--literal-pathspecs", "-C", repo, *args], input=data,
                          stdout=subprocess.PIPE, check=True, env=env).stdout


def blobs(repo, rev, paths):
    entries = git(repo, "ls-tree", "-rz", rev, "--", *paths)
    found = {}
    for entry in entries.split(b"\0")[:-1]:
        meta, path = entry.split(b"\t", 1)
        mode, kind, oid = meta.split()
        if kind == b"blob" and mode in {b"100644", b"100755"}:
            found[os.fsdecode(path)] = oid.decode()
    return found


def fires(repo, rev, markers):
    if not markers:
        return False
    actual = blobs(repo, rev, markers)
    return all(actual.get(path) == oid for path, oid in markers.items())


def select_pair(repo, window):
    base = window[0]["base"]
    paths = []
    ancestors = []
    heads = {row["head"] for row in window}
    for row in window:
        names = git(repo, "diff", "--no-renames", "--name-only", "-z", row["base"], row["merge"])
        paths.append([os.fsdecode(name) for name in names.split(b"\0")[:-1]])
        reachable = git(repo, "rev-list", row["head"], "^" + base).decode().splitlines()
        ancestors.append((heads & set(reachable)) - {row["head"]})
    counts = Counter(p for names in paths for p in names)
    required = set().union(*ancestors)
    markers = {}
    for i, row in enumerate(window):
        if row["head"] in required:
            continue
        unique = [p for p in paths[i] if counts[p] == 1]
        if not unique:
            continue
        after = blobs(repo, row["merge"], unique)
        on_head = blobs(repo, row["head"], unique)
        before = blobs(repo, base, unique)
        for path in sorted(after):
            if on_head.get(path) == after[path] and before.get(path) != after[path]:
                markers[i] = {path: after[path]}
                break
    # Both must be independently orderable. Another PR requiring a culprit
    # would legitimately be held too. Choose before observing any lit result.
    pairs = [(a, b) for a in markers for b in markers if a < b and ancestors[a] == ancestors[b]]
    if not pairs:
        raise ValueError("No independent source pair with unique changed blobs in this window")
    a, b = max(pairs, key=lambda pair: pair[1] - pair[0])
    pair = markers[a] | markers[b]
    if not fires(repo, window[-1]["merge"], pair):
        raise ValueError("Both source markers must be present in the recorded final tree")
    for i, row in enumerate(window):
        present = blobs(repo, row["head"], pair)
        for owner in (a, b):
            if (markers[owner].items() <= present.items()) != (i == owner):
                raise ValueError("A source marker does not distinguish its owner from the other heads")
    return a, b, markers[a], markers[b]


def expected_tree(repo, window, held, scratch):
    # A separate Git index starts at the recorded final tree. Undo the held
    # PR's actual first-parent delta, including file modes, renames and bytes.
    index = Path(scratch) / "oracle.index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    final = window[-1]["merge"]
    git(repo, "read-tree", final, env=env)
    def apply(row, *options):
        patch = git(repo, "diff", "--no-renames", "--no-ext-diff", "--no-textconv",
                    "--full-index", "--binary", row["base"], row["merge"])
        if patch:
            git(repo, "apply", "--cached", "--whitespace=nowarn", *options, "-", data=patch, env=env)

    if held is not None:
        apply(held, "--reverse")
    expected = git(repo, "write-tree", env=env)
    git(repo, "read-tree", window[0]["base"], env=env)
    for row in window:
        if row != held:
            apply(row)
    if git(repo, "write-tree", env=env) != expected:
        raise ValueError("Forward and reverse Git oracles disagree on the expected tree")
    return expected.decode().strip()


def probe(marker_file):
    markers = json.loads(marker_file.read_bytes())
    head = git(".", "rev-parse", "--verify", "HEAD").decode().strip()
    fail = fires(".", head, markers)
    with marker_file.with_name("executions.jsonl").open("a", encoding="utf8") as log:
        log.write(json.dumps([head, "fail" if fail else "pass"]) + "\n")
    return int(fail)


def replay(repo, window, order, markers, held, case, binary, scratch):
    queue = Path(scratch) / "queue.json"
    numbers = {r["number"] for r in order}
    expected = {held["number"]} if held else set()
    tree = expected_tree(repo, window, held, scratch)
    queue.write_text(json.dumps({
        "repository": "replay/replay", "branch": "main", "base": window[0]["base"],
        "changes": [dict(number=r["number"], author="replay", head=r["head"]) for r in order],
    }), encoding="utf8")
    marker_file = Path(scratch) / "markers.json"
    marker_file.write_text(json.dumps(markers), encoding="utf8")
    proc = subprocess.run([binary, "run", str(queue), "--repo", str(repo), "--state", str(scratch),
                           "--test", sys.executable, "-B", str(Path(__file__).resolve()), "--probe", str(marker_file)],
                          capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"lit run exited {proc.returncode}: {proc.stderr[-1500:]}")
    receipt = Path(proc.stdout.strip().splitlines()[-1])
    result = json.loads(receipt.read_bytes())
    attempts = json.loads(receipt.with_name("attempts.json").read_bytes())
    log = marker_file.with_name("executions.jsonl")
    executions = [tuple(json.loads(line)) for line in log.read_bytes().splitlines()] if log.exists() else []
    recorded = [(a["candidate"], a["result"]) for a in attempts if a["result"] in {"pass", "fail"}]
    probes_match = Counter(executions) == Counter(recorded) and bool(executions)
    returned = {r["returned"]["number"] for r in attempts if "returned" in r}
    matches = git(repo, "rev-parse", result["final"] + "^{tree}").decode().strip() == tree
    correct = (probes_match and proc.returncode == int(bool(expected)) and returned == expected
               and set(result["tested"]) == numbers - expected and matches
               and attempts[0]["result"] == ("fail" if expected else "pass"))
    return dict(size=len(order), case=case, base=window[0]["base"], through=window[-1]["merge"],
                sources=[r["number"] for r in order],
                expected_held=sorted(expected), returned=sorted(returned), accepted=result["tested"],
                attempts=len(attempts), test_runs=len(executions), probes_match=probes_match,
                same_tree=matches, correct=correct)


def main(argv=None):
    from benchmarks.replay_history import merges
    from concurrent_merge.engine import write_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--sizes", default="5,10,25")
    parser.add_argument("--lit", default="lit")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    sizes = [int(n) for n in args.sizes.split(",")]
    binary = shutil.which(args.lit)
    if not binary or min(sizes) < 2:
        parser.error("Supply an executable lit and window sizes of at least two")
    source = args.repo.resolve()
    if git(source, "rev-parse", "--is-bare-repository", "--is-shallow-repository").splitlines() != [b"true", b"false"]:
        parser.error("Supply a full bare Git archive")
    history = merges(source, args.branch, max(sizes))
    if len(history) < max(sizes) or any(a["merge"] != b["base"] for a, b in zip(history, history[1:])):
        parser.error("The selected history must cover contiguous two-parent PR merges")
    results = []
    with tempfile.TemporaryDirectory(prefix="lit-failure-replay-") as tmp:
        repo = Path(tmp) / "batch.git"
        git(source, "clone", "-q", "--bare", "--shared", "--", str(source), str(repo))
        for size in sizes:
            window = history[-size:]
            a, b, first, second = select_pair(repo, window)
            reverse = window.copy()
            reverse[a], reverse[b] = reverse[b], reverse[a]
            for name, order, markers, held in [
                ("control", window, {}, None),
                ("single", window, first, window[a]),
                ("pair", window, first | second, window[b]),
                ("pair-reversed", reverse, first | second, window[a]),
            ]:
                with tempfile.TemporaryDirectory(dir=tmp) as scratch:
                    row = replay(repo, window, order, markers, held, name, binary, scratch)
                results.append(row)
                print(json.dumps(row), flush=True)
                if args.out:
                    write_json(args.out, results)
    return 0 if all(r["correct"] for r in results) else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        try:
            sys.exit(probe(Path(sys.argv[2])))
        except Exception as exc:
            # lit interprets exit 1 as a failing test. Only the predicate may return it.
            print(f"Failure predicate could not run: {exc}", file=sys.stderr)
            sys.exit(2)
    sys.exit(main())
