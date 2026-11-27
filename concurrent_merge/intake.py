"""Coordinate source PRs before native GitHub queue admission, using leased Git refs."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
import zipfile

from . import github
from .blame import changed_paths, verdict
from .inputs import verified_changes
from .intake_audit import audit
from .proposal import create_pull, prior_pulls
from .engine import Engine, Repo, write_json
from .intake_guard import (
    Ineligible,
    SourceResolution,
    batch_name,
    eligibility,
    failed_queue_check,
    graphql,
    manifest,
    pin_ref,
    reject_ref,
    pull,
    pull_many,
    trusted_status,
    verify_sources,
)


def ready_for_review(repository, number):
    """Take a PR out of draft; GitHub offers this only through GraphQL."""
    node = github.rest("GET", f"repos/{repository}/pulls/{number}")["node_id"]
    github.graphql(
        "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){clientMutationId}}",
        dict(id=node),
    )


def enqueue(pr, head):
    return graphql(
        """mutation($pr:ID!,$head:GitObjectID!){enqueuePullRequest(input:{
          pullRequestId:$pr,expectedHeadOid:$head}){mergeQueueEntry{position}}}""",
        dict(pr=pr, head=head),
    )


def disable_auto_merge(pr_id):
    return graphql(
        """mutation($pr:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$pr}){clientMutationId}}""",
        dict(pr=pr_id),
    )


TAKEOVER_NOTE = (
    "lit turned off auto-merge on this PR because it carries the `{label}` label. "
    "lit will batch it with other ready PRs and merge it through the merge queue. "
    "To merge it on its own instead, remove the label before turning auto-merge back on."
)


def log_round(record, end, landed):
    if not end:
        return
    def elapsed(start):
        try:
            delta = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
            return int(delta) if delta >= 0 else None
        except (TypeError, ValueError):
            return None
    if (t := elapsed(record.get("started"))) is not None:
        print(f"Round duration: {t}s", flush=True)
    if landed:
        for pr, stamp in sorted(record.get("source_green_at", {}).items()):
            if (t := elapsed(stamp)) is not None:
                print(f"Source #{pr} checks-to-main: {t}s", flush=True)


def now():
    return datetime.now(timezone.utc).isoformat()


# Artifacts of a trusted run that can hold cached-units receipts (validate.yml uploads artifacts/validation/).
RECEIPT_ARTIFACTS = ("validation-timings-*", "lit-*")


def run_receipts(repository, run_id, directory, patterns=RECEIPT_ARTIFACTS):
    """cached-units receipts with failed test files among a run's artifacts (none if it kept none)."""
    try:
        github.download_artifacts(repository, run_id, directory, patterns)
    except (github.GitHubError, OSError, zipfile.BadZipFile):
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


def queue_runs(rows, branch, number):
    """Ids of the completed runs of batch #number's newest merge group, oldest first.

    A batch that fails in the merge queue keeps its fast PR status, and the
    run behind that status never ran the full suites, so the receipts of the
    failing tests are only among the merge-group runs."""
    prefix = f"gh-readonly-queue/{branch}/pr-{number}-"
    done = [
        r for r in rows
        if str(r.get("head_branch") or "").startswith(prefix) and r.get("status") == "completed"
    ]
    if not done:
        return []
    newest = max(done, key=lambda r: (str(r.get("created_at") or ""), int(r["id"])))["head_sha"]
    return [str(i) for i in sorted(int(r["id"]) for r in done if r.get("head_sha") == newest)]


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


# Before any batch has finished, assume about one ready PR in fifty is red (a first batch of 32).
PRIOR_RED, PRIOR_TESTED = 0.1, 5.0
MAX_AUTO_BATCH = 128


def red_share(red):
    """Recent share of ready PRs that came back red, from decayed [returned, tested] counts."""
    returned, tested = red or (0.0, 0.0)
    return (returned + PRIOR_RED) / (tested + PRIOR_TESTED)


