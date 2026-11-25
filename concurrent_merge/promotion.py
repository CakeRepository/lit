"""Release bridges: promote one immutable main snapshot through stages.

Main integrates continuously (frontier.py). Releases are separate: a stage
such as Lab, Early Release or Production points at one snapshot of main, and
moves only by promotion from the stage before it. Three rules keep the
bridges independent of integration and of each other:

- One artifact. A snapshot is named by its commit id and promoted as it is,
  never rebuilt, so what Production runs is byte for byte what Lab tested.
  Each promotion records where the snapshot came from (provenance).
- Evidence per stage, per snapshot. A stage lists the checks it needs, and
  a check counts only for the snapshot it ran on. Passing Lab says nothing
  about Production's checks, and a pass on another snapshot says nothing at
  all. A stage that needs a person's approval waits for one, for that
  snapshot.
- A failure holds one bridge. A failed check holds that snapshot at that
  stage. Main keeps integrating, the stages behind keep promoting newer
  snapshots, and the stages ahead keep what they have.

Stages move by compare-and-swap, like main: promote() and rollback() take
the snapshot the caller saw at the stage and change nothing if it moved.
Rolling back points a stage at the snapshot it held before; it needs no new
evidence because that snapshot already had it.

This is an in-memory model, with no deploy or Git side yet.
"""

from dataclasses import dataclass, field


@dataclass
class Stage:
    name: str
    checks: tuple = ()  # check names a snapshot must pass here
    approval: bool = False  # whether a person must approve each promotion into it


@dataclass
class Bridges:
    stages: list  # [Stage], in promotion order; stage 0 is fed straight from main
    main: list = field(default_factory=list)  # snapshots of main, oldest first
    at: dict = field(default_factory=dict)  # stage name -> snapshot it points at
    history: dict = field(default_factory=dict)  # stage name -> snapshots it pointed at, oldest first
    evidence: dict = field(default_factory=dict)  # (stage, snapshot) -> {check: passed}
    approved: dict = field(default_factory=dict)  # (stage, snapshot) -> who approved
    provenance: list = field(default_factory=list)  # [dict], one per promotion or rollback

    def __post_init__(self):
        names = [s.name for s in self.stages]
        if not names or len(set(names)) != len(names) or "main" in names:
            raise ValueError("Stages need distinct names other than main")
        self.by_name = {s.name: s for s in self.stages}

    # What main and each stage hold

    def integrate(self, snapshot):
        """Main moved to `snapshot` (a landing). No stage moves."""
        if snapshot in self.main:
            raise ValueError("Snapshot already on main")
        self.main.append(snapshot)

    def source(self, stage):
        """Where a stage promotes from: main for the first, else the stage before it."""
        i = self.stages.index(self.by_name[stage])
        return "main" if i == 0 else self.stages[i - 1].name

    def reached(self, name):
        """Snapshots that got to `name` (main, or a stage) and were not rolled back from it."""
        return self.main if name == "main" else self.history.get(name, [])

    def holds(self, name):
        """What `name` (a stage, or main) points at now."""
        if name == "main":
            return self.main[-1] if self.main else None
        return self.at.get(name)

    # Evidence

    def record(self, stage, snapshot, check, passed):
        """A check ran on `snapshot` at `stage`. A failure holds that snapshot there."""
        if check not in self.by_name[stage].checks:
            raise ValueError(f"{stage} does not use check {check}")
        self.evidence.setdefault((stage, snapshot), {})[check] = bool(passed)

    def approve(self, stage, snapshot, who):
        if not self.by_name[stage].approval:
            raise ValueError(f"{stage} takes no approval")
        self.approved[(stage, snapshot)] = who

    def missing(self, stage, snapshot):
        """What `snapshot` still lacks before it may enter `stage`: failed, unrun, approval."""
        got = self.evidence.get((stage, snapshot), {})
        failed = sorted(c for c in self.by_name[stage].checks if got.get(c) is False)
        unrun = sorted(c for c in self.by_name[stage].checks if c not in got)
        need = dict(failed=failed, unrun=unrun)
        if self.by_name[stage].approval and (stage, snapshot) not in self.approved:
            need["approval"] = True
        return {k: v for k, v in need.items() if v}

    # Moving a stage

    def promote(self, stage, snapshot, seen):
        """Point `stage` at `snapshot`, or say why not and change nothing.

        `seen` is what the caller saw at the stage (None if empty). The snapshot must
        have reached the stage's source (be on main, or have been promoted into the stage
        before and not rolled back), and must have all of the stage's evidence.
        Returns None, ("moved", now), ("not at source", source) or ("missing", need).
        """
        if self.at.get(stage) != seen:
            return ("moved", self.at.get(stage))
        src = self.source(stage)
        if snapshot not in self.reached(src):
            return ("not at source", src)
        need = self.missing(stage, snapshot)
        if need:
            return ("missing", need)
        self._point(stage, snapshot, src)
        return None

    def rollback(self, stage, seen):
        """Point `stage` back at the snapshot it held before `seen`, or say why not."""
        if self.at.get(stage) != seen:
            return ("moved", self.at.get(stage))
        past = self.history.get(stage, [])
        if len(past) < 2:
            return ("nothing to roll back to",)
        past.pop()
        self.at[stage] = past[-1]
        self.provenance.append(dict(stage=stage, snapshot=past[-1], source=stage, rollback_from=seen))
        return None

    def _point(self, stage, snapshot, src):
        self.at[stage] = snapshot
        self.history.setdefault(stage, []).append(snapshot)
        self.provenance.append(dict(
            stage=stage, snapshot=snapshot, source=src,
            checks=dict(self.evidence.get((stage, snapshot), {})),
            approved_by=self.approved.get((stage, snapshot)),
        ))
