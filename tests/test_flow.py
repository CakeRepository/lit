import unittest

from concurrent_merge.blame import suspects, verdict
from concurrent_merge.flow import Change, simulate


class BlameTests(unittest.TestCase):
    failed = {
        "tests/a.test.ts": dict(tracked=["src/a.ts", "src/db.ts"], listings=["migrations"], absent=["src/a.local.ts"]),
    }

    def test_only_a_change_the_failing_file_saw_is_a_suspect(self):
        changes = {"1": {"src/b.ts"}, "2": {"src/db.ts"}, "3": {"docs/x.md"}}
        self.assertEqual(verdict(self.failed, changes)["action"], "eject")
        self.assertEqual(verdict(self.failed, changes)["eject"], ["2"])
        self.assertEqual(verdict(self.failed, changes)["keep"], ["1", "3"])

    def test_probed_paths_and_listed_directories_count(self):
        changes = {"1": {"src/a.local.ts"}, "2": {"migrations/0042.sql"}, "3": {"src/c.ts"}}
        self.assertEqual(suspects(self.failed, changes)["tests/a.test.ts"], ["1", "2"])
        self.assertEqual(verdict(self.failed, changes)["action"], "bisect")
        self.assertEqual(verdict(self.failed, changes)["keep"], ["3"])

    def test_a_failure_no_change_explains_is_decided_on_the_base(self):
        result = verdict(self.failed, {"1": {"src/b.ts"}})
        self.assertEqual(result["action"], "rerun-on-base")
        self.assertEqual(result["bisect"], ["1"])

    def test_an_incomplete_trace_suspects_every_change(self):
        failed = {"t": dict(tracked=[], incomplete=["used io_uring"])}
        self.assertEqual(suspects(failed, {"1": {"x"}, "2": {"y"}})["t"], ["1", "2"])


class FlowTests(unittest.TestCase):
    def test_one_failure_restarts_every_change_behind_it(self):
        changes = [Change("a", 0, fails=1), Change("b", 1), Change("c", 2)]
        result = simulate(changes, seconds=100, slots=3)
        self.assertIsNone(result["waits"]["a"])
        self.assertEqual(result["waits"]["b"], 199)  # restarted when a failed at 100
        self.assertEqual(result["runs"], 5)

    def test_groups_land_together_and_attribution_retests_only_the_rest(self):
        changes = [Change(str(i), i, fails=1 if i == 2 else 0) for i in range(4)]
        result = simulate(changes, seconds=100, slots=1, max_group=4, on_failure="attribute")
        self.assertEqual(result["runs"], 3)  # 0 alone, then 1-3 fail, then 1 and 3
        self.assertEqual(result["waits"]["3"], 297)

    def test_bisect_splits_until_the_failing_change_is_alone(self):
        changes = [Change(str(i), 0, fails=1 if i == 3 else 0) for i in range(4)]
        result = simulate(changes, seconds=100, slots=4, max_group=4, on_failure="bisect")
        self.assertEqual(result["landed"], 3)
        self.assertEqual(result["waits"]["0"], 200)
        self.assertEqual(result["waits"]["2"], 300)


if __name__ == "__main__":
    unittest.main()
