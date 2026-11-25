"""A model of a merge flow, for choosing how lit should run one.

Changes arrive ready to land. The flow keeps an ordered pipeline of groups,
each tested on main plus every group ahead of it, at most `slots` runs at once.
A run takes `seconds`. A group lands when it passed and every group ahead of it
has landed. When a group fails:

- `restart` (a merge queue of single changes): the failing change leaves, and
  every group behind it is tested again, because its tree held it.
- `bisect`: a group of several changes splits into two halves in its place,
  both tested at once (the right half on top of the left), until the failing
  change is alone and leaves. Groups behind are tested again.
- `attribute` (lit): the failing tests' footprints name the change they read
  (blame.py), so that change leaves at once and the rest of its group is tested
  again in its place. Groups behind are tested again.

A change that fails is sent back to its author and arrives again when its
`retry` time says (or never). The model answers one question: for a trace of
real arrivals and failures, how long does each change wait from arriving to
landing under each way of running the flow.
"""

from dataclasses import dataclass, field
import heapq
import statistics


@dataclass
class Change:
    id: str
    arrive: float
    fails: int = 0  # how many of its next attempts fail (a fixed change passes)
    retry: list = field(default_factory=list)  # arrival times after each failure


@dataclass
class Group:
    members: list
    start: float = 0.0
    end: float = 0.0
    passed: bool = False
    epoch: int = 0


def simulate(changes, *, seconds, slots, max_group=1, on_failure="restart", wait=0.0):
    """{change id: seconds from first arrival to landing, or None}, plus totals.

    wait: how long the flow may hold a new group open for more changes when a
    slot is free (0: start at once with whatever is waiting).
    """
    if on_failure not in ("restart", "bisect", "attribute"):
        raise ValueError("on_failure is restart, bisect or attribute")
    if on_failure == "restart" and max_group != 1:
        raise ValueError("restart drops a whole group, so it only fits groups of one change")
    by_id = {c.id: c for c in changes}
    left = {c.id: c.fails for c in changes}
    retries = {c.id: list(c.retry) for c in changes}
    first = {c.id: c.arrive for c in changes}
    events = [(c.arrive, 0, "arrive", c.id) for c in changes]
    heapq.heapify(events)
    waiting, pipeline, landed = [], [], {}
    runs = 0
    now = 0.0
    seq = 1

    def push(t, kind, ref):
        nonlocal seq
        heapq.heappush(events, (t, seq, kind, ref))
        seq += 1

    def start(group, t):
        nonlocal runs
        group.start, group.end, group.passed = t, t + seconds, False
        group.epoch += 1
        runs += 1
        push(group.end, "end", (id(group), group.epoch))

    def running():
        return sum(1 for g in pipeline if not g.passed)

    def fill(t):
        while waiting and running() < slots:
            if max_group > 1 and wait and len(waiting) < max_group:
                oldest = min(by_id[i].arrive for i in waiting)
                if t < oldest + wait:
                    push(oldest + wait, "tick", None)
                    return
            group = Group(waiting[:max_group])
            del waiting[:max_group]
            pipeline.append(group)
            start(group, t)

    def land(t):
        while pipeline and pipeline[0].passed:
            for m in pipeline.pop(0).members:
                landed[m] = t

    while events:
        now, _, kind, ref = heapq.heappop(events)
        if kind == "arrive":
            waiting.append(ref)
        elif kind == "end":
            group = next((g for g in pipeline if id(g) == ref[0] and g.epoch == ref[1]), None)
            if group is None:
                pass
            else:
                bad = [m for m in group.members if left[m] > 0]
                at = pipeline.index(group)
                if not bad:
                    group.passed = True
                    land(now)
                else:
                    behind = pipeline[at + 1:]
                    if len(group.members) == 1 or on_failure == "attribute":
                        for m in bad:
                            left[m] -= 1
                            if retries[m]:
                                push(max(now, retries[m].pop(0)), "arrive", m)
                        rest = [m for m in group.members if m not in bad]
                        if rest:
                            group.members = rest
                            start(group, now)
                        else:
                            pipeline.remove(group)
                    else:
                        mid = len(group.members) // 2
                        halves = [Group(group.members[:mid]), Group(group.members[mid:])]
                        pipeline[at:at + 1] = halves
                        for h in halves:
                            start(h, now)
                    for g in behind:
                        start(g, now)
        if not events or events[0][0] > now:
            fill(now)  # once everything that happened at this moment is known
    waits = {i: (landed[i] - first[i]) if i in landed else None for i in by_id}
    done = [w for w in waits.values() if w is not None]
    return dict(
        waits=waits,
        runs=runs,
        landed=len(done),
        median=statistics.median(done) if done else None,
        mean=statistics.mean(done) if done else None,
        worst=max(done) if done else None,
        finished=now,
    )
