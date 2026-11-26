from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from concurrent_merge import intake
from concurrent_merge.intake import Intake, batch_limit, counted, group_size


class GroupSizeTests(unittest.TestCase):
    def test_hwang_sizes_follow_the_red_share(self):
        self.assertEqual(group_size(None), 32)  # prior: about one in fifty
        self.assertEqual(group_size([10, 100]), 8)  # about one in ten
        self.assertEqual(group_size([1, 100]), 64)  # about one in a hundred
        self.assertEqual(group_size([60, 100]), 1)  # most come back red
        self.assertEqual(group_size([0, 10_000]), intake.MAX_AUTO_BATCH)

    def test_each_pr_counts_once_and_old_counts_fade(self):
        record = counted({}, 0, 32)
        self.assertEqual(record["red"], [0.0, 32.0])
        record = counted(record, 1, 1)
        self.assertEqual(record["red"], [1.0, 26.6])
        self.assertLess(group_size(record["red"]), group_size(None))

    def test_limit_is_none_fixed_or_sized(self):
        self.assertIsNone(batch_limit(None, {}))
        self.assertEqual(batch_limit(5, {"red": [9, 10]}), 5)
        self.assertEqual(batch_limit("auto", {"red": [10, 100]}), 8)


class SizedIntakeTests(unittest.TestCase):
    def pins(self, n):
        return {f"refs/heads/lit/ready/{i}": f"{i:040x}" for i in range(1, n + 1)}

    def test_assemble_takes_the_lowest_numbered_ready_prs_up_to_the_limit(self):
        api = Intake("owner/repo", "main", "trusted", Mock(path=Path("unused.git")))
        refs = {api.target: "f" * 40, **self.pins(5)}
        sources = {i: ({"author": {"login": "a"}}, f"{i:040x}") for i in range(1, 6)}
        with (
            patch.object(intake, "pull_many", return_value=sources),
            patch.object(intake, "eligibility", return_value=None),
            patch.object(api, "fetch_commits"),
            patch.object(intake, "verified_changes", return_value=[]) as verified,
            patch.object(intake, "Engine"),
        ):
            self.assertIsNone(api.assemble(refs, None, limit=3))
        rows = verified.call_args.args[1]["changes"]
        self.assertEqual([r["number"] for r in rows], [1, 2, 3])

    def test_a_fresh_batch_is_sized_but_a_split_group_is_not(self):
        api = Intake("owner/repo", "main", "trusted", Mock(), max_batch="auto")
        with patch.object(api, "refs", return_value={}), patch.object(api, "assemble", return_value=None) as assemble:
            api._drain(wait=False)
        self.assertEqual(assemble.call_args.args[2], 32)
        record = dict(remaining=[[[4, "d"]], [[5, "e"]]], red=[1, 2])
        with (
            patch.object(api, "refs", return_value={api.active: "lock"}),
            patch.object(api, "state", side_effect=[record, {}]),
            patch.object(api, "assemble", side_effect=[None, None]) as assemble,
            patch.object(api, "finish") as finish,
        ):
            api._drain(wait=False)
        self.assertEqual(assemble.call_args_list[0].args, ({api.active: "lock"}, [[4, "d"]]))
        finish.assert_called_once()

    def test_a_never_failed_pr_goes_between_suspect_halves(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        ready = {f"refs/heads/lit/ready/{i}": f"{i:040x}" for i in (1, 2, 3, 4)}
        suspects = [[[1, f"{1:040x}"]], [[2, f"{2:040x}"]]]

        def drain(record, refs):
            """One pass over `record`, then stop at the batch it claims."""
            with (
                patch.object(api, "refs", return_value={api.active: "lock", **refs}),
                patch.object(api, "state", return_value=record),
                patch.object(api, "assemble", return_value=("b", "t")) as assemble,
                patch.object(api, "claim", side_effect=StopIteration) as claim,
            ):
                with self.assertRaises(StopIteration):
                    api._drain(wait=False)
            return assemble.call_args, claim.call_args

        # Fresh PRs #3 and #4 go first, alone, and every suspect group stays queued.
        assembled, claimed = drain(dict(remaining=suspects), ready)
        self.assertEqual(assembled.args[1], [(3, f"{3:040x}"), (4, f"{4:040x}")])
        self.assertEqual(claimed.kwargs["remaining"], suspects)
        self.assertTrue(claimed.kwargs["fresh"])
        # After a fresh round the next one takes the first suspect group, without a marker.
        assembled, claimed = drain(dict(remaining=suspects, after_fresh=True), ready)
        self.assertEqual(assembled.args[1], suspects[0])
        self.assertEqual(claimed.kwargs["remaining"], suspects[1:])
        self.assertFalse(claimed.kwargs.get("fresh"))
        # Without fresh PRs the suspect group is taken at once.
        only = {r: h for r, h in ready.items() if r[-1] in "12"}
        assembled, claimed = drain(dict(remaining=suspects), only)
        self.assertEqual(assembled.args[1], suspects[0])
        self.assertFalse(claimed.kwargs.get("fresh"))

    def test_a_fresh_round_that_assembles_nothing_falls_back_to_the_suspect_group(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        suspects = [[[1, f"{1:040x}"]]]
        refs = {api.active: "lock", "refs/heads/lit/ready/1": f"{1:040x}", "refs/heads/lit/ready/3": f"{3:040x}"}
        with (
            patch.object(api, "refs", return_value=refs),
            patch.object(api, "state", return_value=dict(remaining=suspects)),
            patch.object(api, "assemble", side_effect=[None, ("b", "t")]) as assemble,
            patch.object(api, "claim", side_effect=StopIteration) as claim,
        ):
            with self.assertRaises(StopIteration):
                api._drain(wait=False)
        self.assertEqual([c.args[1] for c in assemble.call_args_list], [[(3, f"{3:040x}")], suspects[0]])
        self.assertFalse(claim.call_args.kwargs.get("fresh"))

    def test_claim_marks_a_fresh_batch_and_its_close_marks_the_next_record(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        with (
            patch.object(api, "fetch_commits"),
            patch.object(intake, "manifest", return_value=[[3, "c"]]),
            patch.object(intake, "verify_sources", return_value={}),
            patch.object(api, "record", return_value="lock") as write,
            patch.object(api, "push"),
        ):
            api.claim({api.target: "m"}, "b", "t", remaining=[[[1, "a"]]], fresh=True)
            self.assertIs(write.call_args.args[0]["fresh"], True)
            api.claim({api.target: "m"}, "b", "t", remaining=[[[1, "a"]]])
            self.assertNotIn("fresh", write.call_args.args[0])
        for record, marked in ((dict(fresh=True), True), (dict(), False)):
            with patch.object(api, "record", return_value="next") as write, patch.object(api, "push"):
                api.finish({}, "lock", record, pending=[[[1, "a"]]])
            self.assertEqual(write.call_args.args[0].get("after_fresh", False), marked)

    def test_claim_carries_the_red_counts_into_the_batch_record(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        with (
            patch.object(api, "fetch_commits"),
            patch.object(intake, "manifest", return_value=[[1, "a"]]),
            patch.object(intake, "verify_sources", return_value={}),
            patch.object(api, "record", return_value="lock") as write,
            patch.object(api, "push"),
        ):
            api.claim({api.target: "m"}, "b", "t", red=[1.0, 20.0])
        self.assertEqual(write.call_args.args[0]["red"], [1.0, 20.0])

    def test_finish_keeps_the_counts_only_when_sizing(self):
        record = dict(red=[1.0, 20.0])
        for max_batch, kept in (("auto", True), (None, False), (8, False)):
            api = Intake("owner/repo", "main", "trusted", Mock(), max_batch=max_batch)
            with patch.object(api, "record", return_value="idle") as write, patch.object(api, "push") as push:
                api.finish({}, "lock", record, pending=[])
            self.assertEqual(push.call_args.args[0][-1], (api.active, "lock", "idle" if kept else ""))
            if kept:
                self.assertEqual(write.call_args.args[0], dict(remaining=[], red=[1.0, 20.0]))

    def test_a_landed_batch_counts_its_members_as_green(self):
        api = Intake("owner/repo", "main", "trusted", Mock(), max_batch="auto")
        record = dict(base="b", tip="t", members=[[1, "a"], [2, "b"]], remaining=[], red=[1.0, 4.0])
        refs = {api.target: "m"}
        with (
            patch.object(api, "batch_pr", return_value=dict(number=9, state="MERGED")),
            patch.object(api, "fetch_commits"),
            patch.object(api, "finish") as finish,
        ):
            self.assertTrue(api.advance(refs, "lock", record))
        self.assertEqual(finish.call_args.args[2]["red"], [0.8, 5.2])


if __name__ == "__main__":
    unittest.main()
