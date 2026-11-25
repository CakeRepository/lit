"""Single-runner merge benchmark. Run as python -m benchmarks.runner."""

import argparse
from array import array
from contextlib import nullcontext
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch

from concurrent_merge.engine import Engine, Repo
from concurrent_merge.parallel import ordered_map


def snapshot(repo, width, count):
    def directory(mode, kind, default, replacements):
        records = (
            f"{mode} {kind} {replacements.get(i, default)}\t{i:06d}\0"
            for i in range(width)
        )
        return repo.out("mktree", "-z", data="".join(records).encode())

    blob = repo.out("hash-object", "-w", "--stdin", data=b"original\n")
    changed = repo.out("hash-object", "-w", "--stdin", data=b"revised\n")
    leaves = directory("100644", "blob", blob, {})
    middle = directory("040000", "tree", leaves, {})
    root = directory("040000", "tree", middle, {})
    base = repo.out("commit-tree", root, "-m", "base")
    replacements, prs = {}, []
    for i in range(count):
        leaf = directory("100644", "blob", blob, {i: changed})
        replacements[i] = directory("040000", "tree", leaves, {i: leaf})
        head = directory("040000", "tree", middle, {i: replacements[i]})
        commit = repo.out("commit-tree", head, "-p", base, "-m", str(i))
        prs.append(dict(number=i + 1, author=f"agent-{i}", head=commit))
    expected = directory("040000", "tree", middle, replacements)
    return base, prs, expected


def benchmark_merge(tmp, args):
    repo = Repo(tmp / "fixture.git")
    subprocess.run(["git", "init", "--quiet", "--bare", str(repo.path)], check=True)
    repo.run("config", "user.name", "Benchmark")
    repo.run("config", "user.email", "bench@localhost")
    start = perf_counter()
    base, prs, expected = snapshot(repo, args.width, args.prs)
    setup = perf_counter() - start
    runs = {
        k: [] for k in ("serial_three_way", "disjoint_one_worker", "disjoint_parallel")
    }
    path_times = []
    for iteration in range(args.repeats):
        start = perf_counter()
        paths = repo.paths(base, expected)
        path_times.append(perf_counter() - start)
        assert paths == {"/".join([f"{i:06d}"] * 3) for i in range(args.prs)}
        modes = list(runs)
        for mode in modes[iteration % 3 :] + modes[: iteration % 3]:
            engine = Engine(
                repo, tmp / mode, jobs=args.jobs if mode == "disjoint_parallel" else 1
            )
            baseline = patch(
                "concurrent_merge.engine.disjoint_deltas", return_value=None
            )
            with baseline if mode == "serial_three_way" else nullcontext():
                start = perf_counter()
                final, accepted = engine.batch(base, prs, lambda _: "model")
                runs[mode].append(perf_counter() - start)
            assert len(accepted) == args.prs
            assert engine.history[-1]["tree"] == expected
            assert engine.history[-1]["assembly"] == (
                "three-way" if mode == "serial_three_way" else "disjoint"
            )
            reachable = set(repo.out("rev-list", final, "^" + base).splitlines())
            assert all(p["head"] in reachable for p in prs)
    return dict(
        logical_paths_per_tree=args.width**3,
        changes=args.prs,
        jobs=args.jobs,
        fixture_build_seconds=setup,
        compare_seconds=path_times,
        assembly_seconds=runs,
        median_seconds={k: median(v) for k, v in runs.items()},
    )


def benchmark_raw(tmp, args):
    # A bandwidth reference for unindexed positional comparison. Inputs are
    # distinct on-disk arrays. Standard library only.
    from benchmarks.indexed_diff import benchmark_indexed, count_mismatches

    block = 1_000_000
    size = args.raw_entries
    names = [tmp / "left.u32", tmp / "right.u32"]
    mistakes = {0, size // 2, size - 1}
    start = perf_counter()
    with names[0].open("wb") as left, names[1].open("wb") as right:
        for offset in range(0, size, block):
            n = min(block, size - offset)
            values = array("I", range(offset, offset + n))
            if values.itemsize != 4:
                raise RuntimeError("Expected a 4-byte unsigned array typecode")
            if sys.byteorder != "little":
                values.byteswap()
            left.write(values.tobytes())
            for i in mistakes:
                if offset <= i < offset + n:
                    values[i - offset] ^= 1
            right.write(values.tobytes())
    setup = perf_counter() - start

    def scan(bounds):
        begin, end = bounds
        total = 0
        with names[0].open("rb") as left, names[1].open("rb") as right:
            left.seek(begin * 4)
            right.seek(begin * 4)
            for i in range(begin, end, block):
                n = min(block, end - i)
                a = left.read(n * 4)
                b = right.read(n * 4)
                if len(a) != n * 4 or len(b) != n * 4:
                    raise ValueError("Incomplete benchmark input")
                total += count_mismatches(a, b, 0, n)
        return total

    times = {j: [] for j in {1, args.jobs}}
    for i in range(args.repeats):
        for j in sorted(times, reverse=bool(i % 2)):
            ranges = [(size * k // j, size * (k + 1) // j) for k in range(j)]
            start = perf_counter()
            count = sum(ordered_map(scan, ranges, j))
            times[j].append(perf_counter() - start)
            assert count == len(mistakes)
    result = dict(
        entries_per_side=size,
        bytes_read_per_scan=8 * size,
        mismatches=len(mistakes),
        materialize_seconds=setup,
        scan_seconds=times,
        mapped_comparison=benchmark_indexed(
            names, size, args.jobs, args.repeats, len(mistakes)
        ),
    )
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--width", type=int, default=1000)
    p.add_argument("--prs", type=int, default=64)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument(
        "--raw-entries",
        type=int,
        default=0,
        help="Optional pairs of uint32s for a separate physical scan",
    )
    p.add_argument(
        "--temp",
        type=Path,
        help="Parent for the temporary Git repository and optional arrays",
    )
    args = p.parse_args()
    if (
        not 2 <= args.prs <= args.width
        or min(args.jobs, args.repeats) < 1
        or not 0 <= args.raw_entries <= 2**32
    ):
        p.error(
            "Expected 2 <= prs <= width, positive jobs and repeats, and 0..2^32 raw entries"
        )
    if args.temp is not None:
        args.temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.temp) as directory:
        tmp = Path(directory)
        result = dict(merge=benchmark_merge(tmp, args))
        if args.raw_entries:
            result["unindexed_reference"] = benchmark_raw(tmp, args)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
