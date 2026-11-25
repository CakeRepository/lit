"""Replay a stream of changes through a Frontier with one merge runner.

The workload is a synthetic program map for a two-product codebase: web and
database test units that each read a few dozen functions (popular ones far
more often, like shared helpers), and browser journeys that each read a large
share of the second product. Changes write a few functions. With `preproved`, each change
arrives with its own passes (its PR's run on main as it was then, done by the
PR's own CI); the merge runner then only runs what changes have in common.

Questions it answers: how long from ready to landed, how busy the one merge
runner is, how much of the work is overlap, and where it stops scaling.
"""

from dataclasses import dataclass
import heapq
import random
import statistics

from .frontier import Certificate, Frontier


@dataclass
class Program:
    keys: dict  # product -> [keys]
    weights: dict  # product -> popularity weights per key
    units: dict  # unit -> (keys it reads, seconds)


def program(seed=1, app_keys=6000, lib_keys=4000, web=1117, db=427, journeys=84,
            reads=60, journey_share=0.3, zipf=1.1):
    rng = random.Random(seed)
    keys = {"app": [f"app/f{i}" for i in range(app_keys)], "lib": [f"lib/f{i}" for i in range(lib_keys)]}
    weights = {p: [1 / (i + 1) ** zipf for i in range(len(k))] for p, k in keys.items()}
    units = {}

    def sample(product, n):
        return set(rng.choices(keys[product], weights[product], k=n))

    for i in range(web):
        units[f"web/{i}"] = (sample("app", reads), 0.8)
    for i in range(db):
        units[f"db/{i}"] = (sample("lib", reads), 1.0)  # 2.9 s on three workers
    for i in range(journeys):
        units[f"journey/{i}"] = (set(rng.sample(keys["lib"], int(lib_keys * journey_share))), 12.0)
    return Program(keys, weights, units)


def makespan(seconds, cores):
    """Longest first onto the least-loaded core."""
    loads = [0.0] * max(1, cores)
    for s in sorted(seconds, reverse=True):
        loads[loads.index(min(loads))] += s
    return max(loads) if seconds else 0.0


