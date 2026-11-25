"""How the program map's indexes scale with the number of changes in flight.

Builds a map of `units` test units, each reading `reads` functions out of
`functions`, then admits `changes` changes of `writes` functions each, all
in flight at once. For each change it does what landing alone needs: admit
(refused on a same-function clash), record the change's own passes, ask what
must still run, and land when nothing must. It times those steps per change
as the number in flight grows. If a step compared changes with each other,
its time would grow with the count; with the two indexes it should stay flat.
"""

import random
import sys
import time

try:
    import resource
except ImportError:  # not on Windows
    resource = None

from .frontier import Certificate, Frontier


def peak_mb():
    """This process's peak resident memory so far, in MB (None where the platform can't say)."""
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def bench(*, changes=1_000_000, functions=5_000_000, units=50_000, reads=40, writes=3,
          land_every=2, seed=3, report=(1_000, 10_000, 100_000, 1_000_000)):
    rng = random.Random(seed)
    f = Frontier({})
    for u in range(units):
        f.record(f"u{u}", [f"f{rng.randrange(functions)}" for _ in range(reads)])
    started, steps, refused, landed = time.perf_counter(), 0, 0, 0
    marks = sorted(report)
    for cid in range(changes):
        w = {f"f{rng.randrange(functions)}": f"c{cid}" for _ in range(writes)}
        if f.admit(cid, w):
            refused += 1
            f.refused.pop(cid)
        else:
            # The change's own PR run: every unit reading what it writes, on main plus it.
            for unit in f.cone(cid):
                f.record(unit, f.reads[unit], members={cid})
            # Leave every other change in flight, so the map holds them all at once.
            if cid % land_every == 0 and not f.needs([cid]):
                f.land([cid])
                landed += 1
        steps += 1
        if marks and steps == marks[0]:
            marks.pop(0)
            now = time.perf_counter()
            yield dict(in_flight=len(f.changes), seen=steps, refused=refused, landed=landed,
                       us_per_change=round((now - started) / steps * 1e6, 1), peak_mb=peak_mb())


def grouping(*, changes=1_000_000, functions=5_000_000, units=50_000, reads=40, writes=3,
             seed=3, report=(1_000, 10_000, 100_000, 1_000_000), repeat=1):
    """Time components() over every change in flight as the count grows, with nothing landing.

    Grouping is the scheduler's first question each round. Changes that share a
    test unit join one component, so with nothing landing the largest group
    grows with the count. Grouping work per change is its cone times the
    in-flight writers each cone unit reads, so it rises as the map fills with
    writers (hot fan-out) and is bounded by `reads`; it does not grow with the
    size of the group a change ends up in. Each mark keeps the fastest of
    `repeat` calls."""
    rng = random.Random(seed)
    f = Frontier({})
    for u in range(units):
        f.record(f"u{u}", [f"f{rng.randrange(functions)}" for _ in range(reads)])
    marks = sorted(report)
    for cid in range(changes):
        w = {f"f{rng.randrange(functions)}": f"c{cid}" for _ in range(writes)}
        if f.admit(cid, w):
            f.refused.pop(cid)
        if marks and cid + 1 == marks[0]:
            marks.pop(0)
            seconds = float("inf")
            for _ in range(repeat):
                started = time.perf_counter()
                groups = f.components()
                seconds = min(seconds, time.perf_counter() - started)
            yield dict(in_flight=len(f.changes), seen=cid + 1, components=len(groups),
                       largest=max(map(len, groups), default=0),
                       us_per_change=round(seconds / max(len(f.changes), 1) * 1e6, 2), peak_mb=peak_mb())


if __name__ == "__main__":
    import json
    import sys
    args = sys.argv[1:]
    run = bench
    if args and args[0] == "grouping":
        run, args = grouping, args[1:]
    n = int(args[0]) if args else 1_000_000
    for row in run(changes=n, report=[m for m in (1_000, 10_000, 100_000, 1_000_000) if m <= n]):
        print(json.dumps(row), flush=True)
