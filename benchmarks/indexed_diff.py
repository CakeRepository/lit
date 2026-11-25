"""Compare identical positional queries on mapped snapshots, with and without a hash index."""

from contextlib import ExitStack
from hashlib import sha256
import mmap
import struct
from statistics import median
from time import perf_counter

from concurrent_merge.parallel import ordered_map


LEAF_ENTRIES = 16_384
FANOUT = 256


def count_mismatches(left, right, begin, end):
    """Exact count of differing uint32 entries in [begin, end), standard library only.

    Equal byte ranges are skipped with a memcmp-speed comparison; differing ranges
    are bisected down to a small run that is compared entry by entry.
    """
    if left[begin * 4 : end * 4] == right[begin * 4 : end * 4]:
        return 0
    if end - begin <= 64:
        a = struct.unpack_from(f"<{end - begin}I", left, begin * 4)
        b = struct.unpack_from(f"<{end - begin}I", right, begin * 4)
        return sum(x != y for x, y in zip(a, b))
    mid = (begin + end) // 2
    return count_mismatches(left, right, begin, mid) + count_mismatches(
        left, right, mid, end
    )


def build_index(values, size, jobs):
    leaves = (size + LEAF_ENTRIES - 1) // LEAF_ENTRIES
    groups = (leaves + FANOUT - 1) // FANOUT
    view = memoryview(values)
    digests = [b""] * leaves

    def hash_group(group):
        start = group * FANOUT
        end = min(start + FANOUT, leaves)
        for leaf in range(start, end):
            begin = leaf * LEAF_ENTRIES * 4
            digests[leaf] = sha256(view[begin : begin + LEAF_ENTRIES * 4]).digest()
        return sha256(b"".join(digests[start:end])).digest()

    roots = ordered_map(hash_group, list(range(groups)), jobs)
    return roots, digests


def indexed_count(left, right, size, a, b):
    total = 0
    for group, (ra, rb) in enumerate(zip(a[0], b[0])):
        if ra == rb:
            continue
        first = group * FANOUT
        for leaf in range(first, min(first + FANOUT, len(a[1]))):
            if a[1][leaf] != b[1][leaf]:
                begin = leaf * LEAF_ENTRIES
                total += count_mismatches(
                    left, right, begin, min(begin + LEAF_ENTRIES, size)
                )
    return total


def benchmark_indexed(names, size, jobs, repeats, expected_count):
    with ExitStack() as stack:
        inputs = []
        for name in names:
            file = stack.enter_context(name.open("rb"))
            if file.seek(0, 2) != size * 4:
                raise ValueError("Snapshot length does not match the benchmark input")
            mapping = stack.enter_context(
                mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
            )
            inputs.append(mapping)
        left, right = inputs
        start = perf_counter()
        a, b = (build_index(values, size, jobs) for values in inputs)
        setup = perf_counter() - start

        def count_range(bounds):
            begin, end = bounds
            total = 0
            for start in range(begin, end, 1_000_000):
                total += count_mismatches(
                    left, right, start, min(start + 1_000_000, end)
                )
            return total

        def full_scan(workers):
            ranges = [
                (size * i // workers, size * (i + 1) // workers) for i in range(workers)
            ]
            return sum(ordered_map(count_range, ranges, workers))

        methods = {
            "full_one_worker": lambda: full_scan(1),
            "full_parallel": lambda: full_scan(jobs),
            "indexed": lambda: indexed_count(left, right, size, a, b),
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
            index_bytes_per_snapshot=len(a[0]) * 32 + len(a[1]) * 32,
            build_both_indexes_seconds=setup,
            comparisons_seconds=timings,
            medians_seconds=medians,
            repeated_query_speedup=min(
                medians["full_one_worker"], medians["full_parallel"]
            )
            / medians["indexed"],
        )
