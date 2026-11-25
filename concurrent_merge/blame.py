"""Name the change that broke a combined run, from what the failing tests read.

A group of changes is tested once, as one tree. When test files fail, each
failing file's footprint (every repository path it read or listed, recorded by
cached-units) says which changes it could have seen: a change is a suspect for
a failing file when it added, changed or deleted a path the file read, or
changed the entries of a directory the file listed. A change no failing file
could see cannot have caused the failure, so:

- one suspect: that change goes back to its author; the rest of the group is
  retested without it (only the files it could have touched need to run).
- several suspects: only they are split, by bisecting; the others stay.
- no suspect: no change in the group explains it, as far as the footprints
  see. Rerun the failing files on the base alone: if they fail there too, the
  base is broken and every change waits; if they pass, something the footprints
  missed (an installed dependency, say) changed, so bisect the whole group.

Listings are counted conservatively: any change under a listed directory is a
suspect, though only additions and deletions change what a listing returns.
"""

import json
from pathlib import Path
import subprocess

from .engine import git


def changed_paths(workdir, base, head):
    """Paths a change adds, modifies or deletes relative to base (renames count both ends)."""
    out = subprocess.run(
        [*git(workdir), "diff", "--no-renames", "--name-only", "-z", base, head],
        cwd=workdir, capture_output=True, check=True,
    ).stdout
    return {p.decode("utf8", "surrogateescape") for p in out.split(b"\0") if p}


def parents(path):
    parts = path.split("/")[:-1]
    return {"/".join(parts[:i]) for i in range(len(parts) + 1)}


def suspects(failed, changes):
    """{failing unit: [change ids it could have seen]}.

    failed: {unit: a unit footprint from cached-units: "tracked" (paths read),
    "listings" (directories listed), "absent" (paths probed and not found) and
    "incomplete" (reasons the trace missed reads)}. changes: {id: set of paths}.
    A unit whose trace was incomplete could have seen any change.
    """
    out = {}
    for unit, reads in failed.items():
        if reads.get("incomplete"):
            out[unit] = sorted(changes)
            continue
        seen = set(reads.get("tracked", ())) | set(reads.get("absent", ()))
        listed = set(reads.get("listings", ()))
        out[unit] = sorted(
            cid for cid, paths in changes.items()
            if any(p in seen or parents(p) & listed for p in paths)
        )
    return out


def verdict(failed, changes):
    """What to do about a failed group: action eject, bisect or rerun-on-base, with the ids."""
    blamed = suspects(failed, changes)
    named = sorted({c for hits in blamed.values() for c in hits})
    unexplained = sorted(u for u, hits in blamed.items() if not hits)
    if unexplained:
        # Some failing file saw none of the changes: decide on the base first.
        return dict(action="rerun-on-base", rerun=unexplained, eject=[],
                    bisect=sorted(changes), keep=[], suspects=blamed)
    if len(named) == 1:
        return dict(action="eject", rerun=[], eject=named, bisect=[],
                    keep=sorted(set(changes) - set(named)), suspects=blamed)
    return dict(action="bisect", rerun=[], eject=[], bisect=named,
                keep=sorted(set(changes) - set(named)), suspects=blamed)


def failed_reads(receipts):
    """Merge the failed units' reads from cached-units receipts."""
    merged = {}
    for receipt in receipts:
        data = json.loads(Path(receipt).read_text(encoding="utf8"))
        merged.update(data.get("failed_reads") or {})
    return merged
