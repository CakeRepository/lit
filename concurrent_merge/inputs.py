"""Verify input commit identities and order stacked PR dependencies."""

import re

from .parallel import commit_parents, ordered_map, worker_count


def valid_agent(name):
    """An agent name becomes a commit trailer and a vote key: one printable line."""
    return (
        type(name) is str
        and bool(name.strip())
        and bool(re.fullmatch(r"[^\x00-\x1f\x7f]{1,100}", name))
    )


def verified_changes(repo, snapshot, jobs=None):
    jobs = worker_count(jobs)
    if repo.out("rev-parse", "--is-bare-repository") != "true":
        raise ValueError("Use a dedicated bare clone")
    base = snapshot["base"]

    changes = {}
    by_head = {}
    for row in snapshot["changes"]:
        p = {k: row[k] for k in ("number", "head", "author")}
        if (
            type(p["number"]) is not int
            or p["number"] <= 0
            or p["number"] in changes
            or not valid_agent(p["author"])
        ):
            raise ValueError("Invalid or duplicate PR in snapshot")
        if (agent := row.get("agent")) is not None:
            # Several agents may share one login; the agent is who gets the change back.
            if not valid_agent(agent):
                raise ValueError(f"Invalid agent for PR #{p['number']} in snapshot")
            if agent.strip() != p["author"]:
                p["login"], p["author"] = p["author"], agent.strip()
        by_head.setdefault(p["head"], set()).add(p["number"])
        changes[p["number"]] = p
    oids = by_head.keys() | {base}
    if not all(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) for oid in oids):
        raise ValueError("Snapshot must contain full commit IDs")
    parents = commit_parents(repo, oids)
    if parents.keys() != oids:
        raise ValueError("Snapshot must contain locally available commit objects")

    def dependencies(p):
        if parents[p["head"]] == [base] or p["head"] == base:
            return []
        ancestors = repo.out("rev-list", p["head"], "^" + base).splitlines()[1:]
        return sorted({n for oid in ancestors for n in by_head.get(oid, ())})

    rows = list(changes.values())
    for p, deps in zip(rows, ordered_map(dependencies, rows, jobs)):
        p["requires"] = deps
    ordered = []
    while changes:
        ready = [p for p in changes.values() if not changes.keys() & set(p["requires"])]
        if not ready:
            raise ValueError("Cyclic PR dependencies")
        ordered.extend(ready)
        for p in ready:
            del changes[p["number"]]
    return ordered