def group_size(red):
    """How many ready PRs one batch should hold, by Hwang's binary splitting.

    With a share p of ready PRs red, a group of 2^a with a = floor(log2((1 - p) / p))
    is about as large as a group can be before a red result, and the halving it starts,
    costs more runs than the extra PRs save: one in ten red gives batches of 8, one in
    a hundred gives 64."""
    p = min(0.5, max(1 / (2 * MAX_AUTO_BATCH), red_share(red)))
    return max(1, min(MAX_AUTO_BATCH, 2 ** math.floor(math.log2((1 - p) / p))))


def counted(record, returned, tested, keep=0.8):
    """`record` with `returned` red PRs out of `tested` finished ones added to its red counts.

    Each PR counts once, when it lands or goes back to its author. Older counts fade
    by `keep` per finished batch, so the size follows the swarm's current red share.
    A split that sends nobody back finishes no PR and changes nothing."""
    if not tested:
        return record
    old_returned, old_tested = record.get("red") or (0.0, 0.0)
    red = [round(old_returned * keep + returned, 4), round(old_tested * keep + tested, 4)]
    return dict(record, red=red)


def batch_limit(max_batch, record):
    """The most ready PRs a fresh batch takes: None (all of them), a fixed number, or sized by red share."""
    if max_batch == "auto":
        return group_size(record.get("red"))
    return max_batch


def refusal(said):
    """The line of a refused push that says why: what the remote said, else git's rejection line."""
    said = said.removeprefix("git push:")
    lines = [line.strip() for line in said.splitlines()]
    remote = next(
        (l[len("remote:"):].strip() for l in lines if l.startswith("remote:") and l[len("remote:"):].strip()),
        None,
    )
    line = remote or next((l for l in lines if "rejected]" in l), None)
    line = line or next((l for l in reversed(lines) if l), "")
    return line[:200]


def direct_overlap(branch, footprints, extra):
    """Why a batch can't land directly, or None when its members' own checks cover it.

    Each footprint is (number, own, drift, differ): the files the member changed, the files
    the branch changed since the member's base, and the files where the member head and the
    batch tip differ. `extra` lists what the tip changes on the base."""
    seen = {}
    for number, own, drift, differ in footprints:
        drift, differ = set(drift), set(differ)
        for path in own:
            if path in drift:
                return f"#{number} and {branch} both changed {path}"
            if path in seen:
                return f"#{seen[path]} and #{number} both changed {path}"
            seen[path] = number
            if path in differ:
                return f"the batch does not keep #{number}'s {path}"
    return next((f"the batch changes {p} outside its PRs" for p in extra if p not in seen), None)


