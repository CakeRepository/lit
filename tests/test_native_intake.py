"""Exercise the protected queue against two private remotes and recorded GitHub observations."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from concurrent_merge.engine import Engine, Repo
from concurrent_merge.inputs import verified_changes
from e2e_github import git, lit

REPO = "owner/repo"
CONTEXT = "trusted"
WHEN = "2023-11-14T22:13:00Z"
EPOCH = "1700000000"


@unittest.skipUnless(os.environ.get("LIT_BIN"), "compares native intake against Python")
class IntakeCLI(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.remote = self.root / "intake-remote.git"
        work = self.root / "work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        (work / "README").write_text("base")
        git(work, "add", ".")
        git(work, "commit", "-qm", "base")
        self.base = git(work, "rev-parse", "HEAD")
        git(self.root, "init", "--bare", "-q", "-b", "main", str(self.remote))
        git(work, "push", str(self.remote), "main")
        self.prs = {}
        self.members = []
        for n in (1, 2):
            git(work, "checkout", "-q", "-B", f"pr-{n}", self.base)
            (work / f"feature-{n}").write_text(str(n))
            git(work, "add", ".")
            git(work, "commit", "-qm", f"topic {n}")
            head = git(work, "rev-parse", "HEAD")
            git(work, "push", str(self.remote), f"HEAD:refs/pull/{n}/head")
            self.members.append([n, head])
            self.prs[n] = self.pr(n, head)
        repo = Repo(self.remote)
        snapshot = dict(base=self.base, changes=[dict(number=n, head=h, author="dev") for n, h in self.members])
        with patch.dict(os.environ, SOURCE_DATE_EPOCH=EPOCH):
            self.tip, _ = Engine(repo, self.root / "model").batch(self.base, verified_changes(repo, snapshot), lambda _: "model")
        self.prs[77] = self.pr(77, self.tip)
        self.branch = f"codex/ci/lit-v1/{self.base}/{self.tip}"
        self.rules = []

    def pr(self, n, head):
        status = dict(context=CONTEXT, state="SUCCESS", createdAt=WHEN, creator=dict(login="github-actions"),
                      targetUrl=f"https://github.com/{REPO}/actions/runs/123")
        return dict(number=n, id=f"pr{n}", state="OPEN", isDraft=False, isCrossRepository=False,
                    headRefOid=head, headRefName=f"pr-{n}", baseRefName="main", mergeStateStatus="CLEAN",
                    reviewDecision=None, isInMergeQueue=False, autoMergeRequest=None, mergeQueueEntry=None,
                    author=dict(login="dev"), headRepository=dict(nameWithOwner=REPO),
                    reviewThreads=dict(nodes=[], pageInfo=dict(hasNextPage=False, endCursor=None)),
                    commits=dict(nodes=[dict(commit=dict(oid=head, status=dict(contexts=[status])))]))

    def get(self, path, reply):
        self.rules.append(dict(method="GET", path=f"/repos/{REPO}/{path}", reply=reply))

    def gql(self, query, data, variables=None):
        row = dict(method="POST", path="/graphql", query_contains=query, reply=dict(data=data))
        if variables is not None:
            row["variables"] = variables
        self.rules.append(row)

    def pulls(self, numbers):
        data = {}
        for n in numbers:
            pr = self.prs[n]
            data[f"p{n}"] = pr
            data[f"r{n}"] = dict(target=dict(oid=pr["headRefOid"]))
        self.gql(f"p{numbers[0]}:pullRequest", dict(repository=data))

    def parity(self, *args):
        (self.root / "intake-api.json").write_text(json.dumps(self.rules))
        run = lit(self.root, {"LIT_GITHUB_API": "graphql"}, "parity", "--keep", "intake", REPO,
                  "--context", CONTEXT, "--repo-dir", "cache.git", *args)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("matches between Python and Rust (exit 0)", run.stdout)
        path = Path(run.stdout.split("both runs are in ")[1].splitlines()[0])
        self.addCleanup(shutil.rmtree, path, True)
        self.assertFalse((self.root / "cache.git").exists())
        return path / "rust"

    def test_tick_pins_both_prs_and_enqueues_only_the_combined_head(self):
        self.get("issues?state=open&labels=lit-ready&sort=created&direction=asc&per_page=100&page=1",
                 [dict(number=n, pull_request={}) for n, _ in self.members])
        self.full_suite(1, state="PENDING")
        self.pulls([1, 2])
        self.pulls([77])
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=77, state="OPEN", headRefOid=self.tip, isCrossRepository=False, url="fixture")
        ]))))
        prior = self.rules[-1]
        prior["replies"] = [dict(data=dict(repository=dict(pullRequests=dict(nodes=[])))), prior.pop("reply")]
        self.gql("repository(owner:$o,name:$n){id}", dict(repository=dict(id="repo-id")))
        body = ("lit collected the following ready source revisions before native queue admission. "
                "The protected merge-group workflow validates the combined change.\n\n"
                + "".join(f"- #{n} at `{head}`\n" for n, head in self.members))
        self.gql("draft:$draft", dict(createPullRequest=dict(pullRequest=dict(url="fixture"))),
                 dict(repo="repo-id", base="main", head=self.branch, title="lit batch for #1, #2",
                      body=body, draft=False))
        self.gql("REMOVED_FROM_MERGE_QUEUE_EVENT", dict(repository=dict(pullRequest=dict(timelineItems=dict(nodes=[])))))
        self.gql("mergeQueue(branch:", dict(repository=dict(mergeQueue=dict(entries=dict(nodes=[])))))
        self.gql("enqueuePullRequest", dict(enqueuePullRequest=dict(mergeQueueEntry=dict(position=1))),
                 dict(pr="pr77", head=self.tip))
        side = self.parity("--max-batch", "auto", "--direct-context", "full", "tick")
        remote = side / "intake-remote.git"
        record = json.loads(git(remote, "show", "-s", "--format=%B", "refs/heads/lit/active/main"))
        self.assertEqual(record["direct"], "#1's full is PENDING")
        self.assertEqual(record["members"], self.members)
        self.assertEqual(record["tip"], self.tip)
        self.assertEqual(git(remote, "rev-parse", "refs/heads/" + self.branch), self.tip)
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/lit"), "")

    def lock(self, record, parent=None, ref="refs/heads/lit/active/main"):
        parent = parent or self.tip
        tree = git(self.remote, "rev-parse", parent + "^{tree}")
        lock = git(self.remote, "commit-tree", tree, "-p", parent, "-m", json.dumps(record))
        git(self.remote, "update-ref", ref, lock)

    def refs(self, side, prefix):
        return git(side / "intake-remote.git", "for-each-ref", "--format=%(refname) %(objectname)", prefix)

    def test_a_closing_batch_swaps_the_rejected_pin_for_a_rejection_at_the_same_head(self):
        git(self.remote, "update-ref", "refs/heads/" + self.branch, self.tip)
        prior = self.extra_pr(3)[1]
        git(self.remote, "update-ref", "refs/heads/lit/rejected/1", prior)
        for n, head in self.members:
            git(self.remote, "update-ref", f"refs/heads/lit/ready/{n}", head)
        # Source 1 failed alone; source 2 only rode along and is returned healthy.
        rejected = self.members[:1]
        self.lock(dict(base=self.base, tip=self.tip, members=self.members, remaining=[], started=WHEN,
                       closing=dict(remove=self.members, pending=[], reject=rejected)))
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=77, state="OPEN", headRefOid=self.tip, isCrossRepository=False, url="fixture")
        ]))))
        self.rules.append(dict(method="PATCH", path=f"/repos/{REPO}/pulls/77", reply={}))
        side = self.parity("drain", "--no-wait")
        self.assertEqual(self.refs(side, "refs/heads/lit").splitlines(), [
            f"refs/heads/lit/prior-rejections/{prior} {prior}",
            f"refs/heads/lit/rejected/1 {self.members[0][1]}",
        ])

    def test_tick_holds_out_a_rejected_head_batches_the_rest_and_readmits_after_a_new_push(self):
        (first, head1), (second, head2) = self.members
        git(self.remote, "update-ref", f"refs/heads/lit/rejected/{first}", head1)
        # Only source 2 is batched; the rejected source 1 is skipped with a line saying why.
        work = self.root / "work"
        git(work, "checkout", "-q", "-B", "solo", self.base)
        (work / "feature-2").write_text("2")
        git(work, "add", ".")
        git(work, "commit", "-qm", "topic 2")
        repo = Repo(self.remote)
        snapshot = dict(base=self.base, changes=[dict(number=second, head=head2, author="dev")])
        with patch.dict(os.environ, SOURCE_DATE_EPOCH=EPOCH):
            solo, _ = Engine(repo, self.root / "model").batch(self.base, verified_changes(repo, snapshot), lambda _: "model")
        self.prs[78] = self.pr(78, solo)
        self.get("issues?state=open&labels=lit-ready&sort=created&direction=asc&per_page=100&page=1",
                 [dict(number=n, pull_request={}) for n, _ in self.members])
        self.pulls([1, 2])
        self.pulls([second])
        self.pulls([78])
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=78, state="OPEN", headRefOid=solo, isCrossRepository=False, url="fixture")
        ]))))
        self.gql("REMOVED_FROM_MERGE_QUEUE_EVENT", dict(repository=dict(pullRequest=dict(timelineItems=dict(nodes=[])))))
        self.gql("mergeQueue(branch:", dict(repository=dict(mergeQueue=dict(entries=dict(nodes=[])))))
        self.gql("enqueuePullRequest", dict(enqueuePullRequest=dict(mergeQueueEntry=dict(position=1))),
                 dict(pr="pr78", head=solo))
        side = self.parity("--max-batch", "auto", "tick")
        remote = side / "intake-remote.git"
        record = json.loads(git(remote, "show", "-s", "--format=%B", "refs/heads/lit/active/main"))
        self.assertEqual(record["members"], [self.members[1]])
        self.assertEqual(self.refs(side, "refs/heads/lit/rejected"), f"refs/heads/lit/rejected/1 {head1}")
        self.assertNotIn("refs/heads/lit/ready/1", self.refs(side, "refs/heads/lit"))
        # parity() also proves both implementations print the same lines, skip notice included.

    def test_a_pushed_head_replaces_the_stale_rejection_and_is_pinned(self):
        (first, head1), _ = self.members
        previous = self.members[1][1]
        git(self.remote, "update-ref", f"refs/heads/lit/rejected/{first}", previous)
        self.get("issues?state=open&labels=lit-ready&sort=created&direction=asc&per_page=100&page=1",
                 [dict(number=first, pull_request={})])
        self.pulls([first])
        self.gql("REMOVED_FROM_MERGE_QUEUE_EVENT", dict(repository=dict(pullRequest=dict(timelineItems=dict(nodes=[])))))
        self.gql("enqueuePullRequest", dict(enqueuePullRequest={}), dict(pr="pr1", head=head1))
        side = self.parity("--original-prs-only", "tick")
        self.assertEqual(self.refs(side, "refs/heads/lit/rejected"), "")
        self.assertEqual(self.refs(side, "refs/heads/lit"), f"refs/heads/lit/prior-rejections/{previous} {previous}")

    def test_originals_and_exact_head_withdrawal(self):
        for n, head in self.members:
            git(self.remote, "update-ref", f"refs/heads/lit/ready/{n}", head)
        self.pulls([1, 2])
        self.gql("REMOVED_FROM_MERGE_QUEUE_EVENT", dict(repository=dict(pullRequest=dict(timelineItems=dict(nodes=[])))))
        for n, head in self.members:
            self.gql("enqueuePullRequest", dict(enqueuePullRequest={}), dict(pr=f"pr{n}", head=head))
        side = self.parity("--original-prs-only", "drain", "--no-wait")
        self.assertEqual(git(side / "intake-remote.git", "for-each-ref", "--format=%(refname)", "refs/heads/lit"), "")
        side = self.parity("withdraw", "1", self.members[0][1])
        self.assertEqual(git(side / "intake-remote.git", "for-each-ref", "--format=%(refname)", "refs/heads/lit"),
                         "refs/heads/lit/ready/2")
        # Run the normal CLI with no Python available, against the fixture remote itself.
        env = {"LIT_PYTHON": str(self.root / "unavailable"), "GIT_ALLOW_PROTOCOL": "file",
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{self.remote}.insteadOf",
               "GIT_CONFIG_VALUE_0": f"https://github.com/{REPO}.git"}
        native = lit(self.root, env, "intake", REPO, "--context", CONTEXT, "--repo-dir", "native.git",
                     "withdraw", "1", self.members[0][1])
        self.assertEqual(native.returncode, 0, native.stdout + native.stderr)
        stale = lit(self.root, env, "intake", REPO, "--context", CONTEXT, "--repo-dir", "native.git",
                    "withdraw", "2", self.members[0][1])
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/lit/ready/2"), self.members[1][1])


    def test_report_counts_a_landed_batch_retries_and_trusted_source_latencies(self):
        from urllib.parse import urlencode
        git(self.remote, "update-ref", "refs/pull/77/head", self.tip)
        git(self.remote, "update-ref", "refs/heads/main", self.tip)
        merged = "2023-11-14T22:13:18Z"
        self.get("pulls/77", dict(base=dict(ref="main"), head=dict(sha=self.tip, ref=self.branch), created_at=WHEN))

        def page(endpoint, entries, **fields):
            query = urlencode(dict(fields, created=">=" + WHEN, per_page=100, page=1))
            self.get(endpoint + "?" + query, dict(total_count=len(entries), workflow_runs=entries))

        protected = dict(id=100, run_attempt=2, head_sha=self.tip, conclusion="success", html_url="fixture",
                         head_branch=f"gh-readonly-queue/main/pr-77-{self.tip}")
        page("actions/workflows/trusted-pr-validation.yml/runs", [protected], event="merge_group")
        page("actions/runs", [], branch=self.branch)
        self.gql("statusCheckRollup", dict(repository=dict(pullRequest=dict(state="MERGED",
                 mergeCommit=dict(oid=self.tip), mergedAt=merged, url="https://github.com/owner/repo/pull/77",
                 headRefName=self.branch, commits=dict(nodes=[dict(commit=dict(statusCheckRollup=None))])))))
        for n, h in self.members:
            self.get(f"pulls/{n}", dict(merged_at=merged, head=dict(sha=h)))
            self.get(f"commits/{h}/statuses?per_page=100&page=1",
                     [dict(context=CONTEXT, state="success", created_at=WHEN, creator=dict(login="github-actions[bot]"),
                           target_url=f"https://github.com/{REPO}/actions/runs/103")])
        self.gql("timelineItems(first:100", dict(repository=dict(pullRequest=dict(isInMergeQueue=False,
                 timelineItems=dict(nodes=[dict(__typename="AddedToMergeQueueEvent", createdAt=WHEN)],
                                    pageInfo=dict(hasNextPage=False, endCursor=None))))))
        self.parity("report", "77", "--since", WHEN)

    def test_empty_tick_and_resuming_a_persisted_closure(self):
        self.get("issues?state=open&labels=lit-ready&sort=created&direction=asc&per_page=100&page=1", [])
        self.parity("tick")
        git(self.remote, "update-ref", "refs/heads/" + self.branch, self.tip)
        for n, head in self.members:
            git(self.remote, "update-ref", f"refs/heads/lit/ready/{n}", head)
        record = dict(base=self.base, tip=self.tip, members=self.members, remaining=[], started=WHEN,
                      closing=dict(remove=self.members, pending=[]))
        tree = git(self.remote, "rev-parse", self.tip + "^{tree}")
        lock = git(self.remote, "commit-tree", tree, "-p", self.tip, "-m", json.dumps(record))
        git(self.remote, "update-ref", "refs/heads/lit/active/main", lock)
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=77, state="OPEN", headRefOid=self.tip, isCrossRepository=False, url="fixture")
        ]))))
        self.rules.append(dict(method="PATCH", path=f"/repos/{REPO}/pulls/77", reply={}))
        side = self.parity("drain", "--no-wait")
        self.assertEqual(git(side / "intake-remote.git", "for-each-ref", "--format=%(refname)", "refs/heads/lit"), "")


    def full_suite(self, n, state="SUCCESS"):
        status = dict(self.prs[n]["commits"]["nodes"][0]["commit"]["status"]["contexts"][0], context="full",
                      state=state, targetUrl=f"https://github.com/{REPO}/actions/runs/124")
        self.prs[n]["commits"]["nodes"][0]["commit"]["status"]["contexts"].append(status)

    def pinned_batch(self):
        for n, head in self.members:
            git(self.remote, "update-ref", f"refs/heads/lit/ready/{n}", head)
        self.lock(dict(base=self.base, tip=self.tip, members=self.members, remaining=[], started=WHEN))

    def test_a_batch_whose_prs_passed_the_full_suite_lands_on_main_itself(self):
        self.pinned_batch()
        self.full_suite(1)
        self.full_suite(2)
        self.pulls([1, 2])
        side = self.parity("--direct-context", "full", "drain", "--no-wait")
        remote = side / "intake-remote.git"
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), self.tip)
        self.assertEqual(self.refs(side, "refs/heads/lit"), "")

    def extra_pr(self, n, parent=None):
        work = self.root / f"work{n}"
        git(self.root, "clone", "-q", str(self.remote), str(work))
        if parent:
            git(work, "fetch", "-q", "origin", parent)
        git(work, "checkout", "-q", "-B", f"pr-{n}", parent or self.base)
        (work / f"feature-{n}").write_text(str(n))
        git(work, "add", ".")
        git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"topic {n}")
        head = git(work, "rev-parse", "HEAD")
        git(work, "push", "-q", str(self.remote), f"HEAD:refs/pull/{n}/head")
        self.prs[n] = self.pr(n, head)
        return [n, head]

    def landed_order(self, side):
        log = git(side / "intake-remote.git", "log", "--reverse", "--format=%s", "refs/heads/main")
        return [line.split()[-1] for line in log.splitlines() if line.startswith("topic")]

    def failed_pair(self, fresh):
        """The state after the batch of #1 and #2 failed and was halved, with `fresh` PRs also ready."""
        ready = [*self.members, *(self.extra_pr(n) for n in fresh)]
        for n, head in ready:
            git(self.remote, "update-ref", f"refs/heads/lit/ready/{n}", head)
        for n, _ in ready:
            self.full_suite(n)
        if fresh:
            self.pulls(fresh)  # the fresh round asks about all of them before sizing the batch
        for n, _ in ready:
            self.pulls([n])
        self.lock(dict(remaining=[[self.members[0]], [self.members[1]]]), parent=self.base)

    def test_a_ready_pr_that_never_failed_goes_between_suspect_halves(self):
        self.failed_pair([3, 4])
        side = self.parity("--max-batch", "1", "--direct-context", "full", "drain", "--no-wait")
        self.assertEqual(self.landed_order(side), ["3", "1", "4", "2"])
        self.assertEqual(self.refs(side, "refs/heads/lit"), "")

    def test_the_suspect_half_goes_first_when_no_other_pr_is_ready(self):
        self.failed_pair([])
        side = self.parity("--max-batch", "1", "--direct-context", "full", "drain", "--no-wait")
        self.assertEqual(self.landed_order(side), ["1", "2"])
        self.assertEqual(self.refs(side, "refs/heads/lit"), "")

    def test_a_healthy_singleton_lands_while_the_suspect_wrapper_is_still_checking(self):
        self.parallel_singleton(False)

    def test_a_member_of_the_failed_batch_lands_while_its_sibling_is_still_checking(self):
        self.parallel_singleton(True)

    def parallel_singleton(self, sibling):
        self.pinned_batch()
        parent = self.members[0][1]
        if sibling:
            middle = self.extra_pr(6)[1]
            rejected = self.extra_pr(7, middle)[1]
            git(self.remote, "update-ref", f"refs/heads/lit/prior-rejections/{rejected}", rejected)
            parent = middle
        stacked = self.extra_pr(3, parent)
        fresh = self.extra_pr(4)
        self.lock(dict(base=self.base, tip=self.tip, members=self.members,
                       remaining=[[fresh]] if sibling else [], started=WHEN,
                       direct="no full status", isolating=True))
        for n, head in (stacked, fresh):
            git(self.remote, "update-ref", f"refs/heads/lit/ready/{n}", head)
            self.full_suite(n)
        self.prs[77]["isInMergeQueue"] = True
        self.pulls([1, 2])
        self.pulls([4])
        self.pulls([77])
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=77, state="OPEN", headRefOid=self.tip, isCrossRepository=False, url="fixture")
        ]))))
        side = self.parity("--direct-context", "full", "drain", "--no-wait")
        remote = side / "intake-remote.git"
        self.assertEqual(self.landed_order(side), ["4"])
        record = json.loads(git(remote, "show", "-s", "--format=%B", "refs/heads/lit/active/main"))
        self.assertEqual(record["tip"], self.tip)
        self.assertEqual(record["remaining"], [])
        self.assertEqual(self.refs(side, "refs/heads/lit/parallel"), "")
        self.assertEqual(len(self.refs(side, "refs/heads/lit/ready").splitlines()), 3)

    def test_an_unverifiable_companion_does_not_block_a_reserved_primary_group(self):
        self.failed_pair([])
        self.lock(dict(members=[], remaining=[]), parent=self.base, ref="refs/heads/lit/parallel/main")
        side = self.parity("--direct-context", "full", "drain", "--no-wait")
        self.assertEqual(self.landed_order(side), ["1", "2"])
        self.assertTrue(self.refs(side, "refs/heads/lit/parallel"))

    def test_an_isolated_sibling_with_a_changed_tree_waits_for_protected_validation(self):
        member, (_, base) = self.members
        git(self.remote, "update-ref", "refs/heads/main", base)
        git(self.remote, "update-ref", "refs/heads/lit/ready/1", member[1])
        self.full_suite(1)
        repo = Repo(self.remote)
        snapshot = dict(base=base, changes=[dict(number=1, head=member[1], author="dev")])
        with patch.dict(os.environ, SOURCE_DATE_EPOCH=EPOCH):
            tip, _ = Engine(repo, self.root / "sibling").batch(base, verified_changes(repo, snapshot), lambda _: "model")
        self.lock(dict(base=base, tip=tip, members=[member], remaining=[], started=WHEN, isolated_sibling=True),
                  parent=tip, ref="refs/heads/lit/parallel/main")
        self.prs[78] = dict(self.pr(78, tip), isInMergeQueue=True)
        self.pulls([1])
        self.pulls([78])
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=78, state="OPEN", headRefOid=tip, isCrossRepository=False, url="fixture")
        ]))))
        side = self.parity("--direct-context", "full", "drain", "--no-wait")
        remote = side / "intake-remote.git"
        record = json.loads(git(remote, "show", "-s", "--format=%B", "refs/heads/lit/parallel/main"))
        self.assertEqual(record["direct"], "isolated source requires its exact combined tree")
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), base)

    def test_a_pr_without_the_full_suite_sends_the_batch_through_the_queue(self):
        self.pinned_batch()
        self.full_suite(1)
        self.pulls([1, 2])
        self.pulls([77])
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=77, state="OPEN", headRefOid=self.tip, isCrossRepository=False, url="fixture")
        ]))))
        self.gql("REMOVED_FROM_MERGE_QUEUE_EVENT", dict(repository=dict(pullRequest=dict(timelineItems=dict(nodes=[])))))
        self.gql("mergeQueue(branch:", dict(repository=dict(mergeQueue=dict(entries=dict(nodes=[])))))
        self.gql("enqueuePullRequest", dict(enqueuePullRequest=dict(mergeQueueEntry=dict(position=1))),
                 dict(pr="pr77", head=self.tip))
        side = self.parity("--direct-context", "full", "drain", "--no-wait")
        remote = side / "intake-remote.git"
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), self.base)
        record = json.loads(git(remote, "show", "-s", "--format=%B", "refs/heads/lit/active/main"))
        self.assertEqual(record["direct"], "#2 has no full status")

    def test_a_batch_pr_that_passed_the_full_suite_on_current_main_lands_on_main_itself(self):
        # The members carry only the fast status; the batch PR itself passed the full suite at its tip.
        git(self.remote, "update-ref", "refs/heads/" + self.branch, self.tip)
        for n, head in self.members:
            git(self.remote, "update-ref", f"refs/heads/lit/ready/{n}", head)
        self.lock(dict(base=self.base, tip=self.tip, members=self.members, remaining=[], started=WHEN,
                       direct="#1 has no full status"))
        self.full_suite(77)
        self.pulls([1, 2])
        self.pulls([77])
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=77, state="OPEN", headRefOid=self.tip, isCrossRepository=False, url="fixture")
        ]))))
        self.gql("REMOVED_FROM_MERGE_QUEUE_EVENT", dict(repository=dict(pullRequest=dict(timelineItems=dict(nodes=[])))))
        side = self.parity("--direct-context", "full", "drain", "--no-wait")
        remote = side / "intake-remote.git"
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), self.tip)
        self.assertEqual(self.refs(side, "refs/heads/lit"), "")

    def test_main_refusing_lit_s_push_sends_the_batch_through_the_queue(self):
        self.pinned_batch()
        self.full_suite(1)
        self.full_suite(2)
        self.pulls([1, 2])
        self.pulls([77])
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nwhile read old new ref; do [ \"$ref\" = refs/heads/main ] && "
                        "{ echo 'protected branch' >&2; exit 1; }; done; exit 0\n")
        hook.chmod(0o755)
        self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
            dict(number=77, state="OPEN", headRefOid=self.tip, isCrossRepository=False, url="fixture")
        ]))))
        self.gql("REMOVED_FROM_MERGE_QUEUE_EVENT", dict(repository=dict(pullRequest=dict(timelineItems=dict(nodes=[])))))
        self.gql("mergeQueue(branch:", dict(repository=dict(mergeQueue=dict(entries=dict(nodes=[])))))
        self.gql("enqueuePullRequest", dict(enqueuePullRequest=dict(mergeQueueEntry=dict(position=1))),
                 dict(pr="pr77", head=self.tip))
        side = self.parity("--direct-context", "full", "drain", "--no-wait")
        remote = side / "intake-remote.git"
        self.assertEqual(git(remote, "rev-parse", "refs/heads/main"), self.base)
        record = json.loads(git(remote, "show", "-s", "--format=%B", "refs/heads/lit/active/main"))
        self.assertEqual(record["refused"], "main refused lit's push: protected branch")

    def test_a_failed_batch_returns_the_implicated_source_and_rechecks_the_other(self):
        from zipfile import ZipFile, ZIP_DEFLATED
        first = git(self.remote, "rev-parse", self.tip + "^1")
        git(self.remote, "update-ref", "refs/heads/" + self.branch, self.tip)
        for n, head in self.members:
            git(self.remote, "update-ref", f"refs/heads/lit/ready/{n}", head)
        record = dict(base=self.base, tip=self.tip, members=self.members, remaining=[], started=WHEN)
        tree = git(self.remote, "rev-parse", self.tip + "^{tree}")
        lock = git(self.remote, "commit-tree", tree, "-p", self.tip, "-m", json.dumps(record))
        git(self.remote, "update-ref", "refs/heads/lit/active/main", lock)
        self.prs[77]["commits"]["nodes"][0]["commit"]["status"]["contexts"][0]["state"] = "FAILURE"
        self.prs[78] = self.pr(78, first)
        self.prs[78]["commits"]["nodes"][0]["commit"]["status"]["contexts"][0]["state"] = "PENDING"
        self.pulls([1, 2])
        for n, head in ((77, self.tip), (78, first)):
            self.pulls([n])
            self.gql("pullRequests(baseRefName:", dict(repository=dict(pullRequests=dict(nodes=[
                dict(number=n, state="OPEN", headRefOid=head, isCrossRepository=False, url="fixture")
            ]))), dict(owner="owner", name="repo", base="main", head=f"codex/ci/lit-v1/{self.base}/{head}"))
        self.gql("REMOVED_FROM_MERGE_QUEUE_EVENT", dict(repository=dict(pullRequest=dict(timelineItems=dict(nodes=[])))))
        self.rules.append(dict(method="PATCH", path=f"/repos/{REPO}/pulls/77", reply={}))
        zip_path = f"repos/{REPO}/actions/artifacts/211/zip"
        # The batch PR keeps its fast status (run 123); the failing tests' receipts are in its merge-group run.
        self.get("actions/runs?event=merge_group&per_page=100", dict(workflow_runs=[
            dict(id=456, head_branch=f"gh-readonly-queue/main/pr-77-{self.base}", head_sha="group",
                 status="completed", created_at=WHEN)]))
        self.get("actions/runs/456/artifacts?per_page=100&page=1", dict(artifacts=[
            dict(name="lit-tests", expired=False, archive_download_url=zip_path)]))
        with ZipFile(self.root / "failed.zip", "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("suite/result.json", json.dumps(dict(job="suite", failed_reads={
                "test_2": dict(tracked=["feature-2"], absent=[], listings=[])})))
        self.rules.append(dict(method="GET", path="/" + zip_path, file="failed.zip"))
        side = self.parity("--max-batch", "auto", "drain", "--no-wait")
        remote = side / "intake-remote.git"
        next_batch = json.loads(git(remote, "show", "-s", "--format=%B", "refs/heads/lit/active/main"))
        self.assertEqual(next_batch["red"], [1.0, 1.0])
        self.assertEqual(next_batch["members"], [self.members[0]])
        self.assertEqual(next_batch["tip"], first)
        self.assertEqual(git(remote, "for-each-ref", "--format=%(refname)", "refs/heads/lit/ready"), "refs/heads/lit/ready/1")
