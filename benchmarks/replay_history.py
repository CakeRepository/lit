"""Replay a repository's real merged PRs through lit and check lit gets the tree main actually got.

Run as python -m benchmarks.replay_history BARE_REPO [--branch main] [--merges 100] [--sizes 2,5,10,25].

Every first-parent merge commit "Merge pull request #N" on the branch has the PR's head as its second parent.
For each window of consecutive merges, lit batches those heads on the commit before the first one, exactly as
if they had all been ready at once, and the result must be the tree the branch had after the last of them.
A different tree means lit merged real changes differently from how they really landed. Nothing is pushed and
no test runs: the test command is `true`, so a shallow copy is enough.
"""

import argparse
import glob
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from statistics import median
from time import perf_counter

LIT = str(Path(__file__).resolve().parent.parent)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout.strip()


def merges(repo, branch, count):
    rows = []
    for line in git(repo, "log", "--first-parent", "--merges", "--format=%H %P|%s", branch).splitlines():
        commit, rest = line.split(" ", 1)
        parents, subject = rest.split("|", 1)
        parents = parents.split()
        found = re.match(r"Merge pull request #(\d+)", subject)
        if len(parents) == 2 and found:
            rows.append({"merge": commit, "base": parents[0], "head": parents[1], "number": int(found.group(1))})
        if len(rows) >= count:
            break
    rows.reverse()
    return rows


def replay(repo, window, scratch):
    queue = Path(scratch) / "queue.json"
    state = Path(scratch) / "state"
    shutil.rmtree(state, ignore_errors=True)
    queue.write_text(json.dumps({
        "repository": "replay/replay", "branch": "main", "base": window[0]["base"],
        "changes": [{"number": r["number"], "author": "replay", "head": r["head"]} for r in window],
    }))
    started = perf_counter()
    subprocess.run(
        [sys.executable, "-B", "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); "
         "from concurrent_merge.cli import main; sys.exit(main())", LIT,
         "run", str(queue), "--repo", repo, "--state", str(state), "--test", "true"],
        capture_output=True, text=True,
    )
    seconds = perf_counter() - started
    attempts = glob.glob(str(state / "runs" / "*" / "attempts.json"))
    if not attempts:
        return {"prs": [r["number"] for r in window], "error": "no attempt recorded", "seconds": round(seconds, 2)}
    first = json.loads(Path(attempts[0]).read_text())[0]
    return {
        "prs": [r["number"] for r in window],
        "assembly": first["assembly"],
        "held": first["pending"],
        "same_tree": first["tree"] == git(repo, "rev-parse", window[-1]["merge"] + "^{tree}"),
        "seconds": round(seconds, 2),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--merges", type=int, default=100)
    parser.add_argument("--sizes", default="2,5,10,25")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    rows = merges(args.repo, args.branch, args.merges)
    for a, b in zip(rows, rows[1:]):
        if b["base"] != a["merge"]:
            parser.error(f"#{b['number']} is not merged directly after #{a['number']}; use fewer --merges")
    results = []
    with tempfile.TemporaryDirectory(prefix="lit-replay-") as scratch:
        for size in map(int, args.sizes.split(",")):
            step = 1 if size == 2 else size
            for i in range(0, len(rows) - size + 1, step):
                results.append(dict(size=size, **replay(args.repo, rows[i:i + size], scratch)))
                print(json.dumps(results[-1]), flush=True)
    for size in sorted({r["size"] for r in results}):
        rs = [r for r in results if r["size"] == size]
        print(
            f"{size:>3} PRs: {len(rs)} windows, {sum(r.get('same_tree', False) for r in rs)} same tree as main, "
            f"{sum(bool(r.get('held')) for r in rs)} held a PR, median {median(r['seconds'] for r in rs):.1f}s",
        )
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1))
    return 0 if all(r.get("same_tree") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