class Intake:
    def __init__(
        self, repository, branch, context, repo, *, original_prs_only=False, max_batch=None,
        direct_context=None,
    ):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        self.repo = repo
        self.original_prs_only = original_prs_only
        self.max_batch = max_batch
        self.direct_context = direct_context
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
            "refs/heads/lit/parallel/" + self.branch,
            "refs/heads/lit/ready/*",
            "refs/heads/lit/rejected/*",
            "refs/heads/lit/prior-rejections/*",
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

    def retain_rejection(self, refs, edits, old):
        history = "refs/heads/lit/prior-rejections/" + old
        if history in refs or any(ref == history for ref, _, _ in edits):
            return
        base = refs[self.target]
        self.fetch_commits([base, old])
        reachable = self.repo.run("merge-base", "--is-ancestor", old, base, check=False)
        if reachable.returncode not in (0, 1):
            reachable.check_returncode()
        if reachable.returncode:
            edits.append((history, "", old))

    def register(self, numbers):
        refs, edits = self.refs(), []
        admitted = 0
        for n, (pr, _) in pull_many(self.repository, numbers).items():
            head = pr["headRefOid"] if pr else ""
            reason = eligibility(self.repository, self.branch, pr, head, self.context)
            if reason:
                print(f"Hold #{n}: {reason}", flush=True)
                continue
            held = refs.get(reject_ref(n))
            if held == head:
                print(f"#{n} failed checks alone at {head[:12]}; waiting for a new push", flush=True)
                continue
            if held:
                self.retain_rejection(refs, edits, held)
                edits.append((reject_ref(n), held, ""))
            admitted += 1
            ref = pin_ref(n)
            if refs.get(ref) != head:
                edits.append((ref, refs.get(ref, ""), head))
        if not admitted:
            raise Ineligible("None of the requested source revisions are ready")
        if edits:
            self.fetch_commits(head for _, _, head in edits if head)
            self.push(edits)

    def record(self, value, parent):
        tree = self.repo.out("rev-parse", parent + "^{tree}")
        # Record messages determine commit IDs. Match the native writer even after a later
        # transition adds a field to a record read from an older coordinator.
        order = ("base", "tip", "members", "remaining", "started", "source_green_at",
                 "originals", "individual", "red", "conflict_retry", "closing")
        value = {k: value[k] for k in (*order, *sorted(value.keys() - set(order))) if k in value}
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

    def claim(self, refs, base, tip, remaining=(), red=None, fresh=False, isolating=False, isolated_sibling=False, guard=()):
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
        if red:
            record["red"] = red
        if fresh:
            # Never-failed PRs went first; the next round takes the first suspect group.
            record["fresh"] = True
        if isolating:
            record["isolating"] = True
        if isolated_sibling:
            record["isolated_sibling"] = True
        # A single source is a one-member batch too: its batch PR runs the full suites on the exact
        # tree the queue will build, and the queue group reuses that run while main has not moved.
        # (A record marked "individual" by an older coordinator still advances on the original PR.)
        branches = [("refs/heads/" + batch_name(base, tip), "", tip)]
        lock = self.record(record, tip)
        self.push([(self.active, refs.get(self.active, ""), lock), *branches, *guard])
        return lock, record

    def assemble(self, refs, selected=None, limit=None):
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
        if limit and len(rows) > limit:
            # Lowest PR numbers first; the rest stay pinned for the next batch.
            print(f"Batching {limit} of {len(rows)} ready PRs; the rest wait for the next batch.", flush=True)
            rows = rows[:limit]
        if not rows:
            return None
        self.fetch_commits([base, *(p["head"] for p in rows)])
        changes = verified_changes(self.repo, dict(base=base, changes=rows))
        engine = Engine(self.repo, self.repo.path / "lit-state")
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
        rows = prior_pulls(self.repository, self.branch, name)
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
                create_pull(
                    self.repository,
                    self.branch,
                    name,
                    "lit batch for " + numbers[:180],
                    path,
                    draft=False,
                ),
                flush=True,
            )
        return self.batch_pr(record)

    def finish(self, refs, lock, record, remove=(), pending=None, reject=()):
        self.push(self.finish_edits(refs, lock, record, remove, pending, reject))

    def finish_edits(self, refs, lock, record, remove=(), pending=None, reject=()):
        """The ref updates that close `record`, leaving `pending` (or nothing) as the next record."""
        edits = [
            (pin_ref(n), head, "") for n, head in remove if refs.get(pin_ref(n)) == head
        ]
        for n, head in reject:
            ref = reject_ref(n)
            old = refs.get(ref, "")
            if old == head:
                continue
            if old:
                self.retain_rejection(refs, edits, old)
            edits.append((ref, old, head))
        pending = record.get("remaining", []) if pending is None else pending
        red = record.get("red")
        if pending:
            next = dict(remaining=pending, **({"red": red} if red else {}))
            if record.get("fresh"):
                next["after_fresh"] = True
            next_lock = self.record(next, lock)
        elif red and self.max_batch == "auto" and not self.original_prs_only:
            # Keep the red counts between batches; an idle record has nothing left to do.
            next_lock = self.record(dict(remaining=[], red=red), lock)
        else:
            next_lock = ""
        return [*edits, (self.active, lock, next_lock)]

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
            return "waiting"
        return True

    def advance(self, refs, lock, record):
        if record.get("individual"):
            return self.advance_individual(refs, lock, record)
        progress = self.direct(refs, lock, record)
        if progress is not None:
            return progress
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
            log_round(record, row.get("mergedAt"), True)
            record = counted(record, 0, len(record["members"]))
            self.finish(refs, lock, record, record["members"])
            return True
        if "closing" in record:
            if row["state"] == "OPEN":
                github.rest("PATCH", f"repos/{self.repository}/pulls/{number}", body=dict(state="closed"))
            plan = record["closing"]
            self.finish(refs, lock, record, plan["remove"], plan["pending"], plan.get("reject", ()))
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
              pullRequest(number:$n){timelineItems(last:1,itemTypes:[CONVERT_TO_DRAFT_EVENT]){nodes{__typename}}}}}""",
                dict(zip(("o", "r", "n"), [*self.repository.split("/"), number])),
            )
            if events["repository"]["pullRequest"]["timelineItems"]["nodes"]:
                # A reviewer explicitly put the batch back into draft.
                return False
            ready_for_review(self.repository, number)
            return True
        if pr["isInMergeQueue"]:
            entry = pr.get("mergeQueueEntry") or {}
            group = (entry.get("headCommit") or {}).get("oid")
            if len(record["members"]) > 1 and entry.get("position") == 1 and group:
                try:
                    failed = failed_queue_check(self.repository, number, tip, group)
                except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                    print(f"Could not read required queue checks: {exc}", flush=True)
                    failed = None
                if failed:
                    age = int((datetime.fromisoformat(now()) - datetime.fromisoformat(record["started"])).total_seconds())
                    print(f"Batch #{number}: required check {failed} failed on {group} after {age}s; splitting.", flush=True)
                    _, pending, _ = split_failed(record["members"], record["remaining"], {}, {})
                    self.plan_close(lock, record, [], pending)
                    return True
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
            log_round(record, now(), False)
            if reason == "failed_checks" and len(members) > 1:
                remove, pending, why = self.blame(number, record, trusted)
                print(f"Batch #{number}: {why}", flush=True)
                self.plan_close(
                    lock,
                    counted(record, len(remove), len(remove)),
                    remove,
                    pending,
                    **(dict(reject=remove) if remove else {}),
                )
            else:
                if reason == "failed_checks":
                    record = counted(record, len(members), len(members))
                self.plan_close(
                    lock,
                    record,
                    members,
                    record["remaining"],
                    **(dict(reject=members) if reason == "failed_checks" else {}),
                )
            return True
        if (
            status == "SUCCESS"
            and pr["mergeStateStatus"] == "CLEAN"
            and pr["reviewDecision"] in (None, "APPROVED")
        ):
            progress = self.direct_batch(refs, lock, record, pr)
            if progress is not None:
                return progress
            return self.admit_checked(refs, lock, record, pr, None)
        return self.wait_reason(
            number,
            record,
            f"batch checks {status or 'pending'}, GitHub merge state {pr['mergeStateStatus']}",
        )

    def queue_instead(self, lock, record, why):
        print(f"Batch {record['tip'][:12]} goes through the merge queue: {why}", flush=True)
        self.push([(self.active, lock, self.record(dict(record, direct=why), lock))])
        return True

    def direct(self, refs, lock, record):
        """Land the batch on the branch itself when its members' own full-suite checks cover it.

        Returns the pass's progress, or None to use the merge queue."""
        context, members = self.direct_context, record["members"]
        if not context or "direct" in record or "refused" in record or not members:
            return None
        base, tip, target = record["base"], record["tip"], refs[self.target]
        self.fetch_commits([target, base, tip])
        short = tip[:12]
        landed = counted(record, 0, len(members))
        ancestor = self.repo.run("merge-base", "--is-ancestor", tip, target, check=False)
        if ancestor.returncode == 0:
            print(f"Landed batch {short} directly with {len(members)} source PRs.", flush=True)
            self.finish(refs, lock, landed, members)
            return True
        if record.get("isolated_sibling") and self.repo.out("rev-parse", tip + "^{tree}") != self.repo.out("rev-parse", members[0][1] + "^{tree}"):
            return self.queue_instead(lock, record, "isolated source requires its exact combined tree")
        if target != base:
            return self.queue_instead(lock, record, f"{self.branch} moved since the batch was built")
        try:
            verify_sources(self.repository, self.branch, members, self.context)
        except Ineligible:
            return None
        sources = pull_many(self.repository, [n for n, _ in members])
        for number, head in members:
            pr = sources[number][0]
            if pr["reviewDecision"] not in (None, "APPROVED"):
                return self.queue_instead(lock, record, f"#{number} is not approved")
            status = trusted_status(self.repository, pr, head, context)
            state = status and status.get("state")
            if state == "SUCCESS":
                continue
            if state is None:
                return self.queue_instead(lock, record, f"#{number} has no {context} status")
            return self.queue_instead(lock, record, f"#{number}'s {context} is {state}")
        names = lambda a, b: sorted(changed_paths(self.repo.path, a, b))
        footprints = []
        for number, head in members:
            start = self.repo.out("merge-base", head, base)
            footprints.append((number, names(start, head), names(start, base), names(head, tip)))
        why = direct_overlap(self.branch, footprints, names(base, tip))
        if why:
            return self.queue_instead(lock, record, why)
        return self.land(refs, lock, record)

    def direct_batch(self, refs, lock, record, pr):
        """Land a batch whose PR passed the full-suite status at its exact tip on the current target.

        The tree that was tested is then the tree that lands. Returns the pass's progress, or None."""
        context = self.direct_context
        if not context or "refused" in record or refs.get(self.target) != record["base"]:
            return None
        status = trusted_status(self.repository, pr, record["tip"], context)
        if not status or status.get("state") != "SUCCESS":
            return None
        return self.land(refs, lock, record)

    def land(self, refs, lock, record):
        """Push the batch tip to the target in the same atomic push that closes the batch."""
        members, short = record["members"], record["tip"][:12]
        edits = self.finish_edits(refs, lock, counted(record, 0, len(members)), members)
        try:
            self.push([*edits, (self.target, record["base"], record["tip"])])
        except RuntimeError as error:
            why = f"{self.branch} refused lit's push: {refusal(str(error))}"
            print(f"Batch {short} goes through the merge queue: {why}", flush=True)
            self.push([(self.active, lock, self.record(dict(record, refused=why), lock))])
            return True
        log_round(record, now(), True)
        listed = ", ".join(f"#{n}" for n, _ in members)
        print(
            f"Landed batch {short} directly on {self.branch} with {len(members)} source PRs: {listed}.",
            flush=True,
        )
        return True

    def blame(self, number, record, trusted):
        """Who leaves a failed batch, from what its failing test files read (see split_failed)."""
        members = record["members"]
        run = (trusted.get("targetUrl") or "").rsplit("/", 1)[-1]
        failed, changes = {}, None
        try:
            changes = member_changes(self.repo, record)
            try:
                listing = github.rest(
                    "GET",
                    f"repos/{self.repository}/actions/runs",
                    dict(event="merge_group", per_page=100),
                )
                runs = queue_runs((listing or {}).get("workflow_runs") or [], self.branch, number)
            except (github.GitHubError, OSError, ValueError, KeyError, TypeError):
                runs = []
            if not runs and run.isdigit():
                runs = [run]
            for each in runs:
                with tempfile.TemporaryDirectory(prefix="lit-receipts-") as directory:
                    failed.update(run_receipts(self.repository, each, directory))
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

    def plan_close(self, lock, record, remove, pending, reject=()):
        # Persist the split before closing. A second coordinator or a restart can
        # finish the same transition without returning healthy sources by mistake.
        # `reject` names sources proven red at these heads; finish records them.
        plan = dict(remove=remove, pending=pending)
        if reject:
            plan["reject"] = list(reject)
        next_record = dict(record, closing=plan)
        self.push([(self.active, lock, self.record(next_record, lock))])

    def labelled(self, label):
        """Open PRs into this branch carrying the label, oldest first: the batcher's readiness signal."""
        rows = github.rest_pages(
            f"repos/{self.repository}/issues",
            dict(state="open", labels=label, sort="created", direction="asc"),
        )
        return [row["number"] for row in rows if "pull_request" in row]

    def take_over(self, numbers, label):
        """The label hands a PR to lit, so lit owns its merge. Turn off native auto-merge on a
        labelled PR that GitHub has not queued yet; one already in the queue is left to finish."""
        for n, (pr, _) in pull_many(self.repository, numbers).items():
            if (
                not pr
                or not pr["autoMergeRequest"]
                or pr["isInMergeQueue"]
                or pr["state"] != "OPEN"
                or pr["baseRefName"] != self.branch
                or pr["isCrossRepository"]
                or (pr.get("headRepository") or {}).get("nameWithOwner") != self.repository
            ):
                continue
            disable_auto_merge(pr["id"])
            github.rest(
                "POST",
                f"repos/{self.repository}/issues/{n}/comments",
                body=dict(body=TAKEOVER_NOTE.format(label=label)),
            )
            print(f"Took over #{n}: turned off its auto-merge so lit can batch it.", flush=True)

    def tick(self, label):
        """One pass of the batcher: pin every ready labelled PR, then move the intake as far as
        it can go without waiting. A scheduler (cron, a CI trigger) calls this again later."""
        numbers = self.labelled(label)
        if numbers:
            self.take_over(numbers, label)
            try:
                self.register(numbers)
            except Ineligible as reason:
                print(f"Nothing new to pin: {reason}", flush=True)
        else:
            print(f"No open PRs carry the {label!r} label.", flush=True)
        self.drain(wait=False)

    def drain(self, wait=True):
        for attempt in range(4):
            try:
                return self._drain(wait)
            except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
                if attempt == 3:
                    raise
                print(
                    f"Re-reading the durable intake after a failed operation: {error}",
                    flush=True,
                )
                time.sleep(2**attempt)

    def start_parallel(self, refs, lock, record, parallel):
        groups = record["remaining"]
        active = {n for n, _ in record["members"]}
        suspects = [member for group in [record["members"], *groups] for member in group]
        held = {n for n, _ in suspects}
        base = refs[self.target]
        roots = {h for r, h in refs.items() if r.startswith(("refs/heads/lit/rejected/", "refs/heads/lit/prior-rejections/"))}
        roots.update(h for _, h in suspects)
        self.fetch_commits([base, *sorted(roots)])
        ancestry = {h: set(self.repo.out("rev-list", h, "^" + base).splitlines()) for h in roots}
        rejected = set().union(*(ancestry[h] for r, h in refs.items()
                                 if r.startswith(("refs/heads/lit/rejected/", "refs/heads/lit/prior-rejections/"))))
        pins = sorted((int(m[1]), h) for r, h in refs.items()
                      if (m := re.fullmatch(r"refs/heads/lit/ready/([1-9]\d*)", r)))
        candidates = [m for m in pins if m[0] not in held] + [m for group in groups for m in group]
        base = refs[self.target]
        for n, head in candidates:
            if refs.get(pin_ref(n)) != head or n in active:
                continue
            self.fetch_commits([base, head])
            commits = set(self.repo.out("rev-list", head, "^" + base).splitlines())
            if not commits.isdisjoint(rejected) or any(not commits.isdisjoint(ancestry[oid]) for other, oid in suspects if other != n):
                continue
            prepared = self.assemble(refs, [(n, head)], 1)
            if not prepared:
                continue
            remaining = [[m for m in group if tuple(m) != (n, head)] for group in groups]
            updated = dict(record, remaining=[g for g in remaining if g])
            primary, self.active = self.active, parallel
            try:
                self.claim(refs, *prepared, isolated_sibling=n in held, guard=[(primary, lock, self.record(updated, lock))])
            finally:
                self.active = primary
            return True
        return False

    def advance_parallel(self, refs, parallel, lock):
        record = self.state(lock)
        if len(record["members"]) != 1 or record["remaining"]:
            raise Ineligible("Invalid parallel singleton record")
        primary, limit = self.active, self.max_batch
        self.active, self.max_batch = parallel, 1
        try:
            return self.advance(refs, lock, record)
        finally:
            self.active, self.max_batch = primary, limit

    def _drain(self, wait=True):
        # With wait=False this returns where it would sleep; the next call resumes from the refs.
        while True:
            refs = self.refs()
            lock = refs.get(self.active, "")
            record = self.state(lock) if lock else {}
            parallel = "refs/heads/lit/parallel/" + self.branch
            companion = refs.get(parallel)
            if companion and self.original_prs_only:
                raise Ineligible("A parallel wrapper is active. Withdraw its admission before submitting originals.")
            if companion:
                try:
                    if self.advance_parallel(refs, parallel, companion):
                        continue
                except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError) as why:
                    print(f"Parallel lane held: {why}", flush=True)
                    if wait and "tip" not in record and not record.get("remaining"):
                        raise
                if "tip" not in record and not record.get("remaining"):
                    if not wait:
                        return
                    time.sleep(2)
                    continue
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
                moved = self.drain_originals(refs, lock, record)
                if not moved:
                    return
                if moved == "waiting":
                    if not wait:
                        print("Original PRs are still in GitHub's queue; the next pass resumes.", flush=True)
                        return
                    time.sleep(2)
                continue
            if "tip" in record:
                if not self.advance(refs, lock, record):
                    if not companion and record.get("isolating"):
                        try:
                            if self.start_parallel(refs, lock, record, parallel):
                                continue
                        except Ineligible as why:
                            print(f"Parallel lane held: {why}", flush=True)
                    if not wait:
                        print(f"Batch {record['tip'][:12]} is still being checked; the next pass resumes.", flush=True)
                        return
                    time.sleep(30)
                continue
            groups = record.get("remaining", [])
            if not companion and groups and not record.get("after_fresh"):
                # Alternate: a ready PR that never failed goes before the next suspect group.
                suspects = {n for group in groups for n, _ in group}
                fresh = [
                    (n, head)
                    for n, head in sorted(
                        (int(ref.rsplit("/", 1)[1]), head)
                        for ref, head in refs.items()
                        if re.fullmatch(r"refs/heads/lit/ready/[1-9]\d*", ref)
                    )
                    if n not in suspects
                ]
                prepared = self.assemble(refs, fresh, batch_limit(self.max_batch, record)) if fresh else None
                if prepared:
                    self.claim(refs, *prepared, remaining=groups, red=record.get("red"), fresh=True)
                    continue
            if groups:
                prepared = self.assemble(refs, groups[0])
            else:
                prepared = self.assemble(refs, None, batch_limit(self.max_batch, record))
            if prepared:
                self.claim(refs, *prepared, remaining=groups[1:], red=record.get("red"), isolating=bool(groups))
            elif groups:
                self.finish(refs, lock, record, pending=groups[1:])
            else:
                print("No eligible source requests remain.", flush=True)
                return


