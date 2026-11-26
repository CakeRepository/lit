import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from concurrent_merge import cli
from concurrent_merge.github import GitHubError
from concurrent_merge.engine import Engine, Repo, commit_message
from concurrent_merge.inputs import verified_changes
from concurrent_merge.proposal import propose

BASE = "a" * 40
IDENTITY = dict(
    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
)


def pr(number, head=None, **extra):
    row = dict(
        number=number,
        headRefOid=head or f"{number:040x}",
        baseRefName="main",
        isDraft=False,
        author={"login": "shared-bot"},
        body="",
        labels={"nodes": []},
        latestOpinionatedReviews={"nodes": []},
    )
    row.update(extra)
    return row


def review(login, oid, state="APPROVED"):
    return dict(state=state, author={"login": login}, commit={"oid": oid})


class FakeGitHub:
    """Answers GraphQL requests with fixed pages and records each request."""

    def __init__(self, pages, base=BASE, key="pullRequests"):
        self.pages, self.base, self.key, self.calls = pages, base, key, []

    def __call__(self, query, variables=None, raise_on_errors=True):
        self.calls.append(variables)
        after = (variables or {}).get("after")
        index = int(after) if after else 0
        nodes = self.pages[index]
        info = dict(hasNextPage=index + 1 < len(self.pages), endCursor=str(index + 1))
        if self.key == "pullRequests":
            body = dict(pullRequests=dict(pageInfo=info, nodes=nodes))
        else:
            entries = [
                dict(state="AWAITING_CHECKS", headCommit=None, pullRequest=dict(p, state="OPEN"))
                for p in nodes
            ]
            body = dict(mergeQueue=dict(entries=dict(pageInfo=info, nodes=entries)))
        base = self.base(index) if callable(self.base) else self.base
        return dict(data=dict(repository=dict(ref=dict(target=dict(oid=base)), **body)))


def collect(pages, selection, pattern=None, **fake):
    github = FakeGitHub(pages, **fake)
    with patch.object(cli.github, "graphql", github):
        return cli.collect("owner/repo", "main", selection, pattern), github