def simulate(prog, *, per_hour, hours, writes=3, hot=0.05, preproved=True, interaction=0.02,
             seed=2, lag=(300, 3600), cores=1):
    """Stream changes at `per_hour` for `hours` through one merge runner.

    lag: how long before a change is ready its PR's own run happened (seconds, uniform),
    so its passes were taken on an older main.
    cores: units the one merge runner runs side by side (a snapshot's units are independent).
    """
    rng = random.Random(seed)
    base = {k: "v0" for p in prog.keys.values() for k in p}
    f = Frontier(base)
    for unit, (keys, seconds) in prog.units.items():
        f.record(unit, keys, seconds)  # main is proven before the stream starts
    n = int(per_hour * hours)
    arrivals = sorted(rng.uniform(0, hours * 3600) for _ in range(n))
    events = [(t, i, "arrive", i) for i, t in enumerate(arrivals)]
    if preproved:
        events += [(max(0.0, t - rng.uniform(*lag)), n + i, "prove", i) for i, t in enumerate(arrivals)]
    heapq.heapify(events)
    seq = 2 * n
    planned, proofs = {}, {}
    arrived, landed_at = {}, {}
    stats = dict(refused=0, sent_back=0, runs=0, overlap_runs=0, runner_seconds=0.0,
                 author_seconds=0.0, wall=0.0, cones=[], overlap_counts=[])
    busy = False
    reserved = set()  # the cone of the snapshot being proved
    dirty = []  # changes whose component still needs runs, oldest first

    def writes_for(product):
        k = prog.keys[product]
        out = set()
        while len(out) < writes:
            out.add(rng.choices(k, prog.weights[product])[0] if rng.random() < hot else rng.choice(k))
        return out

    def component(cid):
        seen, todo = {cid}, [cid]
        while todo:
            c = todo.pop()
            for unit in f.cone(c):
                for other in f.touching(unit):
                    if other not in seen:
                        seen.add(other)
                        todo.append(other)
        order = {c: i for i, c in enumerate(f.changes)}
        return sorted(seen, key=order.__getitem__)

    def land(group, now):
        f.land(group)
        for c in group:
            landed_at[c] = now
            if c in dirty:
                dirty.remove(c)

    def try_land(cid, now):
        """A change lands alone when its own passes still prove main plus it."""
        if cid not in f.changes:
            return
        # While a snapshot is being proved, a change that touches its units waits for it,
        # or its landing would leave the snapshot's passes proving an old main.
        if not (f.cone(cid) & reserved) and not f.needs([cid]):
            land([cid], now)
        elif cid not in dirty:
            dirty.append(cid)

    def schedule(now):
        """Prove one snapshot: every waiting change in the oldest one's component, together, once."""
        nonlocal busy, seq
        while dirty and not busy:
            cid = dirty[0]
            if cid not in f.changes:
                dirty.pop(0)
                continue
            group = [c for c in component(cid) if c in dirty]
            need = f.needs(group)
            if not need:
                land(group, now)
                continue
            members = set(group)
            certs = [f.snapshot(u, prog.units[u][0], prog.units[u][1], members) for u in need]
            seconds = sum(prog.units[u][1] for u in need)
            wall = makespan([prog.units[u][1] for u in need], cores)
            overlaps = [u for u in need if len(set(f.touching(u)) & members) > 1]
            stats["runs"] += len(need)
            stats["overlap_runs"] += len(overlaps)
            stats["runner_seconds"] += seconds
            busy = True
            reserved.clear()
            reserved.update(set().union(*(f.cone(c) for c in group)))
            stats["wall"] += wall
            heapq.heappush(events, (now + wall, seq, "done", (group, certs, overlaps)))
            seq += 1

    now = 0.0
    while events:
        now, _, kind, ref = heapq.heappop(events)
        if kind == "prove":
            # The PR's own run: main as it is now, plus only this change.
            cid = ref
            product = "app" if rng.random() < 0.5 else "lib"
            planned[cid] = {k: f"c{cid}" for k in writes_for(product)}
            touched = set().union(*(f.readers.get(k, set()) for k in planned[cid]))
            proofs[cid] = [
                Certificate(u, {k: planned[cid].get(k, f.base.get(k)) for k in prog.units[u][0]},
                            prog.units[u][1])
                for u in touched
            ]
            stats["author_seconds"] += sum(prog.units[u][1] for u in touched)
        elif kind == "arrive":
            cid = ref
            if cid not in planned:
                product = "app" if rng.random() < 0.5 else "lib"
                planned[cid] = {k: f"c{cid}" for k in writes_for(product)}
            if f.admit(cid, planned[cid], dict(title=f"change {cid}")):
                stats["refused"] += 1
                continue
            arrived[cid] = now
            stats["cones"].append(len(f.cone(cid)))
            for cert in proofs.pop(cid, ()):
                f.record(cert.unit, cert=cert)
            stats["overlap_counts"].append(len(f.overlaps(component(cid))))
            try_land(cid, now)
        else:
            busy = False
            reserved.clear()
            group, certs, overlaps = ref
            live = [c for c in group if c in f.changes]
            # Each change beyond the first that shares a unit with the others may break it.
            sharing = {c for u in overlaps for c in f.touching(u) if c in live}
            failing = None
            if len(sharing) > 1 and rng.random() < 1 - (1 - interaction) ** (len(sharing) - 1):
                failing = rng.choice(overlaps)
            # Every unit that passed keeps its certificate, failed run or not: it is keyed by
            # content, so after the culprit leaves, only units that read the culprit's inputs rerun.
            for cert in certs:
                if cert.unit != failing:
                    f.record(cert.unit, cert=cert)
            if failing is not None:
                # blame.py names the changes the failing unit read; the newest of them goes back.
                culprit = [c for c in f.touching(failing) if c in live][-1]
                f.eject(culprit)
                if culprit in dirty:
                    dirty.remove(culprit)
                stats["sent_back"] += 1
                live = [c for c in live if c != culprit]
            if live and not f.needs(live):
                land(live, now)
            for c in live:
                try_land(c, now)
        schedule(now)
    waits = [landed_at[c] - arrived[c] for c in landed_at]
    total = hours * 3600
    return dict(
        arrived=len(arrived),
        landed=len(landed_at),
        refused=stats["refused"],
        sent_back=stats["sent_back"],
        median=statistics.median(waits) if waits else None,
        p90=statistics.quantiles(waits, n=10)[-1] if len(waits) >= 10 else None,
        instant=sum(1 for w in waits if w == 0) / len(waits) if waits else 0,
        runner_busy=stats["wall"] / total,
        runner_seconds_per_change=stats["runner_seconds"] / max(1, len(arrived)),
        author_seconds_per_change=stats["author_seconds"] / max(1, len(arrived)),
        overlap_runs=stats["overlap_runs"],
        unit_runs=stats["runs"],
        median_cone=statistics.median(stats["cones"]) if stats["cones"] else 0,
        finished=now,
    )
