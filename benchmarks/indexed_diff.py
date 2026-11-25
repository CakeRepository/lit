"""Compare identical positional queries on mapped snapshots, with and without a hash index."""

from contextlib import ExitStack
from hashlib import sha256
import mmap
from statistics import median
from time import perf_counter

from concurrent_merge.parallel import ordered_map


LEAF_ENTRIES = 16_384
FANOUT = 256


def build_index(values, jobs):
    import numpy as np

    leaves = (len(values) + LEAF_ENTRIES - 1) // LEAF_ENTRIES
    groups = (leaves + FANOUT - 1) // FANOUT
    digests = np.empty(leaves, dtype="V32")

    def hash_group(group):
        start = group * FANOUT
        end = min(start + FANOUT, leaves)
        for leaf in range(start, end):
            begin = leaf * LEAF_ENTRIES
            digests[leaf] = sha256(values[begin : begin + LEAF_ENTRIES]).digest()
        return sha256(digests[start:end]).digest()

    roots = np.array(ordered_map(hash_group, list(range(groups)), jobs), dtype="V32")
    return roots, digests


def indexed_count(left, right, a, b):
    import numpy as np

    total = 0
    for group in np.flatnonzero(a[0] != b[0]):
        first = group * FANOUT
        group_leaves = slice(first, first + FANOUT)
        changed = np.flatnonzero(a[1][group_leaves] != b[1][group_leaves])
        for leaf in first + changed:
            begin = leaf * LEAF_ENTRIES
            chunk = slice(begin, begin + LEAF_ENTRIES)
            total += np.count_nonzero(left[chunk] != right[chunk])
    return total


def benchmark_indexed(names, size, jobs, repeats, expected_count):
    import numpy as np

    with ExitStack() as stack:
        inputs = []
        for name in names:
            file = stack.enter_context(name.open("rb"))
            if file.seek(0, 2) != size * 4:
                raise ValueError("Snapshot length does not match the benchmark input")
            mapping = stack.enter_context(
                mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
            )
            inputs.append(np.ndarray((size,), dtype=np.uint32, buffer=mapping))
        left, right = inputs
        start = perf_counter()
        a, b = (build_index(values, jobs) for values in inputs)
        setup = perf_counter() - start

        def count_range(bounds):
            begin, end = bounds
            total = 0
            for start in range(begin, end, 1_000_000):
                chunk = slice(start, min(start + 1_000_000, end))
                total += np.count_nonzero(left[chunk] != right[chunk])
            return total

        def full_scan(workers):
            ranges = [
                (size * i // workers, size * (i + 1) // workers) for i in range(workers)
            ]
            return sum(ordered_map(count_range, ranges, workers))

        methods = {
            "full_one_worker": lambda: full_scan(1),
            "full_parallel": lambda: full_scan(jobs),
            "indexed": lambda: indexed_count(left, right, a, b),
        }
        timings = {name: [] for name in methods}
        # Warm each path once, then rotate the measured order. Both methods
        # compare the same mappings and return exact counts, including within
        # each differing leaf. Only setup can see all values to build the index.
        modes = list(methods)
        for i in range(repeats + 1):
            offset = i % len(modes)
            for mode in modes[offset:] + modes[:offset]:
                start = perf_counter()
                count = methods[mode]()
                elapsed = perf_counter() - start
                if count != expected_count:
                    raise AssertionError(f"{mode} found {count} mismatches")
                if i:
                    timings[mode].append(elapsed)
        medians = {name: median(samples) for name, samples in timings.items()}
        return dict(
            leaf_entries=LEAF_ENTRIES,
            fanout=FANOUT,
            index_bytes_per_snapshot=sum(level.nbytes for level in a),
            build_both_indexes_seconds=setup,
            comparisons_seconds=timings,
            medians_seconds=medians,
            repeated_query_speedup=min(
                medians["full_one_worker"], medians["full_parallel"]
            )
            / medians["indexed"],
        )