class CollectWithoutMergeQueueTests(unittest.TestCase):
    def test_prs_by_number_across_pages(self):
        snapshot, github = collect(
            [[pr(133), pr(7)], [pr(134), pr(135)]], cli.selection_of([135, 133, 134])
        )
        self.assertEqual(len(github.calls), 2)
        self.assertEqual(snapshot["base"], BASE)
        self.assertEqual(snapshot["selection"], {"prs": [133, 134, 135]})
        self.assertEqual([c["number"] for c in snapshot["changes"]], [133, 134, 135])
        self.assertEqual(
            snapshot["changes"][0],
            dict(number=133, head=f"{133:040x}", author="shared-bot"),
        )
        self.assertEqual(snapshot["groups"], [])

    def test_named_pr_that_is_closed_draft_or_elsewhere_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Not open against main: #9"):
            collect([[pr(1)]], cli.selection_of([1, 9]))
        with self.assertRaisesRegex(ValueError, "Draft or not matching.*#2"):
            collect([[pr(1), pr(2, isDraft=True)]], cli.selection_of([1, 2]))

    def test_label_picks_only_labelled_ready_prs(self):
        ready = {"nodes": [{"name": "lit-ready"}]}
        snapshot, _ = collect(
            [[pr(1, labels=ready), pr(2), pr(3, labels=ready, isDraft=True)]],
            cli.selection_of(label="lit-ready"),
        )
        self.assertEqual([c["number"] for c in snapshot["changes"]], [1])

    def test_min_reviews_counts_distinct_approvals_on_the_current_head(self):
        head = "b" * 40
        reviews = lambda *r: {"nodes": list(r)}
        snapshot, _ = collect(
            [
                [
                    pr(1, head, latestOpinionatedReviews=reviews(review("x", head), review("y", head))),
                    pr(2, head, latestOpinionatedReviews=reviews(review("x", head), review("y", "c" * 40))),
                    pr(3, head, latestOpinionatedReviews=reviews(review("x", head), review("y", head, "CHANGES_REQUESTED"))),
                ]
            ],
            cli.selection_of(min_reviews=2),
        )
        self.assertEqual([c["number"] for c in snapshot["changes"]], [1])

    def test_rules_combine(self):
        ready = {"nodes": [{"name": "lit-ready"}]}
        snapshot, _ = collect(
            [[pr(1, labels=ready), pr(2, labels=ready), pr(3)]],
            cli.selection_of([1], label="lit-ready"),
        )
        self.assertEqual([c["number"] for c in snapshot["changes"]], [1])
        with self.assertRaisesRegex(ValueError, "#3"):
            collect(
                [[pr(1, labels=ready), pr(2, labels=ready), pr(3)]],
                cli.selection_of([1, 3], label="lit-ready"),
            )

    def test_target_moving_between_pages_stops_collection(self):
        with self.assertRaisesRegex(ValueError, "Target advanced"):
            collect(
                [[pr(1)], [pr(2)]],
                cli.selection_of(label="x"),
                base=lambda index: BASE if index == 0 else "d" * 40,
            )

    def test_no_merge_queue_points_at_the_other_rules(self):
        missing = lambda *a, **k: dict(
            data=dict(repository=dict(ref=dict(target=dict(oid=BASE)), mergeQueue=None))
        )
        with patch.object(cli.github, "graphql", missing):
            with self.assertRaisesRegex(ValueError, "--prs, --label or --min-reviews"):
                cli.collect("owner/repo", "main")

    def test_bad_selection_values(self):
        with self.assertRaises(ValueError):
            cli.selection_of([0])
        with self.assertRaises(ValueError):
            cli.selection_of(min_reviews=-1)
        self.assertEqual(cli.selection_of(), {})


def rest_pr(number, head=None, **extra):
    row = dict(
        number=number,
        head={"sha": head or f"{number:040x}"},
        base={"ref": "main"},
        draft=False,
        user={"login": "shared-bot"},
        body=None,
        labels=[],
    )
    row.update(extra)
    return row


class FakeREST:
    """Refuses GraphQL the way a proxy does and answers REST GET paths."""

    def __init__(self, pulls, reviews=None, targets=(BASE,)):
        self.pulls, self.reviews = pulls, reviews or {}
        self.targets, self.calls = list(targets), []

    def graphql(self, query, variables=None, raise_on_errors=True):
        self.calls.append(["graphql"])
        raise GitHubError("GraphQL is not available", 403)

    def rest(self, method, path, query=None, body=None):
        self.calls.append([method, path])
        assert method == "GET"
        query = query or {}
        if path.endswith("/git/ref/heads/main"):
            oid = self.targets.pop(0) if len(self.targets) > 1 else self.targets[0]
            return dict(object=dict(sha=oid, type="commit"))
        if "/git/ref/heads/" in path:
            raise GitHubError("Not Found", 404)
        rows = self.pulls if path.endswith("/pulls") else self.reviews[int(path.split("/")[4])]
        if path.endswith("/pulls"):
            assert query["state"] == "open" and query["base"] == "main", query
        size, page = int(query["per_page"]), int(query["page"])
        return rows[(page - 1) * size : page * size]

    def patched(self):
        stack = ExitStack()
        stack.enter_context(patch.object(cli.github, "graphql", self.graphql))
        stack.enter_context(patch.object(cli.github, "rest", self.rest))
        return stack


def rest_collect(fake, selection, pattern=None, api=None):
    env = {"LIT_GITHUB_API": api} if api else {}
    with fake.patched(), patch.dict(cli.os.environ, env):
        with patch("sys.stderr"):
            return cli.collect("owner/repo", "main", selection, pattern)


