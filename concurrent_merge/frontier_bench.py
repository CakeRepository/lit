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
import time

from .frontier import Certificate, Frontier


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
                            us_per_change=round((now - started) / steps * 1e6, 1))


if __name__ == "__main__":
    import json
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    for row in bench(changes=n, report=[m for m in (1_000, 10_000, 100_000, 1_000_000) if m <= n]):
        print(json.dumps(row), flush=True)