def main(
    argv=None, *, repository, branch="main", context, original_prs_only=False, max_batch=None,
    direct_context=None,
):
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
    drain = cmd.add_parser("drain", help="Resume admission from the durable Git refs")
    drain.add_argument(
        "--no-wait",
        action="store_true",
        help="Return instead of sleeping while GitHub checks a batch; call again to resume",
    )
    tick = cmd.add_parser(
        "tick",
        help="Batcher pass: pin every ready PR carrying --label, then drain without waiting",
    )
    tick.add_argument("--label", default="lit-ready")
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
    api = Intake(
        repository, branch, context, repo, original_prs_only=original_prs_only, max_batch=max_batch,
        direct_context=direct_context,
    )
    if args.action == "withdraw":
        if not re.fullmatch(r"[0-9a-f]{40}", args.head):
            parser.error("withdraw requires the exact 40-hex source head")
        api.push([(pin_ref(args.number), args.head, "")])
        return
    if args.action == "report":
        found = github.rest("GET", f"repos/{repository}/pulls/{args.number}")
        pr = dict(
            createdAt=found["created_at"],
            headRefOid=found["head"]["sha"],
            headRefName=found["head"]["ref"],
            baseRefName=found["base"]["ref"],
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
    if args.action == "tick":
        api.tick(args.label)
        return
    if args.action == "ready":
        api.register(args.numbers)
    api.drain(wait=not getattr(args, "no_wait", False))