class RestFallbackTests(unittest.TestCase):
    def test_refused_graphql_falls_back_to_rest_with_the_same_rules(self):
        ready = [{"name": "lit-ready"}]
        fake = FakeREST(
            [
                rest_pr(1, labels=ready, body="Bot: agent-a"),
                rest_pr(2, labels=ready, draft=True),
                rest_pr(3),
                rest_pr(4, labels=ready, base={"ref": "other"}),
            ]
        )
        snapshot = rest_collect(fake, cli.selection_of(label="lit-ready"), r"^Bot: (.+)$")
        self.assertEqual(snapshot["base"], BASE)
        self.assertEqual(
            snapshot["changes"],
            [dict(number=1, head=f"{1:040x}", author="shared-bot", agent="agent-a")],
        )
        self.assertEqual(snapshot["selection"], {"label": "lit-ready"})
        self.assertEqual(fake.calls[0][0], "graphql")
        self.assertFalse(any("/reviews" in c[-1] for c in fake.calls))

    def test_rest_pages_through_every_open_pr(self):
        fake = FakeREST([rest_pr(n) for n in range(1, 151)])
        snapshot = rest_collect(fake, cli.selection_of([2, 150]), api="rest")
        self.assertEqual([c["number"] for c in snapshot["changes"]], [2, 150])
        self.assertFalse(any(c[0] == "graphql" for c in fake.calls))
        with self.assertRaisesRegex(ValueError, "Not open against main: #151"):
            rest_collect(FakeREST([rest_pr(1)]), cli.selection_of([1, 151]), api="rest")

    def test_rest_counts_each_reviewers_latest_approval_on_the_current_head(self):
        head, old = "b" * 40, "c" * 40

        def review(login, state, commit=head):
            return dict(user={"login": login}, state=state, commit_id=commit)

        fake = FakeREST(
            [rest_pr(1, head), rest_pr(2, head), rest_pr(3, head), rest_pr(4, head)],
            reviews={
                1: [review("x", "APPROVED"), review("y", "COMMENTED"), review("y", "APPROVED")],
                2: [review("x", "APPROVED"), review("y", "APPROVED", old)],
                3: [review("x", "APPROVED"), review("y", "APPROVED"), review("y", "CHANGES_REQUESTED")],
                4: [review("x", "APPROVED"), review("y", "APPROVED"), review("y", "DISMISSED")],
            },
        )
        snapshot = rest_collect(fake, cli.selection_of(min_reviews=2))
        self.assertEqual([c["number"] for c in snapshot["changes"]], [1])

    def test_rest_target_moving_during_collection_stops_it(self):
        fake = FakeREST([rest_pr(1)], targets=(BASE, "d" * 40))
        with self.assertRaisesRegex(ValueError, "Target advanced"):
            rest_collect(fake, cli.selection_of([1]), api="rest")

    def test_missing_branch_over_rest(self):
        fake = FakeREST([rest_pr(1)])
        with fake.patched(), patch.dict(cli.os.environ, {"LIT_GITHUB_API": "rest"}):
            with self.assertRaisesRegex(ValueError, "Branch dev not found"):
                cli.collect("owner/repo", "dev", cli.selection_of([1]))

    def test_graphql_only_does_not_fall_back(self):
        with self.assertRaises(GitHubError):
            rest_collect(FakeREST([rest_pr(1)]), cli.selection_of([1]), api="graphql")
        with self.assertRaisesRegex(ValueError, "LIT_GITHUB_API"):
            rest_collect(FakeREST([]), cli.selection_of([1]), api="soap")

    def test_merge_queue_without_graphql_points_at_rest_rules(self):
        with FakeREST([]).patched():
            with self.assertRaisesRegex(ValueError, "also work over REST"):
                cli.collect("owner/repo", "main")


