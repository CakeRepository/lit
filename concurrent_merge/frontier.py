"""Compositional passes over a program map: land a change on its own test passes.

The program map. For every input on main (a file, or one function of a file
recorded with symbols, keyed "path#function"), lit knows its content digest
and which test units read it on their last passing run. Every admitted change
adds the inputs it writes. Two indexes answer every question in this module:
input -> the one admitted change that writes it, and input -> the units that
read it. Nothing is ever compared change against change.

A pass is a certificate. A unit's passing run records the digest of every
input it read. The certificate holds on any tree that agrees with it on those
inputs, not just the tree it ran on: a deterministic unit that reads the same
content takes the same path, reads nothing else and gives the same result.
lit already reuses certificates across commits of main (unit_store.py); here
they are reused across changes.

So passes compose. A unit that read inputs of one change only sees, on the
combined tree, exactly what that change's own run saw, so that run proves it.
Only a unit that read inputs written by two or more changes saw a tree no run
has seen. Those overlaps are the only units that must run again, once, on the
combined tree. Two changes that write the same input do not compose: the later
one is refused, with a hand-off an agent can act on (conflict_packet).

Changes linked by an overlapping unit form a component (union-find over the
units the overlaps share). A component lands when every unit reading anything
its members wrote holds a certificate for main plus that component. Landing
work grows with a change's own cone (the units reading what it wrote) and the
overlaps it really has, never with how many other changes are in flight.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path

KEEP = 16  # certificates kept per unit


@dataclass
class Certificate:
    unit: str
    inputs: dict  # input key -> digest the passing run read
    seconds: float = 0.0


@dataclass
class Frontier:
    base: dict  # input key -> digest on main
    changes: dict = field(default_factory=dict)  # id -> {key: digest or None}
    meta: dict = field(default_factory=dict)  # id -> what an agent needs (title, intent, diffs per key)
    certs: dict = field(default_factory=dict)  # unit -> [Certificate], newest last
    reads: dict = field(default_factory=dict)  # unit -> keys its newest passing run read
    readers: dict = field(default_factory=dict)  # key -> units that read it (the program map)
    writer: dict = field(default_factory=dict)  # key -> admitted change writing it
    landed: list = field(default_factory=list)  # in landing order
    done: set = field(default_factory=set)  # the same, for lookups
    refused: dict = field(default_factory=dict)  # id -> conflict packet

    # Content

    def content(self, key, members=None):
        """The digest of `key` on main plus `members` (default: every admitted change)."""
        cid = self.writer.get(key)
        if cid is not None and (members is None or cid in members):
            return self.changes[cid][key]
        return self.base.get(key)

    # Admission

    def admit(self, cid, writes, meta=None):
        """Add a change, unless another admitted change writes one of the same inputs."""
        if cid in self.changes or cid in self.done:
            raise ValueError("Change already admitted")
        clash = sorted({self.writer[k] for k in writes if k in self.writer})
        if clash:
            self.refused[cid] = conflict_packet(self, cid, writes, meta or {})
            return self.refused[cid]
        self.changes[cid] = dict(writes)
        self.meta[cid] = meta or {}
        for key in writes:
            self.writer[key] = cid
        return None

    def eject(self, cid):
        for key in self.changes.pop(cid, {}):
            self.writer.pop(key, None)

    # Certificates

    def snapshot(self, unit, keys, seconds=0.0, members=None):
        """The certificate a run of `unit` that reads `keys` would earn on main plus `members`, taken when it starts."""
        return Certificate(unit, {k: self.content(k, members) for k in keys}, seconds)

    def record(self, unit, keys=None, seconds=0.0, members=None, cert=None):
        """A passing run: `cert` from snapshot(), or one taken now on main plus `members` (default: everything).

        Without `keys`, the run read what the unit's last run read."""
        if cert is None:
            keys = self.reads[unit] if keys is None else keys
            cert = self.snapshot(unit, keys, seconds, members)
        keys = list(cert.inputs)
        kept = self.certs.setdefault(unit, [])
        kept.append(cert)
        # A unit read by several in-flight changes is an overlap and needs a combined run anyway,
        # so only the newest few certificates can still prove anything.
        del kept[:-KEEP]
        for key in self.reads.get(unit, ()):
            self.readers.get(key, set()).discard(unit)
        self.reads[unit] = set(keys)
        for key in keys:
            self.readers.setdefault(key, set()).add(unit)
        return cert

    def proves(self, unit, members=None):
        for cert in reversed(self.certs.get(unit, ())):
            if all(self.content(k, members) == d for k, d in cert.inputs.items()):
                return cert
        return None

    # The map's questions, all by index

    def cone(self, cid):
        """Units whose newest run read something the change writes."""
        return set().union(*(self.readers.get(k, set()) for k in self.changes[cid])) if self.changes[cid] else set()

    def touching(self, unit):
        """Admitted changes writing something the unit read."""
        return sorted({self.writer[k] for k in self.reads.get(unit, ()) if k in self.writer})

    def components(self, cids=None):
        """Changes grouped so that no unit reads inputs of two groups."""
        cids = list(self.changes) if cids is None else list(cids)
        parent = {c: c for c in cids}

        def find(c):
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c

        for cid in cids:
            for unit in self.cone(cid):
                for other in self.touching(unit):
                    if other in parent:
                        parent[find(other)] = find(cid)
        groups = {}
        for cid in cids:
            groups.setdefault(find(cid), []).append(cid)
        return [sorted(g, key=list(self.changes).index) for g in groups.values()]

    def needs(self, cids):
        """Units that must run before `cids` land together: their cone minus what certificates prove."""
        members = set(cids)
        units = set().union(*(self.cone(c) for c in cids)) if cids else set()
        return sorted(u for u in units if not self.proves(u, members))

    def overlaps(self, cids=None):
        """Units reading inputs of two or more admitted changes (the only new work a merge creates)."""
        cids = set(self.changes if cids is None else cids)
        units = set().union(*(self.cone(c) for c in cids)) if cids else set()
        return sorted(u for u in units if len(set(self.touching(u)) & cids) > 1)

    def landable(self):
        """Components whose combined tree every unit they touch already proves."""
        return [g for g in self.components() if not self.needs(g)]

    def land(self, cids):
        """Move changes into main. Certificates stay valid: they are keyed by content."""
        for cid in cids:
            writes = self.changes.pop(cid)
            for key, digest in writes.items():
                self.writer.pop(key, None)
                if digest is None:
                    self.base.pop(key, None)
                else:
                    self.base[key] = digest
            self.landed.append(cid)
            self.done.add(cid)


