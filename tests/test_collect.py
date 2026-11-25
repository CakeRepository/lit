import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from concurrent_merge import cli
from concurrent_merge.engine import Engine, Repo, commit_message
from concurrent_merge.inputs import verified_changes

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
    """Answers `gh api graphql` with fixed pages and records each request."""

    def __init__(self, pages, base=BASE, key="pullRequests"):
        self.pages, self.base, self.key, self.calls = pages, base, key, []

    def __call__(self, args):
        self.calls.append(args)
        after = next((a[6:] for a in args if a.startswith("after=")), None)
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
        return json.dumps(
            dict(data=dict(repository=dict(ref=dict(target=dict(oid=base)), **body)))
        ).encode()


def collect(pages, selection, pattern=None, **fake):
    github = FakeGitHub(pages, **fake)
    with patch.object(cli.subprocess, "check_output", github):
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
        missing = lambda args: json.dumps(
            dict(data=dict(repository=dict(ref=dict(target=dict(oid=BASE)), mergeQueue=None)))
        ).encode()
        with patch.object(cli.subprocess, "check_output", missing):
            with self.assertRaisesRegex(ValueError, "--prs, --label or --min-reviews"):
                cli.collect("owner/repo", "main")

    def test_bad_selection_values(self):
        with self.assertRaises(ValueError):
            cli.selection_of([0])
        with self.assertRaises(ValueError):
            cli.selection_of(min_reviews=-1)
        self.assertEqual(cli.selection_of(), {})


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
        with patch.object(cli.subprocess, "check_output", github):
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
