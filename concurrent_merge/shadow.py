"""Shadow decisions: what Frontier would have chosen for a batch, next to what ran.

Step 1 of the plan in docs/compositional-integration.md. Nothing here changes
what lit runs, tests or lands. It builds Frontier's program map from lit's
unit store (frontier.from_store), sets main to the batch's real base commit,
admits each PR of the snapshot with the inputs it writes, and records the
components Frontier would have formed, the units each would have had to run
(`needs`), and the overlaps. Beside a `run` receipt it also records what the
full suite actually passed, so the two can be compared over many batches
before Frontier is allowed to decide anything.

Inputs are the store's: a whole file (its blob id), or one function of a file
recorded with symbols ("path#name", digested by that function's own code).
A change writes an input when the input's digest at the PR head differs from
its digest where the PR forked from main. If main has changed the same input
since then, the merged content is neither side's, so the input gets a digest
no certificate holds and every unit reading it counts as unproven. A path that
no stored unit read is still recorded as one input, so two PRs editing it are
reported as a write-write conflict.

The runner counts as well (see frontier.py): a certificate holds only while
the environment its run saw and the host files it read are what this runner
holds now. A stored unit whose runner inputs differ is reported as stale.

What the shadow cannot see, and so never claims:
- units the store has no passing footprint for (a new test, an untraced job).
  Given the same `--discover` patterns as `cached-units`, it lists the test
  files that have none as `unrecorded`: a file a PR adds belongs to that PR's
  component, and one already on main leaves every component unproven. Without
  the patterns they are invisible;
- paths a unit listed or probed but did not read (directory listings, absent
  files);
- anything about the runner that the record did not name (an ignored
  environment variable, a device, the kernel).
So `needs` here is a lower bound on what a decision would have to run, and the
comparison with the real run is how the gap gets measured.
"""

import os
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from . import js_symbols
from .frontier import from_store, runner_inputs
from .symbols import digests

UNREADABLE = "unreadable:"  # prefix of a digest no certificate holds
MERGED = "merged:"


class Contents:
    """Digests of program-map inputs at commits of one repository, cached."""

    def __init__(self, repo):
        self.repo = repo
        self.trees = {}
        self.tables = {}

    def tree(self, commit):
        if commit not in self.trees:
            data = self.repo.run("ls-tree", "-r", "-z", "--full-tree", commit).stdout
            entries = {}
            for line in data.split(b"\0"):
                if not line:
                    continue
                meta, _, path = line.partition(b"\t")
                entries[os.fsdecode(path)] = meta.split()[2].decode("ascii")
            self.trees[commit] = entries
        return self.trees[commit]

    def symbols(self, path, oid):
        if oid not in self.tables:
            table = js_symbols.table(self.repo.blobs([oid])[0], PurePosixPath(path).suffix.lower())
            self.tables[oid] = digests(table) if table else None
        return self.tables[oid]

    def digest(self, commit, key):
        """The digest `key` has at `commit`, as from_store records it; None when absent."""
        path, hashed, name = key.partition("#")
        oid = self.tree(commit).get(path)
        if not hashed or oid is None:
            return oid
        table = self.symbols(path, oid)
        if table is None:
            return UNREADABLE + oid
        return table.get(name)


def by_file(keys):
    out = {}
    for key in keys:
        out.setdefault(key.partition("#")[0], []).append(key)
    return out


def program(repo, store, job, base, contents=None, env=None, proofs=None):
    """Frontier's map from the store, with main set to `base`'s real content on this runner.

    Runner inputs (the environment, files outside the checkout) take the values this
    runner holds now, or those of `env` for the environment, so a certificate recorded
    in another environment proves nothing.
    """
    contents = contents or Contents(repo)
    f = from_store(store, job, proofs=proofs)
    runner = runner_inputs(f.readers, env)
    f.base = {}
    for key in f.readers:
        digest = runner[key] if key in runner else contents.digest(base, key)
        if digest is not None:
            f.base[key] = digest
    return f


