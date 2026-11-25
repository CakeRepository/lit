import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from concurrent_merge.intake import Intake
from concurrent_merge.engine import Repo
from concurrent_merge.intake_audit import attempts, queue_visits, source_latency
from concurrent_merge.intake_guard import (
    eligibility,
    manifest,
    batch_name,
    Ineligible,
    pull_many,
    verify_sources,
)


class IntakeTests(unittest.TestCase):
    def test_native_admission_resumes_after_a_partial_request(self):
        intake = Intake("owner/repo", "main", "trusted", Mock(), original_prs_only=True)
        started = "2026-11-23T00:00:00Z"
        heads = {n: str(n) * 40 for n in range(1, 6)}
        members = list(heads.items())
        record = {"originals": [(n, h, started) for n, h in members]}
        refs = {f"refs/heads/lit/ready/{n}": h for n, h in members}
        sources = {
            n: (
                {
                    "id": f"PR{n}",
                    "number": n,
                    "headRefOid": h,
                    "isInMergeQueue": n == 1,
                },
                h,
            )
            for n, h in members
        }
        # Already queued, previously removed, newly ready, pin withdrawn, new unresolved review.
        sources[4] = (sources[4][0], None)
        removals = {"nodes": [{"createdAt": started}]}

        def timeline(query, variables):
            events = removals if variables["n"] == 2 else {"nodes": []}
            return {"repository": {"pullRequest": {"timelineItems": events}}}

        with (
            patch("concurrent_merge.intake.pull_many", return_value=sources),
            patch("concurrent_merge.intake.graphql", side_effect=timeline),
            patch(
                "concurrent_merge.intake.eligibility",
                side_effect=lambda r, b, p, h, c: "unresolved review"
                if p["number"] == 5
                else None,
            ),
            patch("concurrent_merge.intake.enqueue") as enqueue,
            patch.object(intake, "finish") as finish,
        ):
            self.assertTrue(intake.drain_originals(refs, "lock", record))
        enqueue.assert_called_once_with("PR3", heads[3])
        finish.assert_called_once_with(refs, "lock", record, members)

    def test_native_intake_persists_heads_before_submitting_any_original(self):
        intake = Intake("owner/repo", "main", "trusted", Mock(), original_prs_only=True)
        refs = {
            intake.target: "a" * 40,
            "refs/heads/lit/ready/1": "b" * 40,
            "refs/heads/lit/ready/2": "c" * 40,
        }
        with (
            patch.object(
                intake, "refs", side_effect=[refs, RuntimeError("interrupted")]
            ),
            patch.object(intake, "fetch_commits"),
            patch.object(intake, "record", return_value="lock") as save,
            patch.object(intake, "push") as push,
            patch.object(intake, "assemble") as wrapper,
            patch("concurrent_merge.intake.enqueue") as enqueue,
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                intake._drain()
        self.assertEqual(
            [(n, h) for n, h, _ in save.call_args.args[0]["originals"]],
            [(1, "b" * 40), (2, "c" * 40)],
        )
        push.assert_called_once_with([(intake.active, "", "lock")])
        wrapper.assert_not_called()
        enqueue.assert_not_called()

    def test_cold_git_fetch_transfers_multiple_exact_heads_and_warm_read_is_local(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Repo(Path(temp) / "source.git")
            destination = Repo(Path(temp) / "destination.git")
            for repo in (source, destination):
                subprocess.run(
                    ["git", "init", "--quiet", "--bare", repo.path], check=True
                )
            tree = source.out("mktree", data=b"")
            heads = [
                source.out(
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.org",
                    "commit-tree",
                    tree,
                    "-m",
                    m,
                )
                for m in ("one", "two")
            ]
            for i, head in enumerate(heads):
                source.run("update-ref", f"refs/heads/source{i}", head)
            intake = Intake("owner/repo", "main", "trusted", destination)
            intake.url = str(source.path)
            with patch.object(destination, "run", wraps=destination.run) as git:
                intake.fetch_commits([*heads, heads[0]])
                self.assertEqual(
                    [call.args[0] for call in git.call_args_list], ["cat-file", "fetch"]
                )
                git.reset_mock()
                intake.fetch_commits(list(reversed(heads)))
                self.assertEqual(
                    [call.args[0] for call in git.call_args_list], ["cat-file"]
                )
                with self.assertRaises(Ineligible):
                    intake.fetch_commits([tree])

    def test_cached_git_verification_rejects_changed_lock_members(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        row = dict(base="base", tip="tip", members=[[1, "head"]])
        api.repo.out.side_effect = [
            json.dumps(row),
            json.dumps(row),
            json.dumps(dict(row, members=[[2, "other"]])),
        ]
        with (
            patch.object(api, "fetch_commits"),
            patch(
                Intake.__module__ + ".manifest", return_value=[(1, "head")]
            ) as reconstruct,
        ):
            api.state("first")
            api.state("second")
            with self.assertRaises(Ineligible):
                api.state("tampered")
            reconstruct.assert_called_once()

    def test_bulk_reads_bind_every_pin_and_page_to_its_source(self):
        def threads(more=False, resolved=True):
            return dict(
                nodes=[dict(isResolved=resolved)],
                pageInfo=dict(hasNextPage=more, endCursor="cursor"),
            )

        def respond(query, variables):
            if "after" in variables:
                self.assertEqual((variables["n"], variables["after"]), (9, "cursor"))
                return {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": "9",
                            "reviewThreads": threads(resolved=False),
                        }
                    }
                }
            data = {}
            for n in range(1, 28):
                if f"p{n}:pullRequest(" in query:
                    data[f"p{n}"] = dict(
                        number=n, headRefOid=str(n), reviewThreads=threads(more=n == 9)
                    )
                    data[f"r{n}"] = {"target": {"oid": str(n)}}
            return {"repository": data}

        module = pull_many.__module__
        with patch(module + ".graphql", side_effect=respond) as read:
            rows = pull_many("owner/repo", list(range(1, 28)) + [1])
            self.assertEqual(read.call_count, 3)
            self.assertEqual(
                [n for n, (pr, pin) in rows.items() if pr["unresolvedReview"]], [9]
            )
            self.assertTrue(
                all(pin == pr["headRefOid"] == str(n) for n, (pr, pin) in rows.items())
            )
        with patch(module + ".graphql") as read:
            with self.assertRaises(Ineligible):
                pull_many("owner/repo", ["1"])
            read.assert_not_called()
        with patch(module + ".pull_many", return_value={1: (None, "replaced")}):
            with self.assertRaisesRegex(Ineligible, "withdrawn or replaced"):
                verify_sources("owner/repo", "main", [(1, "pinned")], "trusted")

    def test_bulk_review_pagination_rejects_source_change(self):
        first = dict(
            number=2,
            headRefOid="old",
            reviewThreads=dict(
                nodes=[], pageInfo=dict(hasNextPage=True, endCursor="c")
            ),
        )
        with patch(
            pull_many.__module__ + ".graphql",
            side_effect=[
                {"repository": {"p2": first, "r2": None}},
                {"repository": {"pullRequest": {"headRefOid": "new"}}},
            ],
        ):
            with self.assertRaisesRegex(Ineligible, "changed"):
                pull_many("owner/repo", [2])

    def test_predecessor_conflict_keeps_same_batch_and_failure_still_splits(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        module = Intake.__module__
        members = [[1, "a"], [2, "b"]]
        refs = {api.target: "base"}
        record = dict(
            members=members,
            remaining=[],
            tip="tip",
            started="2026-11-23T00:00:00+00:00",
        )
        pr = dict(
            id="P",
            number=3,
            state="OPEN",
            headRefOid="tip",
            baseRefName="main",
            isDraft=False,
            isInMergeQueue=False,
            reviewDecision=None,
            mergeStateStatus="CLEAN",
        )
        event = dict(id="removal1", reason="merge_conflict")
        timeline = {
            "repository": {"pullRequest": {"timelineItems": {"nodes": [event]}}}
        }
        with (
            patch.object(api, "batch_pr", return_value=pr),
            patch(module + ".verify_sources"),
            patch(module + ".pull", return_value=(pr, None)),
            patch(module + ".graphql", return_value=timeline),
            patch(module + ".trusted_status", return_value={"state": "SUCCESS"}),
            patch(module + ".enqueue") as enqueue,
            patch.object(
                api,
                "queue_boundary",
                return_value={
                    "pullRequest": {"number": 4},
                    "headCommit": {"oid": "prior"},
                },
            ) as boundary,
            patch.object(api, "preview_conflict", return_value=True),
            patch.object(api, "plan_close") as close,
            patch.object(api, "record", return_value="next") as save,
            patch.object(api, "push") as push,
        ):
            self.assertFalse(api.advance(refs, "lock", record))
            close.assert_not_called()
            enqueue.assert_not_called()
            boundary.return_value = None
            self.assertTrue(api.advance(refs, "lock", record))
            persisted = save.call_args.args[0]
            push.assert_called_once_with([(api.active, "lock", "next")])
            self.assertFalse(api.advance(refs, "next", persisted))
            enqueue.assert_called_once_with("P", "tip")
            event["id"] = "removal2"
            self.assertTrue(api.advance(refs, "next", persisted))
            close.assert_called_once_with("next", persisted, members, [])
            event["reason"] = "failed_checks"
            api.advance(refs, "next", persisted)
            close.assert_called_with(
                "next", persisted, [], [[members[0]], [members[1]]]
            )

    def test_source_latency_uses_uninterrupted_trusted_success_before_merge(self):
        module = source_latency.__module__

        def row(time, state):
            return dict(
                created_at=f"2026-11-23T00:{time}Z",
                state=state,
                context="check",
                creator={"login": "github-actions[bot]"},
                target_url="https://github.com/owner/repo/actions/runs/123",
            )

        pr = dict(merged_at="2026-11-23T00:04:00Z", head={"sha": "pin"})
        rows = [
            row("05:00", "success"),
            row("03:30", "success"),
            row("02:00", "success"),
            row("01:59", "pending"),
            row("00:00", "success"),
        ]
        with patch(module + ".api", side_effect=[pr, rows, pr]):
            timing = source_latency("owner/repo", 1, "pin", "check", True)
            self.assertEqual(timing["trusted_green_to_merge_seconds"], 120)
            self.assertIs(timing["within_120_seconds"], True)
            self.assertIsNone(
                source_latency("owner/repo", 1, "another", "check", True)[
                    "within_120_seconds"
                ]
            )
        rows[1]["creator"] = {"login": "other"}
        with patch(module + ".api", side_effect=[pr, rows]):
            self.assertIsNone(
                source_latency("owner/repo", 1, "pin", "check", True)[
                    "trusted_green_at"
                ]
            )

    def test_queue_visits_keep_native_rejections_and_current_wait(self):
        def event(what, minute, **kwargs):
            return dict(
                __typename=what + "MergeQueueEvent",
                createdAt=f"2026-11-23T00:{minute}:00Z",
                **kwargs,
            )

        def page(events, more):
            return {
                "repository": {
                    "pullRequest": {
                        "isInMergeQueue": True,
                        "timelineItems": {
                            "nodes": events,
                            "pageInfo": {"hasNextPage": more, "endCursor": "cursor"},
                        },
                    }
                }
            }

        with patch(
            queue_visits.__module__ + ".graphql",
            side_effect=[
                page(
                    [
                        event("AddedTo", "00"),
                        event("RemovedFrom", "03", reason="merge_conflict"),
                    ],
                    True,
                ),
                page([event("AddedTo", "05")], False),
            ],
        ):
            visits = queue_visits("owner/repo", 2, None, "2026-11-23T00:09:00Z")
            self.assertEqual(
                [(v["seconds"], v["reason"]) for v in visits],
                [(180, "merge_conflict"), (240, "observed_open")],
            )

    def test_source_rules_apply_to_the_same_revision(self):
        repo, head, context = "owner/repo", "1" * 40, "trusted"
        pr = dict(
            state="OPEN",
            isDraft=False,
            baseRefName="main",
            headRefOid=head,
            headRefName="agent/topic",
            headRepository={"nameWithOwner": repo},
            isCrossRepository=False,
            unresolvedReview=False,
            reviewDecision="APPROVED",
            mergeStateStatus="CLEAN",
            isInMergeQueue=False,
            autoMergeRequest=None,
            commits={
                "nodes": [
                    {
                        "commit": {
                            "oid": head,
                            "status": {
                                "contexts": [
                                    {
                                        "context": context,
                                        "state": "SUCCESS",
                                        "creator": {"login": "github-actions"},
                                        "targetUrl": "https://github.com/owner/repo/actions/runs/42",
                                    }
                                ]
                            },
                        }
                    }
                ]
            },
        )
        self.assertIsNone(eligibility(repo, "main", pr, head, context))
        for change in [
            dict(isDraft=True),
            dict(isCrossRepository=True),
            dict(headRefOid="2" * 40),
            dict(reviewDecision="CHANGES_REQUESTED"),
            dict(unresolvedReview=True),
            dict(isInMergeQueue=True),
            dict(autoMergeRequest={"enabledAt": "then"}),
            dict(mergeStateStatus="BLOCKED"),
            dict(commits={"nodes": [{"commit": {"oid": head, "status": None}}]}),
        ]:
            with self.subTest(change=change):
                self.assertTrue(
                    eligibility(repo, "main", dict(pr, **change), head, context)
                )

    def test_manifest_reproduces_each_tree_in_the_pinned_chain(self):
        store = Mock()
        base, source, middle, second, tip = [c * 40 for c in "abcde"]
        store.out.return_value = f"{middle} {base} {source}\n{tip} {middle} {second}"
        tree = b"1" * 40
        store.objects.return_value = [
            b"tree "
            + tree
            + f"\n\nBatch PR #{n}\n\nPull-Request: #{n}\nPR-Author: dev\nPR-Head: {head}\n".encode()
            for n, head in [(3, source), (4, second)]
        ]
        ok = b"1\0" + tree + b"\0\0"
        for data in [
            ok + ok,
            b"1\0" + b"2" * 40 + b"\0\0" + ok,
            b"0\0" + tree + b"\0path\nwith-newline\0\0" + ok,
            ok,
            ok + ok + ok,
            (ok + ok)[:-1],
        ]:
            with self.subTest(data=data):
                store.run.return_value = Mock(stdout=data)
                if data == ok + ok:
                    self.assertEqual(
                        manifest(store, batch_name(base, tip), tip, base),
                        [(3, source), (4, second)],
                    )
                else:
                    with self.assertRaises(Ineligible):
                        manifest(store, batch_name(base, tip), tip, base)

    def test_release_cannot_remove_replaced_pins(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        refs = {
            api.active: "lock",
            "refs/heads/lit/ready/1": "new",
            "refs/heads/lit/ready/2": "b",
        }
        with patch.object(api, "push") as push:
            api.finish(refs, "lock", {}, [(1, "a"), (2, "b")])
            push.assert_called_once_with(
                [("refs/heads/lit/ready/2", "b", ""), (api.active, "lock", "")]
            )

    def test_failures_persist_the_split_before_closing(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        record = dict(members=[[1, "a"], [2, "b"]], remaining=[], tip="t")
        with (
            patch.object(api, "record", return_value="planned") as write,
            patch.object(api, "push") as push,
        ):
            api.plan_close("lock", record, [], [[[1, "a"]], [[2, "b"]]])
            self.assertEqual(
                write.call_args.args[0]["closing"]["pending"], [[[1, "a"]], [[2, "b"]]]
            )
            push.assert_called_once_with([(api.active, "lock", "planned")])

    def test_singleton_reuses_the_source_pr_and_recovers_an_existing_submission(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        record = dict(
            individual=True, members=[[12, "a"]], started="2026-11-23T00:00:00+00:00"
        )
        pr = dict(id="P", headRefOid="a", isInMergeQueue=False)
        prior = {"repository": {"pullRequest": {"timelineItems": {"nodes": []}}}}
        module = Intake.__module__
        with (
            patch(module + ".pull", return_value=(pr, "a")),
            patch(module + ".eligibility", return_value=None),
            patch(module + ".graphql", return_value=prior),
            patch(module + ".enqueue") as enqueue,
            patch.object(api, "finish") as finish,
            patch.object(api, "batch_pr") as wrapper,
        ):
            api.advance({}, "lock", record)
            enqueue.assert_called_once_with("P", "a")
            pr["isInMergeQueue"] = True
            api.advance({}, "lock", record)
            pr["isInMergeQueue"] = False
            prior["repository"]["pullRequest"]["timelineItems"]["nodes"] = [
                {"createdAt": "2026-11-23T00:10:00Z"}
            ]
            api.advance({}, "lock", record)
            self.assertEqual(enqueue.call_count, 1)
            self.assertEqual(finish.call_count, 3)
            wrapper.assert_not_called()

    def test_run_count_includes_retries_and_individual_duplicates(self):
        def run(n, attempts):
            return dict(
                id=n,
                head_branch=f"gh-readonly-queue/main/pr-{n}-" + "0" * 40,
                head_sha=str(n),
                html_url="https://example.org",
                conclusion="success",
                run_attempt=attempts,
            )

        rows = attempts([run(3, 2), run(1, 1), run(9, 5)], [1, 2], 3)
        self.assertEqual(sum(r["attempts"] for r in rows), 3)
