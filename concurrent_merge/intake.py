"""Coordinate source PRs before native GitHub queue admission, using leased Git refs."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from .blame import changed_paths, verdict
from .inputs import verified_changes
from .intake_audit import audit
from .engine import Engine, Repo, write_json
from .intake_guard import (
    Ineligible,
    SourceResolution,
    batch_name,
    eligibility,
    graphql,
    manifest,
    pin_ref,
    pull,
    pull_many,
    trusted_status,
    verify_sources,
)


def gh(*args):
    return subprocess.check_output(
        ["gh", *map(str, args)], text=True, encoding="utf8"
    ).strip()


def enqueue(pr, head):
    return graphql(
        """mutation($pr:ID!,$head:GitObjectID!){enqueuePullRequest(input:{
          pullRequestId:$pr,expectedHeadOid:$head}){mergeQueueEntry{position}}}""",
        dict(pr=pr, head=head),
    )


def now():
    return datetime.now(timezone.utc).isoformat()


# Artifacts of a trusted run that can hold cached-units receipts (validate.yml uploads artifacts/validation/).
RECEIPT_ARTIFACTS = ("validation-timings-*", "lit-*")


def run_receipts(repository, run_id, directory, patterns=RECEIPT_ARTIFACTS):
    """cached-units receipts with failed test files among a run's artifacts (none if it kept none)."""
    args = ["run", "download", run_id, "-R", repository, "-D", directory]
    for pattern in patterns:
        args += ["-p", pattern]
    try:
        gh(*args)
    except subprocess.CalledProcessError:
        return {}
    failed = {}
    for path in sorted(Path(directory).rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("failed_reads"), dict) and "job" in data:
            failed.update(data["failed_reads"])
    return failed


def member_changes(repo, record):
    """{source number: paths its own step of the batch chain changed}, or None if the chain is not one step per member."""
    rows = repo.out(
        "rev-list", "--first-parent", "--reverse", "--parents", record["base"] + ".." + record["tip"]
    )
    steps = [line.split() for line in rows.splitlines()]
    if len(steps) != len(record["members"]) or any(len(s) < 2 for s in steps):
        return None
    return {
        str(n): changed_paths(repo.path, step[1], step[0])
        for (n, _), step in zip(record["members"], steps)
    }


