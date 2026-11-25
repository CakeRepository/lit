"""Compare a queue prediction with a later protected landing, without running CI or updating refs."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from concurrent_merge.engine import Repo
from concurrent_merge.inputs import verified_changes
from concurrent_merge.intake import Intake
from concurrent_merge.intake_guard import pull_many


def ancestor(repo, before, after):
    result = repo.run("merge-base", "--is-ancestor", before, after, check=False)
    if result.returncode not in (0, 1):
        raise ValueError("Missing commit in archive")
    return result.returncode == 0


def assemble(repo, base, changes):
    tip = base
    for pr in verified_changes(repo, {"base": base, "changes": changes}):
        if ancestor(repo, pr["head"], base):
            raise ValueError(f"Anchor already contains source #{pr['number']}")
        result = repo.run("merge-tree", "--write-tree", tip, pr["head"], check=False)
        if result.returncode == 1:
            return {"conflict_at": pr["number"], "tree": None}
        if result.returncode:
            raise ValueError("Unable to reproduce native merge")
        # Fix the timestamp so independent replays name the same trial commit.
        tip = repo.commit(
            result.stdout.splitlines()[0].decode("ascii"), tip, pr, timestamp=0
        )
    return {"tip": tip, "tree": repo.out("rev-parse", tip + "^{tree}")}


def project(repo, queue, sources, landed=None):
    if any(sources[k] != queue[k] for k in ("base", "repository", "branch")):
        raise ValueError(
            "Queue and source snapshots must agree on the repository, branch and base"
        )
    changes, base = sources["changes"], queue["base"]
    if not changes:
        raise ValueError("Supply at least one source")
    ids = [base, *(p["head"] for p in changes), *(p["head"] for p in queue["changes"])]
    if not all(
        isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{40}", oid) for oid in ids
    ):
        raise ValueError("Snapshots must identify GitHub commits by full object ID")
    members = {p["number"]: p["head"] for p in changes}
    groups = {g["number"]: g for g in queue["groups"]}
    prefix, seen_source = [], False
    for pr in queue["changes"]:
        if pr["number"] in members:
            if members[pr["number"]] != pr["head"]:
                raise ValueError("Source revision differs between snapshots")
            seen_source = True
        elif seen_source:
            raise ValueError(
                "Interleaved queue entries have no single known insertion boundary"
            )
        else:
            prefix.append(pr)
    # Only an already assigned, preceding native group is available to an early planner.
    # The observed landing is deliberately excluded from anchor and candidate selection.
    anchor = base
    for pr in reversed(prefix):
        group = groups.get(pr["number"])
        if group and group["state"] in {"AWAITING_CHECKS", "MERGEABLE", "LOCKED"}:
            anchor = group["head"]
            if (
                not re.fullmatch(r"[0-9a-f]{40}", anchor)
                or not ancestor(repo, base, anchor)
                or not ancestor(repo, pr["head"], anchor)
            ):
                raise ValueError("Invalid archived queue predecessor")
            if group.get("tree") and group["tree"] != repo.out(
                "rev-parse", anchor + "^{tree}"
            ):
                raise ValueError("Queue tree differs from the archived predecessor")
            break
    result = dict(
        main=base,
        anchor=anchor,
        sources=members,
        preceding_prs_outside_anchor=[
            p["number"] for p in prefix if not ancestor(repo, p["head"], anchor)
        ],
        from_main=assemble(repo, base, changes),
    )
    result["from_queue"] = (
        result["from_main"] if anchor == base else assemble(repo, anchor, changes)
    )
    if landed:
        if not re.fullmatch(r"[0-9a-f]{40}", landed):
            raise ValueError("Supply the full protected landing commit ID")
        tree = repo.out("rev-parse", landed + "^{tree}")
        result["observed"] = dict(
            commit=landed,
            tree=tree,
            anchor_in_landing=ancestor(repo, anchor, landed),
            all_sources_in_landing=all(
                ancestor(repo, p["head"], landed) for p in changes
            ),
            main_tree_matched=result["from_main"]["tree"] == tree,
            queue_tree_matched=result["from_queue"]["tree"] == tree,
        )
    return result


def preview(repo, repository, branch, numbers, collect):
    """Record a candidate before any of its original PRs enter the native queue."""
    if repo.out("rev-parse", "--is-bare-repository") != "true":
        raise ValueError("Use a dedicated bare clone")
    snapshot = collect(repository, branch)
    sources = []
    for n, (pr, _) in pull_many(repository, numbers).items():
        if (
            not pr
            or pr["state"] != "OPEN"
            or pr["baseRefName"] != branch
            or pr["isCrossRepository"]
            or (pr.get("headRepository") or {}).get("nameWithOwner") != repository
            or pr["isInMergeQueue"]
        ):
            raise ValueError(
                f"#{n} must be an open same-repository PR preceding queue admission"
            )
        sources.append(
            dict(
                number=n,
                head=pr["headRefOid"],
                author=(pr["author"] or {}).get("login", "ghost"),
            )
        )
    if any(p["number"] in numbers for p in snapshot["changes"]):
        raise ValueError("A selected PR already entered the queue")
    inputs = dict(
        repository=repository, branch=branch, base=snapshot["base"], changes=sources
    )
    Intake(repository, branch, "", repo).fetch_commits(
        [
            snapshot["base"],
            *(p["head"] for p in sources),
            *(p["head"] for p in snapshot["changes"]),
            *(g["head"] for g in snapshot["groups"]),
        ]
    )
    result = project(repo, snapshot, inputs)
    timestamp = datetime.now(timezone.utc).isoformat()
    if collect(repository, branch) != snapshot:
        raise ValueError("Queue changed while computing the prediction. Collect again.")
    current = pull_many(repository, numbers)
    for source in sources:
        pr, _ = current[source["number"]]
        if (
            not pr
            or pr["state"] != "OPEN"
            or pr["baseRefName"] != branch
            or pr["headRefOid"] != source["head"]
            or pr["isInMergeQueue"]
        ):
            raise ValueError("A source changed or entered the queue during prediction")
    return dict(
        computed_at=timestamp, queue=snapshot, sources=inputs, prediction=result
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        required=True,
        type=Path,
        help="Bare archive with the observed Git objects",
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--queue",
        type=Path,
        help="Archived concurrent-merge collect snapshot",
    )
    selection.add_argument(
        "--observation", type=Path, help="Receipt written by concurrent-merge preview"
    )
    parser.add_argument(
        "--sources",
        type=Path,
        help="Source snapshot from the same observation",
    )
    parser.add_argument(
        "--landed", help="Optional later protected commit to score the prediction"
    )
    args = parser.parse_args()
    if args.observation:
        if args.sources or not args.landed:
            parser.error(
                "--observation takes --landed and reads its sources from the saved receipt"
            )
        observation = json.loads(args.observation.read_bytes())
        result = project(
            Repo(args.repo), observation["queue"], observation["sources"], args.landed
        )
        saved = observation["prediction"]
        # JSON serializes numeric PR map keys as strings.
        if saved != json.loads(
            json.dumps({k: v for k, v in result.items() if k != "observed"})
        ):
            raise ValueError("The saved prediction disagrees with its original inputs")
        result["computed_at"] = observation["computed_at"]
    else:
        if not args.sources:
            parser.error("--queue requires --sources")
        result = project(
            Repo(args.repo),
            json.loads(args.queue.read_bytes()),
            json.loads(args.sources.read_bytes()),
            args.landed,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