class OutsideContributorTests(unittest.TestCase):
    def test_hold_outside_keeps_outside_prs_out_until_allowed(self):
        pages = [[
            pr(1, authorAssociation="MEMBER"),
            pr(2, authorAssociation="CONTRIBUTOR"),
            pr(3, authorAssociation="FIRST_TIME_CONTRIBUTOR"),
            pr(4),
        ]]
        with patch("sys.stderr") as err:
            snapshot, _ = collect(pages, cli.selection_of([1, 2, 3, 4], hold_outside=True))
        self.assertEqual([c["number"] for c in snapshot["changes"]], [1])
        self.assertIn("#2, #3, #4", "".join(c.args[0] for c in err.write.call_args_list))
        selection = cli.selection_of([1, 2, 3], hold_outside=True, allow_outside=[3])
        with patch("sys.stderr"):
            snapshot, _ = collect(pages, selection)
        self.assertEqual([c["number"] for c in snapshot["changes"]], [1, 3])
        self.assertEqual(snapshot["selection"]["allow_outside"], [3])

    def test_without_hold_every_author_lands_as_before(self):
        snapshot, _ = collect([[pr(1, authorAssociation="NONE")]], cli.selection_of([1]))
        self.assertEqual([c["number"] for c in snapshot["changes"]], [1])

    def test_rest_reads_author_association(self):
        fake = FakeREST([rest_pr(1, author_association="OWNER"), rest_pr(2, author_association="NONE")])
        snapshot = rest_collect(fake, cli.selection_of([1, 2], hold_outside=True), api="rest")
        self.assertEqual([c["number"] for c in snapshot["changes"]], [1])

    def test_allow_outside_needs_hold_and_a_pick(self):
        with self.assertRaisesRegex(ValueError, "needs --hold-outside"):
            cli.selection_of([1], allow_outside=[1])
        with self.assertRaisesRegex(ValueError, "Pick changes"):
            collect([[pr(1)]], cli.selection_of(hold_outside=True))


class AgentOverrideTests(unittest.TestCase):
    PATTERN = r"^Agents Hangout bot: (.+)$"

    def test_pattern_reads_agent_from_pr_body(self):
        body = "Summary\n\nAgents Hangout bot: Croakie Cupcake\n"
        snapshot, _ = collect(
            [[pr(1, body=body), pr(2, body="no line here")]],
            cli.selection_of([1, 2]),
            self.PATTERN,
        )
        first, second = snapshot["changes"]
        self.assertEqual(first["agent"], "Croakie Cupcake")
        self.assertEqual(first["author"], "shared-bot")
        self.assertNotIn("agent", second)

    def test_pattern_applies_to_the_merge_queue_too(self):
        github = FakeGitHub([[pr(5, body="Agents Hangout bot: Ribbit Red Velvet")]], key="queue")
        with patch.object(cli.github, "graphql", github):
            snapshot = cli.queue("owner/repo", "main", self.PATTERN)
        self.assertEqual(snapshot["changes"][0]["agent"], "Ribbit Red Velvet")

    def test_pattern_without_group_uses_whole_match_and_rejects_multiline_names(self):
        self.assertEqual(cli.agent_name("bot-7 wrote this", r"bot-\d+"), "bot-7")
        self.assertIsNone(cli.agent_name("x", None))
        self.assertIsNone(cli.agent_name("name: \x07bell", r"name: (.+)"))

    def test_cli_rejects_an_invalid_pattern(self):
        args = ["concurrent-merge", "collect", "owner/repo", "--out", "q.json",
                "--repo", "batch.git", "--prs", "1", "--author-pattern", "("]
        with patch.object(sys, "argv", args), patch.object(cli, "collect") as called:
            with patch("sys.stderr"):
                self.assertNotEqual(cli.main(), 0)
            called.assert_not_called()


class AgentInSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "batch.git"
        subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
        self.repo = Repo(path)
        tree = self.repo.out("mktree", data=b"")
        self.base = self.repo.out("commit-tree", tree, data=b"base\n", env=IDENTITY)

    def tearDown(self):
        self.tmp.cleanup()

    def head(self, name):
        blob = self.repo.out("hash-object", "-w", "--stdin", data=name.encode())
        tree = self.repo.out("mktree", data=f"100644 blob {blob}\t{name}\n".encode())
        return self.repo.out(
            "commit-tree", tree, "-p", self.base, data=name.encode(), env=IDENTITY
        )

    def test_agent_overrides_login_and_keeps_it_as_a_trailer(self):
        snapshot = dict(
            base=self.base,
            changes=[
                dict(number=1, head=self.head("a"), author="shared-bot", agent="Croakie Cupcake"),
                dict(number=2, head=self.head("b"), author="shared-bot", agent="Tadpole Tiramisu"),
                dict(number=3, head=self.head("c"), author="shared-bot"),
            ],
        )
        changes = {p["number"]: p for p in verified_changes(self.repo, snapshot)}
        self.assertEqual(changes[1]["author"], "Croakie Cupcake")
        self.assertEqual(changes[1]["login"], "shared-bot")
        self.assertEqual(changes[3]["author"], "shared-bot")
        self.assertNotIn("login", changes[3])
        message = commit_message(changes[1])
        self.assertIn("PR-Author: Croakie Cupcake\nPR-Login: shared-bot\n", message)
        self.assertNotIn("PR-Login", commit_message(changes[3]))

    def test_failure_goes_back_to_the_agent(self):
        snapshot = dict(
            base=self.base,
            changes=[
                dict(number=1, head=self.head("a"), author="shared-bot", agent="Croakie Cupcake"),
                dict(number=2, head=self.head("b"), author="shared-bot", agent="Tadpole Tiramisu"),
            ],
        )
        changes = verified_changes(self.repo, snapshot)

        def test(oid):
            files = self.repo.out("ls-tree", "--name-only", oid).split()
            return "fail" if "b" in files else "pass"

        with tempfile.TemporaryDirectory() as state:
            engine = Engine(self.repo, Path(state))
            _, passed = engine.batch(self.base, changes, test)
        self.assertEqual(passed, {1})
        returned = [a["returned"] for a in engine.history if "returned" in a]
        self.assertEqual([(r["number"], r["author"]) for r in returned], [(2, "Tadpole Tiramisu")])

    def test_landing_rechecks_the_same_selection(self):
        head = self.head("a")
        selection = {"prs": [1]}
        snapshot = dict(
            repository="owner/repo", branch="main", base=self.base, selection=selection,
            changes=[dict(number=1, head=head, author="bot")], groups=[],
        )
        run = Path(self.tmp.name) / "runs" / "batch-x"
        run.mkdir(parents=True)
        receipt = run / "result.json"
        receipt.write_text(json.dumps(dict(snapshot=snapshot, final=head, tested=[1])))
        self.repo.run("update-ref", "refs/concurrent-merge/tested/batch-x", head)
        moved = dict(snapshot, changes=[dict(number=1, head=self.head("b"), author="bot")])
        with patch.object(cli, "collect", return_value=moved) as live:
            with self.assertRaisesRegex(ValueError, "eligibility changed"):
                cli.land(argparse.Namespace(receipt=receipt, repo=self.repo.path))
        live.assert_called_once_with("owner/repo", "main", selection)

    def proposable(self):
        remote = Repo(Path(self.tmp.name) / "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote.path)], check=True)
        destination = "https://github.com/owner/repo.git"
        self.repo.run("config", "url." + remote.path.as_uri() + ".insteadOf", destination)
        self.repo.run("push", "-q", destination, self.base + ":refs/heads/main")
        head = self.head("a")
        changes = verified_changes(
            self.repo, dict(base=self.base, changes=[dict(number=7, head=head, author="bot")])
        )
        with tempfile.TemporaryDirectory() as state:
            final, passed = Engine(self.repo, Path(state)).batch(self.base, changes, lambda _: "pass")
        snapshot = dict(
            repository="owner/repo", branch="main", base=self.base, selection={"prs": [7]},
            changes=[dict(number=7, head=head, author="bot")], groups=[],
        )
        run = Path(self.tmp.name) / "runs" / "batch-p"
        run.mkdir(parents=True)
        receipt = run / "result.json"
        receipt.write_text(json.dumps(dict(snapshot=snapshot, final=final, tested=sorted(passed))))
        self.repo.run("update-ref", "refs/concurrent-merge/tested/batch-p", final)
        args = argparse.Namespace(receipt=receipt, repo=self.repo.path, head="lit/batch-1", title=None, notes=None)
        return args, snapshot, remote, final

    def test_propose_rechecks_the_selection_without_a_merge_queue(self):
        args, snapshot, remote, final = self.proposable()
        reads = []

        def live(repository, branch, selection=None):
            reads.append(selection)
            return snapshot

        bodies = []

        def graphql(query, variables=None, raise_on_errors=True):
            if "createPullRequest" in query:
                self.assertIn("draft:$draft", query.replace(" ", ""))
                self.assertIs(variables["draft"], True)
                bodies.append(variables["body"])
                return dict(data=dict(createPullRequest=dict(pullRequest=dict(url="https://github.com/owner/repo/pull/9"))))
            if "pullRequests" in query:
                return dict(data=dict(repository=dict(pullRequests=dict(nodes=[]))))
            return dict(data=dict(repository=dict(id="R_1")))

        with patch("concurrent_merge.github.graphql", side_effect=graphql):
            with patch("sys.stdout"):
                propose(args, live)
        self.assertEqual(reads, [{"prs": [7]}])
        self.assertEqual(remote.out("rev-parse", "refs/heads/lit/batch-1"), final)
        self.assertIn("lit combined #7 for review. Each source", bodies[0])
        self.assertNotIn("merge queue", bodies[0])

    def test_propose_without_a_matching_pr_says_so(self):
        args, snapshot, _, _ = self.proposable()
        gone = dict(snapshot, changes=[])
        with self.assertRaisesRegex(ValueError, "#7 no longer matches the collected selection"):
            propose(args, lambda *_: gone)

    def test_propose_falls_back_to_rest_for_the_pr(self):
        args, snapshot, remote, final = self.proposable()
        calls = []

        def graphql(query, variables=None, raise_on_errors=True):
            calls.append(["graphql"])
            raise GitHubError("GraphQL is not available", 403)

        def rest(method, path, query=None, body=None):
            calls.append([method, path, query, body])
            if method == "GET":
                other = dict(number=10, head=dict(ref="lit/batch-10", sha="e" * 40, repo=dict(full_name="owner/repo")),
                             state="open", html_url="x", merged_at=None)
                return [other]
            self.assertIn("lit combined #7 for review.", body["body"])
            self.assertIs(body["draft"], True)
            self.assertEqual(body["head"], "lit/batch-1")
            return dict(html_url="https://github.com/owner/repo/pull/9")

        with patch("concurrent_merge.github.graphql", side_effect=graphql):
            with patch("concurrent_merge.github.rest", side_effect=rest):
                with patch("sys.stdout") as out, patch("sys.stderr"):
                    propose(args, lambda *_: snapshot)
        self.assertEqual([c[0] for c in calls], ["graphql", "GET", "graphql", "POST"])
        self.assertEqual(calls[1][2]["head"], "owner:lit/batch-1")
        self.assertEqual(remote.out("rev-parse", "refs/heads/lit/batch-1"), final)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("https://github.com/owner/repo/pull/9", printed)

    def test_invalid_agent_is_refused(self):
        for agent in ["", "   ", "two\nlines", "x" * 101, 7]:
            snapshot = dict(
                base=self.base,
                changes=[dict(number=1, head=self.head("a"), author="bot", agent=agent)],
            )
            with self.assertRaisesRegex(ValueError, "Invalid agent"):
                verified_changes(self.repo, snapshot)


if __name__ == "__main__":
    unittest.main()
