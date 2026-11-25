"""A failed batch sends back only what its failing tests could have seen; one source is a batch too."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from concurrent_merge.engine import Repo
from concurrent_merge.github import GitHubError
from concurrent_merge.intake import Intake, member_changes, run_receipts, split_failed

MEMBERS = [[1, "a"], [2, "b"], [3, "c"], [4, "d"]]
CHANGES = {"1": {"web/a.ts"}, "2": {"web/b.ts"}, "3": {"psa/c.sql"}, "4": {"docs/d.md"}}


class SplitTests(unittest.TestCase):
    def test_one_suspect_goes_back_and_the_rest_retry_together(self):
        failed = {"web/a.test.ts": dict(tracked=["web/a.ts", "web/shared.ts"])}
        remove, pending, why = split_failed(MEMBERS, [[[9, "z"]]], failed, CHANGES)
        self.assertEqual(remove, [[1, "a"]])
        self.assertEqual(pending, [[[2, "b"], [3, "c"], [4, "d"]], [[9, "z"]]])
        self.assertIn("#1", why)

    def test_several_suspects_are_halved_and_the_others_stay_together(self):
        failed = {"t1": dict(tracked=["web/a.ts"]), "t2": dict(tracked=["web/b.ts"])}
        remove, pending, _ = split_failed(MEMBERS, [], failed, CHANGES)
        self.assertEqual(remove, [])
        self.assertEqual(pending, [[[3, "c"], [4, "d"]], [[1, "a"]], [[2, "b"]]])

    def test_listing_a_directory_sees_a_file_added_under_it(self):
        failed = {"t": dict(tracked=[], listings=["psa"])}
        remove, _, _ = split_failed(MEMBERS, [], failed, CHANGES)
        self.assertEqual(remove, [[3, "c"]])

    def test_without_receipts_or_with_an_unexplained_failure_the_batch_is_halved(self):
        halves = [[[1, "a"], [2, "b"]], [[3, "c"], [4, "d"]]]
        self.assertEqual(split_failed(MEMBERS, [], {}, CHANGES)[:2], ([], halves))
        self.assertEqual(split_failed(MEMBERS, [], {"t": dict(tracked=["x"])}, None)[:2], ([], halves))
        unexplained = {"t": dict(tracked=["elsewhere.ts"])}
        self.assertEqual(split_failed(MEMBERS, [], unexplained, CHANGES)[:2], ([], halves))

    def test_an_incomplete_trace_suspects_everyone(self):
        failed = {"t": dict(tracked=["web/a.ts"], incomplete=["exec"])}
        remove, pending, _ = split_failed(MEMBERS, [], failed, CHANGES)
        self.assertEqual(remove, [])
        self.assertEqual(sum(len(g) for g in pending), 4)


class ChainTests(unittest.TestCase):
    def test_each_member_is_blamed_for_its_own_step_of_the_chain(self):
        with tempfile.TemporaryDirectory() as d:
            run = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True).stdout.strip()
            run("init", "-q", "-b", "main")
            run("config", "user.email", "t@example.com")
            run("config", "user.name", "t")
            Path(d, "base.txt").write_text("base\n")
            run("add", ".")
            run("commit", "-qm", "base")
            base = run("rev-parse", "HEAD")
            heads = []
            for name in ("one", "two"):
                run("checkout", "-q", "-b", name, base)
                Path(d, f"{name}.txt").write_text(name + "\n")
                run("add", ".")
                run("commit", "-qm", name)
                heads.append(run("rev-parse", "HEAD"))
            run("checkout", "-q", "--detach", base)
            for h in heads:
                run("merge", "-q", "--no-ff", "-m", "merge", h)
            tip = run("rev-parse", "HEAD")
            record = dict(base=base, tip=tip, members=[[11, heads[0]], [12, heads[1]]])
            self.assertEqual(member_changes(Repo(d), record), {"11": {"one.txt"}, "12": {"two.txt"}})
            self.assertIsNone(member_changes(Repo(d), dict(record, members=[[11, heads[0]]])))


class ReceiptTests(unittest.TestCase):
    def test_failed_reads_come_from_receipts_among_the_run_artifacts(self):
        def download(repository, run_id, directory, patterns):
            out = Path(directory)
            (out / "validation-timings-1-portal" / "portal-shards").mkdir(parents=True)
            receipt = dict(job="portal", failed_reads={"a.test.ts": dict(tracked=["a.ts"])})
            (out / "validation-timings-1-portal" / "portal-shards" / "lit-shard-1.json").write_text(json.dumps(receipt))
            (out / "validation-timings-1-portal" / "timings.json").write_text(json.dumps(dict(seconds=3)))
            return ""

        with tempfile.TemporaryDirectory() as d, patch("concurrent_merge.github.download_artifacts", side_effect=download) as gh:
            self.assertEqual(run_receipts("o/r", "42", d), {"a.test.ts": dict(tracked=["a.ts"])})
            self.assertIn("validation-timings-*", gh.call_args.args[3])

    def test_a_run_without_artifacts_gives_no_receipts(self):
        error = GitHubError("no artifact matches the given patterns")
        with tempfile.TemporaryDirectory() as d, patch("concurrent_merge.github.download_artifacts", side_effect=error):
            self.assertEqual(run_receipts("o/r", "42", d), {})


class SingletonTests(unittest.TestCase):
    def test_one_source_gets_its_own_batch_branch(self):
        api = Intake("owner/repo", "main", "trusted", Mock())
        module = Intake.__module__
        base, tip = "b" * 40, "c" * 40
        with (
            patch(module + ".manifest", return_value=[[7, "a" * 40]]),
            patch(module + ".verify_sources", return_value="2026-11-24T00:00:00+00:00"),
            patch.object(api, "fetch_commits"),
            patch.object(api, "record", return_value="lock") as write,
            patch.object(api, "push") as push,
        ):
            api.claim({api.target: base}, base, tip)
        self.assertNotIn("individual", write.call_args.args[0])
        branches = [e[0] for e in push.call_args.args[0]]
        self.assertIn("refs/heads/codex/ci/lit-v1/" + base + "/" + tip, branches)


if __name__ == "__main__":
    unittest.main()