def writes(repo, contents, files, base, head):
    """The inputs a PR at `head` writes on main at `base`: {key: digest, or None for a delete}."""
    fork = repo.out("merge-base", base, head)
    out = {}
    for path in sorted(repo.paths(fork, head)):
        for key in files.get(path, [path]):
            before, after = contents.digest(fork, key), contents.digest(head, key)
            if before == after:
                continue
            if contents.digest(base, key) != before:
                # Main and the change both changed this input since they forked.
                after = f"{MERGED}{base}:{head}:{key}"
            out[key] = after
    return out


def discovered(paths, patterns, strip):
    """Unit names for the paths `cached-units --discover` would find, as it names them."""
    return {
        path[len(strip):]
        for path in paths
        if path.startswith(strip) and any(fnmatchcase(path, pattern) for pattern in patterns)
    }


def decide(f, changes, added=None, unrecorded=()):
    """Admit `changes` ({id: writes}) into `f` and say what Frontier would do with them.

    added: {id: unit names the change adds that have no certificate}; those units belong to
    its component and must run. unrecorded: units on main with no certificate, which leave
    every component unproven.
    """
    added = added or {}
    conflicts = {}
    for cid, w in changes.items():
        packet = f.admit(cid, w, dict(title=f"PR #{cid}"))
        if packet:
            conflicts[cid] = dict(
                inputs=packet["inputs"], held_by=[s["change"] for s in packet["landed_first"]]
            )
    components = []
    for group in f.components():
        members = set(group)
        cone = set().union(*(f.cone(c) for c in group))
        proven = {u: f.proves(u, members) for u in cone}
        components.append(dict(
            members=group, cone=len(cone), needs=f.needs(group), overlaps=f.overlaps(group),
            unrecorded=sorted(set().union(*(added.get(c, ()) for c in group))),
            # Units only a signed proof (a PR's own CI run) proves on main plus this group.
            by_proof=sorted(u for u, c in proven.items() if c is not None and c.source == "proof"),
        ))
    return dict(
        units=len(f.certs),
        stale=sorted(u for u in f.certs if not f.proves(u, set())),
        components=components,
        conflicts=conflicts,
        unrecorded=sorted(unrecorded),
        unrun=[] if unrecorded else sorted(
            c for comp in components if not comp["needs"] and not comp["unrecorded"] for c in comp["members"]
        ),
    )


def shadow(repo, snapshot, store, job, receipt=None, attempts=None, env=None, proofs=None,
           discover=None, strip=""):
    """The shadow decision for one snapshot; with a run's receipt, what actually passed too.

    `proofs`: a store `concurrent-merge proof take` filled with signed records, such as
    each PR's own CI run on its head. Only take puts records there, and only ones an
    allowed signer signed for this repository, an expected commit and the expected image.
    """
    contents = Contents(repo)
    base = snapshot["base"]
    f = program(repo, store, job, base, contents, env, proofs)
    files = by_file(f.readers)
    changes = {p["number"]: writes(repo, contents, files, base, p["head"]) for p in snapshot["changes"]}
    added, unrecorded = {}, set()
    if discover:
        on_main = discovered(contents.tree(base), discover, strip)
        unrecorded = {u for u in on_main if u not in f.certs}
        for p in snapshot["changes"]:
            new = discovered(contents.tree(p["head"]), discover, strip) - on_main
            added[p["number"]] = sorted(u for u in new if u not in f.certs)
    out = dict(
        kind="lit-shadow-decision",
        base=base,
        job=job,
        proofs=proofs is not None,
        discover=list(discover or ()),
        writes={n: sorted(w) for n, w in changes.items()},
        decision=decide(f, changes, added, unrecorded),
    )
    if receipt is not None:
        passed = set(receipt.get("tested", ()))
        ran = [a for a in attempts or () if a.get("result") in ("pass", "fail", "known-failure")]
        out["outcome"] = dict(
            passed=sorted(passed),
            failed=sorted(set(changes) - passed),
            suite_runs=len(ran),
            component_runs=sum(1 for c in out["decision"]["components"] if c["needs"]),
            # Changes Frontier would have landed without running anything that the full suite
            # did not pass: the cases that show its evidence is not yet enough.
            unrun_but_failed=sorted(set(out["decision"]["unrun"]) - passed),
        )
    return out
