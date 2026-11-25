"""Account for GitHub Actions attempts during one admission window."""

from datetime import datetime, timezone
import re
from urllib.parse import urlencode

from . import github
from .intake_guard import graphql


def api(path):
    return github.rest("GET", path)


VIEW_QUERY = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){
  pullRequest(number:$number){state mergeCommit{oid} mergedAt url headRefName
    commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100){nodes{__typename
      ... on CheckRun{detailsUrl} ... on StatusContext{targetUrl}}}}}}}}}}"""


def pull_view(repository, number):
    """state, mergeCommit, mergedAt, statusCheckRollup, url and headRefName of a PR."""
    owner, name = repository.split("/")
    data = graphql(VIEW_QUERY, dict(owner=owner, name=name, number=int(number)))
    found = data["repository"]["pullRequest"]
    commits = found.pop("commits")["nodes"]
    rollup = ((commits[0]["commit"] if commits else {}).get("statusCheckRollup") or {})
    found["statusCheckRollup"] = (rollup.get("contexts") or {}).get("nodes", [])
    return found


def attempts(runs, members, wrapper):
    selected = []
    for row in runs:
        match = re.fullmatch(
            r"gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]{40}", row.get("head_branch") or ""
        )
        if match and int(match[1]) in {*members, wrapper}:
            selected.append(
                dict(
                    number=int(match[1]),
                    run=row["id"],
                    attempts=row["run_attempt"],
                    head=row["head_sha"],
                    conclusion=row["conclusion"],
                    url=row["html_url"],
                )
            )
    return selected


def workflow_runs(endpoint, **filters):
    query = urlencode(dict(filters, per_page=100))
    rows = []
    for page in range(1, 11):
        data = api(f"{endpoint}?{query}&page={page}")
        if data["total_count"] > 1000:
            raise ValueError("Window exceeds GitHub's 1,000-run search limit")
        rows.extend(data["workflow_runs"])
        if len(data["workflow_runs"]) < 100:
            break
    return rows


def source_latency(repository, number, head, context, landed):
    pr = api(f"repos/{repository}/pulls/{number}")
    merged = pr["merged_at"] if landed and pr["head"]["sha"] == head else None
    stamp = None
    if merged:
        endpoint = f"repos/{repository}/commits/{head}/statuses"
        page = 1
        while True:
            rows = api(f"{endpoint}?per_page=100&page={page}")
            stop = False
            # GitHub returns newest first. Ignore activity after landing.
            for row in rows:
                if row["context"] != context or row["created_at"] > merged:
                    continue
                if (
                    row["state"] != "success"
                    or (row.get("creator") or {}).get("login") != "github-actions[bot]"
                    or not re.fullmatch(
                        re.escape(f"https://github.com/{repository}/actions/runs/")
                        + r"[1-9]\d*",
                        row.get("target_url") or "",
                    )
                ):
                    stop = True
                    break
                stamp = row["created_at"]
            if stop or len(rows) < 100:
                break
            page += 1
    elapsed = (
        int(
            (
                datetime.fromisoformat(merged) - datetime.fromisoformat(stamp)
            ).total_seconds()
        )
        if stamp
        else None
    )
    return dict(
        number=number,
        head=head,
        trusted_green_at=stamp,
        merged_at=merged,
        trusted_green_to_merge_seconds=elapsed,
        within_120_seconds=elapsed <= 120 if elapsed is not None else None,
    )


def queue_visits(repository, number, merged, measured):
    owner, name = repository.split("/")
    after, visits = None, []
    while True:
        data = graphql(
            """query($o:String!,$r:String!,$n:Int!,$after:String){
          repository(owner:$o,name:$r){pullRequest(number:$n){isInMergeQueue timelineItems(first:100,after:$after,
          itemTypes:[ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT]){
          nodes{__typename ... on AddedToMergeQueueEvent{createdAt} ... on RemovedFromMergeQueueEvent{createdAt reason}}
          pageInfo{hasNextPage endCursor}}}}}""",
            dict(o=owner, r=name, n=number, after=after),
        )["repository"]["pullRequest"]
        queued = data["isInMergeQueue"]
        data = data["timelineItems"]
        for event in data["nodes"]:
            if event["__typename"] == "AddedToMergeQueueEvent":
                visits.append(
                    dict(entered_at=event["createdAt"], exited_at=None, reason=None)
                )
            elif visits and not visits[-1]["exited_at"]:
                visits[-1].update(exited_at=event["createdAt"], reason=event["reason"])
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    for i, visit in enumerate(visits):
        if not visit["exited_at"] and i == len(visits) - 1:
            visit.update(
                exited_at=merged,
                reason="merged"
                if merged
                else ("observed_open" if queued else "unknown"),
            )
        end = visit["exited_at"] or (
            measured if visit["reason"] == "observed_open" else None
        )
        visit["seconds"] = (
            int(
                (
                    datetime.fromisoformat(end)
                    - datetime.fromisoformat(visit["entered_at"])
                ).total_seconds()
            )
            if end
            else None
        )
    return visits


def audit(repository, wrapper, members, since, *, heads=None, context=None):
    start = datetime.fromisoformat(since)
    if start.tzinfo is None:
        raise ValueError("The admission start time must include a timezone")
    created = ">=" + start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    actions = f"repos/{repository}/actions"
    runs = workflow_runs(
        f"{actions}/workflows/trusted-pr-validation.yml/runs",
        event="merge_group",
        created=created,
    )
    selected = attempts(runs, members, wrapper)
    pr = pull_view(repository, wrapper)
    merge = (pr.get("mergeCommit") or {}).get("oid")
    landed = pr["state"] == "MERGED" and any(
        r["number"] == wrapper and r["head"] == merge and r["conclusion"] == "success"
        for r in selected
    )
    fast_runs = {}
    for check in pr["statusCheckRollup"]:
        link = check.get("detailsUrl") or check.get("targetUrl") or ""
        match = re.search(r"/actions/runs/(\d+)", link)
        if match:
            fast_runs[int(match[1])] = link.split("/job/")[0]
    # Draft and closed-PR workflow runs can disappear from the current check
    # rollup. List branch events as well as the linked trusted workflow runs.
    overhead = {
        r["id"]: r
        for r in workflow_runs(
            f"{actions}/runs", branch=pr["headRefName"], created=created
        )
    }
    for run_id in fast_runs.keys() - overhead.keys():
        overhead[run_id] = api(f"{actions}/runs/{run_id}")
    extra = [
        dict(
            run=r["id"],
            name=r["name"],
            event=r["event"],
            attempts=r["run_attempt"],
            conclusion=r["conclusion"],
        )
        for r in overhead.values()
        if r["event"] in ("pull_request", "workflow_run", "push")
    ]
    count = sum(r["attempts"] for r in selected)
    # N separate native entries is the baseline under an all-green merge
    # policy. A split or partial landing needs a cohort-wide audit.
    measured = datetime.now(timezone.utc).isoformat()
    return dict(
        batch_pr=pr["url"],
        sources=members,
        since=since,
        landed=landed,
        separate_entry_baseline=len(members),
        observed_protected_attempts=count,
        protected_attempts_avoided=len(members) - count if landed else None,
        observed_runs=selected,
        wrapper_pr_check_runs=sorted(extra, key=lambda r: r["run"]),
        wrapper_pr_workflow_attempts=sum(r["attempts"] for r in extra),
        source_check_to_merge=[
            source_latency(repository, n, head, context, landed)
            for n, head in (heads or {}).items()
        ],
        native_queue_visits=queue_visits(repository, wrapper, pr["mergedAt"], measured),
        measured_at=measured,
    )