def split_failed(members, remaining, failed, changes):
    """(sources to send back, groups to retry) for a failed combined run.

    With the failing test files' reads (cached-units receipts) and each member's
    changed paths, blame names who could have caused it: one suspect goes back
    and the rest retry together; several suspects are halved while the others
    retry together. Without receipts, or when a failing file saw no member's
    change, the batch is halved as before."""
    by_id = {str(n): [n, head] for n, head in members}
    mid = len(members) // 2
    halves = ([members[:mid], members[mid:], *remaining], "no receipts or chain: halving")
    if not failed or not changes:
        return [], *halves
    result = verdict(failed, changes)
    keep = [by_id[c] for c in result["keep"]]
    if result["action"] == "eject":
        groups = [keep] if keep else []
        return [by_id[c] for c in result["eject"]], [*groups, *remaining], f"blame names #{result['eject'][0]}"
    if result["action"] == "bisect" and keep:
        named = [by_id[c] for c in result["bisect"]]
        half = max(1, len(named) // 2)
        groups = [keep, named[:half], named[half:]] if len(named) > 1 else [keep, named]
        return [], [g for g in groups if g] + list(remaining), f"blame suspects {', '.join('#' + c for c in result['bisect'])}"
    return [], *halves[:1], f"blame: {result['action']}, halving"


class Intake:
    def __init__(self, repository, branch, context, repo, *, original_prs_only=False):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        self.repo = repo
        self.original_prs_only = original_prs_only
        self.repository, self.branch, self.context = repository, branch, context
        self.url = "https://github.com/" + repository + ".git"
        self.target = "refs/heads/" + branch
        self.active = "refs/heads/lit/active/" + branch
        self.preview = None
        self.last_wait = None
        self.manifest_cache = None
        repo.run("check-ref-format", self.active)

    def refs(self):
        output = self.repo.out(
            "ls-remote",
            "--heads",
            self.url,
            self.target,
            self.active,
            "refs/heads/lit/ready/*",
        )
        return {ref: oid for oid, ref in (line.split() for line in output.splitlines())}

    def push(self, edits):
        # Each (ref, expected old value, new value) is part of one CAS transaction.
        leases = [f"--force-with-lease={ref}:{old}" for ref, old, _ in edits]
        specs = [f"{new}:{ref}" for ref, _, new in edits]
        self.repo.run("push", "--atomic", *leases, self.url, *specs)

    def fetch_commits(self, heads):
        """Transfer missing immutable objects together. GitHub owns live readiness."""
        heads = list(dict.fromkeys(heads))
        if not heads:
            return
        if any(not re.fullmatch(r"[0-9a-f]{40}", head) for head in heads):
            raise Ineligible("Expected full commit IDs from GitHub")
        data = ("\n".join(heads) + "\n").encode("ascii")
        rows = self.repo.out(
            "cat-file", "--batch-check=%(objectname) %(objecttype)", data=data
        ).splitlines()
        missing = []
        for head, row in zip(heads, rows, strict=True):
            if row == head + " missing":
                missing.append(head)
            elif row != head + " commit":
                raise Ineligible("Unexpected Git object in source commit set")
        if missing:
            self.repo.run(
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--stdin",
                self.url,
                data=("\n".join(missing) + "\n").encode("ascii"),
            )

    def register(self, numbers):
        refs, edits = self.refs(), []
        admitted = 0
        for n, (pr, _) in pull_many(self.repository, numbers).items():
            head = pr["headRefOid"] if pr else ""
            reason = eligibility(self.repository, self.branch, pr, head, self.context)
            if reason:
                print(f"Hold #{n}: {reason}", flush=True)
                continue
            admitted += 1
            ref = pin_ref(n)
            if refs.get(ref) != head:
                edits.append((ref, refs.get(ref, ""), head))
        if not admitted:
            raise Ineligible("None of the requested source revisions are ready")
        if edits:
            self.fetch_commits(head for _, _, head in edits)
            self.push(edits)

    def record(self, value, parent):
        tree = self.repo.out("rev-parse", parent + "^{tree}")
        return self.repo.out(
            "-c",
            "user.name=lit",
            "-c",
            "user.email=batch@localhost",
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            json.dumps(value, separators=(",", ":")),
        )

    def state(self, oid):
        self.fetch_commits([oid])
        record = json.loads(self.repo.out("show", "-s", "--format=%B", oid))
        if "tip" in record:
            name = batch_name(record["base"], record["tip"])
            # Only the immutable Git reconstruction can be cached. Compare the
            # current record and re-read reviews and ready refs every time.
            identity = (record["base"], record["tip"])
            if not self.manifest_cache or self.manifest_cache[0] != identity:
                self.manifest_cache = (
                    identity,
                    manifest(self.repo, name, record["tip"], record["base"]),
                )
            members = self.manifest_cache[1]
            if [list(m) for m in members] != record["members"]:
                raise Ineligible(
                    "Stored source list disagrees with the batch commit graph"
                )
        return record

    def claim(self, refs, base, tip, remaining=()):
        self.fetch_commits([refs[self.target]])
        members = manifest(self.repo, batch_name(base, tip), tip, refs[self.target])
        green = verify_sources(self.repository, self.branch, members, self.context)
        record = dict(
            base=base,
            tip=tip,
            members=members,
            remaining=list(remaining),
            started=now(),
            source_green_at=green,
        )
        # A single source is a one-member batch too: its batch PR runs the full suites on the exact
        # tree the queue will build, and the queue group reuses that run while main has not moved.
        # (A record marked "individual" by an older coordinator still advances on the original PR.)
        branches = [("refs/heads/" + batch_name(base, tip), "", tip)]
        lock = self.record(record, tip)
        self.push([(self.active, refs.get(self.active, ""), lock), *branches])
        return lock, record

    def assemble(self, refs, selected=None):
        base = refs[self.target]
        pins = {
            int(ref.rsplit("/", 1)[1]): head
            for ref, head in refs.items()
            if re.fullmatch(r"refs/heads/lit/ready/[1-9]\d*", ref)
        }
        rows = []
        requests = [
            (n, head)
            for n, head in (selected if selected is not None else sorted(pins.items()))
            if pins.get(n) == head
        ]
        sources = pull_many(self.repository, (n for n, _ in requests))
        for n, head in requests:
            pr, pin = sources[n]
            reason = eligibility(self.repository, self.branch, pr, head, self.context)
            if reason or pin != head:
                print(f"Hold #{n}: {reason or 'ready ref changed'}", flush=True)
                continue
            rows.append(dict(number=n, head=head, author=pr["author"]["login"]))
        if not rows:
            return None
        self.fetch_commits([base, *(p["head"] for p in rows)])
        changes = verified_changes(self.repo, dict(base=base, changes=rows))
        engine = Engine(
            self.repo, self.repo.path / "lit-state", shutil.which("weave-driver")
        )
        # Use the parallel engine on the entire ready list. Rebuild only when an
        # author must put a structural resolution on their own source branch.
        while changes:
            tip, accepted = engine.batch(base, changes, lambda _: "model")
            for issue in engine.history[-1]["pending"]:
                print(f"Hold #{issue['number']}: {issue}", flush=True)
            if not accepted:
                return None
            try:
                manifest(self.repo, batch_name(base, tip), tip, base)
                return base, tip
            except SourceResolution as error:
                print(f"Hold #{error.number}: {error}", flush=True)
                changes = [p for p in changes if p["number"] != error.number]
        return None

    def batch_pr(self, record):
        name = batch_name(record["base"], record["tip"])
        rows = json.loads(
            gh(
                "pr",
                "list",
                "-R",
                self.repository,
                "--head",
                name,
                "--base",
                self.branch,
                "--state",
                "all",
                "--json",
                "number,state,headRefOid,isCrossRepository,url",
            )
        )
        rows = [p for p in rows if not p["isCrossRepository"]]
        if rows:
            if len(rows) != 1 or rows[0]["headRefOid"] != record["tip"]:
                raise Ineligible("The batch branch or PR was replaced")
            return rows[0]
        verify_sources(self.repository, self.branch, record["members"], self.context)
        numbers = ", ".join(f"#{n}" for n, _ in record["members"])
        body = (
            "lit collected the following ready source revisions before native queue admission. "
            "The protected merge-group workflow validates the combined change.\n\n"
            + "\n".join(f"- #{n} at `{head}`" for n, head in record["members"])
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "body.md"
            path.write_text(body + "\n", encoding="utf8")
            print(
                gh(
                    "pr",
                    "create",
                    "-R",
                    self.repository,
                    "--head",
                    name,
                    "--base",
                    self.branch,
                    "--draft",
                    "--title",
                    "lit batch for " + numbers[:180],
                    "--body-file",
                    path,
                ),
                flush=True,
            )
        return self.batch_pr(record)

    def finish(self, refs, lock, record, remove=(), pending=None):
        edits = [
            (pin_ref(n), head, "") for n, head in remove if refs.get(pin_ref(n)) == head
        ]
        pending = record.get("remaining", []) if pending is None else pending
        next_lock = self.record(dict(remaining=pending), lock) if pending else ""
        self.push([*edits, (self.active, lock, next_lock)])

    def advance_individual(self, refs, lock, record):
        number, head = record["members"][0]
        pr, pin = pull(self.repository, number)
        if not self.submit_individual(number, head, pr, pin, record["started"]):
            return False
        self.finish(refs, lock, record, record["members"])
        return True

    def submit_individual(self, number, head, pr, pin, started):
        if pr and pr["headRefOid"] == head and pr["isInMergeQueue"]:
            print(f"Original PR #{number} is in the protected queue.", flush=True)
        elif pin == head:
            reason = eligibility(self.repository, self.branch, pr, head, self.context)
            if reason and "(UNKNOWN)" in reason:
                return False
            if reason:
                print(f"Released #{number}: {reason}", flush=True)
            else:
                # Do not retry a failed or withdrawn native submission if the
                # client lost its response or restarted after the entry finished.
                timeline = graphql(
                    """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){
                      pullRequest(number:$n){timelineItems(last:1,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){
                      nodes{... on RemovedFromMergeQueueEvent{createdAt}}}}}}""",
                    dict(zip(("o", "r", "n"), [*self.repository.split("/"), number])),
                )["repository"]["pullRequest"]["timelineItems"]["nodes"]
                if timeline and datetime.fromisoformat(
                    timeline[-1]["createdAt"]
                ) >= datetime.fromisoformat(started):
                    print(
                        f"Original PR #{number} already left the native queue.",
                        flush=True,
                    )
                else:
                    enqueue(pr["id"], head)
                    print(
                        f"Submitted original PR #{number} to the protected queue.",
                        flush=True,
                    )
        return True

    def drain_originals(self, refs, lock, record):
        # Persist the heads and starting time before any GitHub mutation so
        # recovery can recognize entries that were already removed from the queue.
        pins = sorted(
            (int(ref.rsplit("/", 1)[1]), head)
            for ref, head in refs.items()
            if re.fullmatch(r"refs/heads/lit/ready/[1-9]\d*", ref)
        )
        members = record.get("originals", [])
        tracked = {(n, head) for n, head, _ in members}
        started = now()
        new = [(n, head, started) for n, head in pins if (n, head) not in tracked]
        if new:
            parent = lock or refs[self.target]
            self.fetch_commits([parent])
            claim = self.record(dict(originals=[*members, *new]), parent)
            self.push([(self.active, lock, claim)])
            return True
        if not members:
            if lock:
                self.finish(refs, lock, record, pending=[])
            return False
        sources = pull_many(self.repository, (n for n, _, _ in members))
        pending, done = [], []
        for number, head, started in members:
            pr, pin = sources[number]
            if self.submit_individual(number, head, pr, pin, started):
                done.append((number, head))
            else:
                pending.append((number, head, started))
        if not pending:
            self.finish(refs, lock, record, done)
        elif done:
            updated = self.record(dict(originals=pending), lock)
            edits = [(pin_ref(n), h, "") for n, h in done if refs.get(pin_ref(n)) == h]
            self.push([*edits, (self.active, lock, updated)])
        else:
            time.sleep(2)
        return True

    def advance(self, refs, lock, record):
        if record.get("individual"):
            return self.advance_individual(refs, lock, record)
        try:
            row = self.batch_pr(record)
        except Ineligible as error:
            if "(UNKNOWN)" in str(error):
                return False
            # No wrapper exists yet or the branch was replaced. Leave source pins
            # intact so the next collection can admit the still-eligible subset.
            if "batch branch" in str(error):
                raise
            self.finish(refs, lock, record)
            return True
        number, tip = row["number"], record["tip"]
        if row["state"] == "MERGED":
            self.fetch_commits([refs[self.target]])
            self.repo.run("merge-base", "--is-ancestor", tip, refs[self.target])
            print(
                f"Landed batch #{number} with {len(record['members'])} source PRs.",
                flush=True,
            )
            self.finish(refs, lock, record, record["members"])
            return True
        if "closing" in record:
            if row["state"] == "OPEN":
                gh("pr", "close", number, "-R", self.repository)
            plan = record["closing"]
            self.finish(refs, lock, record, plan["remove"], plan["pending"])
            return True
        if row["state"] == "CLOSED":
            print(f"Batch #{number} closed; releasing its pins.", flush=True)
            self.finish(refs, lock, record, record["members"])
            return True
        try:
            verify_sources(
                self.repository, self.branch, record["members"], self.context
            )
        except Ineligible as error:
            # GitHub briefly reports UNKNOWN while recomputing mergeability.
            if "(UNKNOWN)" in str(error):
                return False
            print(f"Withdrawing batch #{number}: {error}", flush=True)
            self.plan_close(lock, record, [], record["remaining"])
            return True
        pr, _ = pull(self.repository, number)
        if not pr or pr["headRefOid"] != tip or pr["baseRefName"] != self.branch:
            raise Ineligible("The batch PR was retargeted or its head was replaced")
        if pr["isDraft"]:
            events = graphql(
                """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){
              pullRequest(number:$n){timelineItems(last:1,itemTypes:[CONVERT_TO_DRAFT_EVENT]){totalCount}}}}""",
                dict(zip(("o", "r", "n"), [*self.repository.split("/"), number])),
            )
            if events["repository"]["pullRequest"]["timelineItems"]["totalCount"]:
                # A reviewer explicitly put the batch back into draft.
                return False
            gh("pr", "ready", number, "-R", self.repository)
            return True
        if pr["isInMergeQueue"]:
            entry = pr.get("mergeQueueEntry") or {}
            group = (entry.get("headCommit") or {}).get("oid")
            detail = (
                f"protected group {group[:12]} assigned"
                if group
                else "waiting for a protected group commit"
            )
            return self.wait_reason(
                number,
                record,
                f"native queue {entry.get('state')}, position {entry.get('position')}, {detail}",
            )
        if pr["mergeStateStatus"] == "DIRTY":
            print(
                f"Batch #{number} conflicts with current main; recollecting.",
                flush=True,
            )
            self.plan_close(lock, record, [], record["remaining"])
            return True
        timeline = graphql(
            """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){
          pullRequest(number:$n){timelineItems(last:1,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){
          nodes{... on RemovedFromMergeQueueEvent{id reason}}}}}}""",
            dict(zip(("o", "r", "n"), [*self.repository.split("/"), number])),
        )
        removed = timeline["repository"]["pullRequest"]["timelineItems"]["nodes"]
        trusted = trusted_status(self.repository, pr, tip, self.context) or {}
        status = trusted.get("state")
        removal = removed[-1] if removed else None
        reason = removal["reason"] if removal else None
        # A conflicting predecessor may itself fail and leave. That rejection
        # contains no failed test from this batch. Keep its existing green PR.
        if reason == "merge_conflict" and status not in ("FAILURE", "ERROR"):
            if (
                status == "SUCCESS"
                and pr["mergeStateStatus"] == "CLEAN"
                and pr["reviewDecision"] in (None, "APPROVED")
            ):
                return self.admit_checked(refs, lock, record, pr, removal)
            return self.wait_reason(
                number,
                record,
                "rechecking the existing batch after a native merge conflict",
            )
        if removed or status in ("FAILURE", "ERROR"):
            if reason in (None, "merge_conflict"):
                reason = "failed_checks"
            members = record["members"]
            print(f"Batch #{number} left admission: {reason}", flush=True)
            if reason == "failed_checks" and len(members) > 1:
                remove, pending, why = self.blame(record, trusted)
                print(f"Batch #{number}: {why}", flush=True)
                self.plan_close(lock, record, remove, pending)
            else:
                self.plan_close(lock, record, members, record["remaining"])
            return True
        if (
            status == "SUCCESS"
            and pr["mergeStateStatus"] == "CLEAN"
            and pr["reviewDecision"] in (None, "APPROVED")
        ):
            return self.admit_checked(refs, lock, record, pr, None)
        return self.wait_reason(
            number,
            record,
            f"batch checks {status or 'pending'}, GitHub merge state {pr['mergeStateStatus']}",
        )

    def blame(self, record, trusted):
        """Who leaves a failed batch, from what its failing test files read (see split_failed)."""
        members = record["members"]
        run = (trusted.get("targetUrl") or "").rsplit("/", 1)[-1]
        failed, changes = {}, None
        try:
            changes = member_changes(self.repo, record)
            if run.isdigit():
                with tempfile.TemporaryDirectory(prefix="lit-receipts-") as directory:
                    failed = run_receipts(self.repository, run, directory)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
            # Blame only narrows a split; whatever stops it, the batch is halved as before.
            print(f"Blame unavailable: {error!r}", flush=True)
        return split_failed(members, record["remaining"], failed, changes)

    def wait_reason(self, number, record, reason):
        # Preserve the batch start across process restarts. Each source's exact
        # green time is available separately, so this clock never claims to
        # measure a review or check that happened before intake.
        current = datetime.now(timezone.utc)
        age = int((current - datetime.fromisoformat(record["started"])).total_seconds())
        green = [
            datetime.fromisoformat(t)
            for t in record.get("source_green_at", {}).values()
        ]
        source_age = int((current - min(green)).total_seconds()) if green else None
        notice = (number, reason, age // 120)
        if notice != self.last_wait:
            source = (
                f", {source_age}s since oldest source check passed (target 120s)"
                if green
                else ""
            )
            print(f"Batch #{number}, {age}s since intake{source}: {reason}", flush=True)
            self.last_wait = notice
        return False

    def queue_boundary(self):
        owner, name = self.repository.split("/")
        queue = graphql(
            """query($o:String!,$r:String!,$b:String!){repository(owner:$o,name:$r){
              mergeQueue(branch:$b){entries(last:1){nodes{pullRequest{number} headCommit{oid}}}}}}""",
            dict(o=owner, r=name, b=self.branch),
        )["repository"]["mergeQueue"]
        if queue is None:
            raise Ineligible("The protected merge queue is unavailable")
        entries = queue["entries"]["nodes"]
        return entries[0] if entries else None

    def preview_conflict(self, predecessor, tip):
        head = (predecessor.get("headCommit") or {}).get("oid")
        if not head:
            return False
        if not self.preview or self.preview[:2] != (head, tip):
            self.fetch_commits([head])
            result = self.repo.run("merge-tree", "--write-tree", head, tip, check=False)
            if result.returncode not in (0, 1):
                raise RuntimeError("Could not preview the native queue commit")
            conflict = result.returncode == 1
            if conflict:
                write_json(
                    self.repo.path
                    / "lit-state"
                    / "queue-conflicts"
                    / f"{head}-{tip}.json",
                    dict(
                        predecessor=predecessor["pullRequest"]["number"],
                        predecessor_head=head,
                        batch_head=tip,
                        git_merge=result.stdout.decode("utf8", errors="replace"),
                    ),
                )
            self.preview = (head, tip, conflict)
        return self.preview[2]

    def admit_checked(self, refs, lock, record, pr, removal):
        number, tip = pr["number"], record["tip"]
        predecessor = self.queue_boundary()
        if predecessor and self.preview_conflict(predecessor, tip):
            return self.wait_reason(
                number,
                record,
                f"overlap with native PR #{predecessor['pullRequest']['number']}; keeping the checked batch together",
            )
        if removal:
            if predecessor and not predecessor.get("headCommit"):
                return self.wait_reason(
                    number,
                    record,
                    "waiting for the preceding native entry to acquire a test commit",
                )
            boundary = (
                predecessor["headCommit"]["oid"] if predecessor else refs[self.target]
            )
            retry = record.get("conflict_retry", {})
            event = removal["id"]
            if retry.get("event") != event:
                if retry.get("boundary") == boundary:
                    print(
                        f"Native queue rejected batch #{number} twice on the same boundary; returning its sources for review.",
                        flush=True,
                    )
                    self.plan_close(
                        lock, record, record["members"], record["remaining"]
                    )
                    return True
                updated = dict(
                    record, conflict_retry=dict(event=event, boundary=boundary)
                )
                self.push([(self.active, lock, self.record(updated, lock))])
                return True
        enqueue(pr["id"], tip)
        print(f"Enqueued batch #{number} on its exact head.", flush=True)
        return False

    def plan_close(self, lock, record, remove, pending):
        # Persist the split before closing. A second coordinator or a restart can
        # finish the same transition without returning healthy sources by mistake.
        next_record = dict(record, closing=dict(remove=remove, pending=pending))
        self.push([(self.active, lock, self.record(next_record, lock))])

    def drain(self):
        for attempt in range(4):
            try:
                return self._drain()
            except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
                if attempt == 3:
                    raise
                print(
                    f"Re-reading the durable intake after a failed operation: {error}",
                    flush=True,
                )
                time.sleep(2**attempt)

    def _drain(self):
        while True:
            refs = self.refs()
            lock = refs.get(self.active, "")
            record = self.state(lock) if lock else {}
            if (
                self.original_prs_only
                and "tip" in record
                and not record.get("individual")
            ):
                raise Ineligible(
                    "An older wrapper is active. Withdraw its admission before submitting originals."
                )
            if "originals" in record or (
                self.original_prs_only and "tip" not in record
            ):
                if not self.drain_originals(refs, lock, record):
                    return
                continue
            if "tip" in record:
                if not self.advance(refs, lock, record):
                    time.sleep(30)
                continue
            groups = record.get("remaining", [])
            prepared = self.assemble(refs, groups[0] if groups else None)
            if prepared:
                self.claim(refs, *prepared, remaining=groups[1:])
            elif groups:
                self.finish(refs, lock, record, pending=groups[1:])
            else:
                print("No eligible source requests remain.", flush=True)
                return


def main(argv=None, *, repository, branch="main", context, original_prs_only=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.home() / ".cache" / "lit" / repository / (branch + ".git"),
    )
    cmd = parser.add_subparsers(dest="action", required=True)
    ready = cmd.add_parser(
        "ready", help="Register ready source revisions and drain the intake"
    )
    ready.add_argument("numbers", nargs="+", type=int)
    cmd.add_parser("drain", help="Resume admission from the durable Git refs")
    withdraw = cmd.add_parser("withdraw", help="Withdraw an exact source request")
    withdraw.add_argument("number", type=int)
    withdraw.add_argument("head")
    report = cmd.add_parser(
        "report", help="Count protected workflow attempts for a landed batch"
    )
    report.add_argument("number", type=int)
    report.add_argument(
        "--since", help="Earlier admission-window start (ISO time with offset)"
    )
    args = parser.parse_args(argv)
    repo = Repo(args.repo_dir)
    if not repo.path.exists():
        repo.path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "--quiet", repo.path], check=True)
    if repo.out("rev-parse", "--is-bare-repository") != "true":
        parser.error("--repo-dir must be a dedicated bare repository")
    api = Intake(repository, branch, context, repo, original_prs_only=original_prs_only)
    if args.action == "withdraw":
        if not re.fullmatch(r"[0-9a-f]{40}", args.head):
            parser.error("withdraw requires the exact 40-hex source head")
        api.push([(pin_ref(args.number), args.head, "")])
        return
    if args.action == "report":
        pr = json.loads(
            gh(
                "pr",
                "view",
                args.number,
                "-R",
                repository,
                "--json",
                "createdAt,headRefOid,headRefName,baseRefName",
            )
        )
        if pr["baseRefName"] != branch:
            parser.error("Batch targets a different branch")
        refs = api.refs()
        repo.run(
            "fetch",
            "--quiet",
            "--no-tags",
            api.url,
            refs[api.target],
            f"refs/pull/{args.number}/head",
        )
        members = manifest(repo, pr["headRefName"], pr["headRefOid"], refs[api.target])
        print(
            json.dumps(
                audit(
                    repository,
                    args.number,
                    [n for n, _ in members],
                    args.since or pr["createdAt"],
                    heads=dict(members),
                    context=context,
                ),
                indent=2,
            )
        )
        return
    if args.action == "ready":
        api.register(args.numbers)
    api.drain()