def conflict_packet(frontier, cid, writes, meta):
    """What an agent needs to fix a same-input conflict: both changes, the inputs, both sides, the tests."""
    keys = sorted(k for k in writes if k in frontier.writer)
    others = sorted({frontier.writer[k] for k in keys})
    sides = []
    for other in others:
        mine = [k for k in keys if frontier.writer[k] == other]
        sides.append(dict(
            change=other,
            title=frontier.meta.get(other, {}).get("title"),
            intent=frontier.meta.get(other, {}).get("intent"),
            inputs=mine,
            diffs={k: frontier.meta.get(other, {}).get("diffs", {}).get(k) for k in mine},
        ))
    tests = sorted(set().union(*(frontier.readers.get(k, set()) for k in keys))) if keys else []
    return dict(
        kind="lit-conflict",
        change=cid,
        title=meta.get("title"),
        intent=meta.get("intent"),
        inputs=keys,
        diffs={k: meta.get("diffs", {}).get(k) for k in keys},
        landed_first=sides,
        tests=tests,
        task=(
            f"Change {cid} and {', '.join(map(str, others))} both change {', '.join(keys)}. "
            f"{', '.join(map(str, others))} is ahead and stays. Rewrite {cid} on top of it so both "
            "intents hold in each input listed, keep every other input as it is, and make the "
            "listed tests pass. Push the result to the same branch; lit admits it again."
        ),
    )


def from_store(store, job):
    """The program map of one job from lit's unit store (cached-units records).

    A file recorded with symbols becomes one input per function the unit ran
    ("path#name", "" for its top-level code), digested by that function's own
    code; any other file the unit read is one input, digested by its blob id.
    Each unit's newest passing footprint becomes its certificate on main.
    """
    directory = Path(store) / job
    tables, base, f = {}, {}, None
    certs = []
    for path in sorted((directory / "units").glob("*.json")):
        try:
            bundle = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            continue
        if bundle.get("kind") != "lit-unit-footprints":
            continue
        for name, unit in bundle["units"].items():
            ran = unit.get("symbols") or {}
            inputs = {}
            for file in unit["tracked"]:
                oid = bundle["tracked"][file][1]
                if file in ran and oid not in tables:
                    try:
                        tables[oid] = json.loads((directory / "symbols" / (oid + ".json")).read_text(encoding="utf8"))
                    except (OSError, ValueError):
                        tables[oid] = None
                table = tables.get(oid) if file in ran else None
                if table is None:
                    inputs[file] = oid
                else:
                    for symbol in sorted(set(ran[file]) | {""}):
                        inputs[f"{file}#{symbol}"] = table.get(symbol)
            base.update(inputs)
            certs.append(Certificate(name, inputs, unit.get("seconds") or 0.0))
    f = Frontier(base)
    for cert in certs:
        f.record(cert.unit, cert=cert)
    return f
