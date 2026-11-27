import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from concurrent_merge.unit_store import cached_units

TEST = """import os, sys
open(os.environ["LIT_TEST_MARKER"], "a").write("{name}\\n")
sys.exit(0 if "ok" in open("src/{name}.py").read() else 1)
"""


def traced():
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        return False
    return any(l.split()[1:] != ["0"] for l in status.splitlines() if l.startswith("TracerPid:"))


@unittest.skipUnless(shutil.which("strace") and not traced(), "needs strace and no outer tracer")
class UnitStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work, self.store, self.marker = root / "work", root / "store", root / "marker"
        self.git("init", "-q", "-b", "main", str(self.work), cwd=root)
        files = {"README.md": "x\n"}
        for name in ("a", "b"):
            files[f"src/{name}.py"] = "ok\n"
            files[f"tests/test_{name}.py"] = TEST.format(name=name)
        self.commit(files)
        env = patch.dict(os.environ, {"LIT_TEST_MARKER": str(self.marker)})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args, cwd=None):
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args],
            cwd=cwd or self.work, check=True, capture_output=True,
        )

    def commit(self, files):
        for path, text in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")

    def run_units(self, **kw):
        self.marker.write_text("")
        result = cached_units(
            self.work, "unit", self.store, [sys.executable, "{}"],
            discover=["tests/test_*.py"], log=os.devnull, **kw,
        )
        return result, sorted(self.marker.read_text().split())

    def test_shadow_runs_everything_and_scores_each_skip(self):
        first, ran = self.run_units()
        self.assertEqual((first["predicted_reusable"], ran), (0, ["a", "b"]))
        self.commit({"src/b.py": "ok again\n"})
        second, ran = self.run_units()
        self.assertEqual(ran, ["a", "b"])
        self.assertEqual(second["predicted_reusable"], 1)
        self.assertEqual(second["unsound"], [])
        self.assertEqual(second["first_reasons"], {"changed": 1, "reusable": 1})

    def test_enforce_runs_only_changed_new_and_audited_files(self):
        self.run_units()
        self.commit({"src/a.py": "bad\n", "tests/test_c.py": TEST.format(name="b")})
        result, ran = self.run_units(mode="enforce")
        self.assertEqual(ran, ["a", "b"])  # test_c reads src/b.py and writes "b"
        self.assertEqual(result["failed"], ["tests/test_a.py"])
        self.assertIn("src/a.py", result["failed_reads"]["tests/test_a.py"]["tracked"])
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["reused"], 1)
        again, ran = self.run_units(mode="enforce")
        self.assertEqual(ran, ["a"])  # a failing file never gets a footprint
        audited, ran = self.run_units(mode="enforce", audit_rate=1.0)
        self.assertEqual(ran, ["a", "b", "b"])
        self.assertEqual((audited["audited"], audited["reused"]), (2, 0))


    def batch(self):
        loop = 'for f; do "$0" "$f" >> "$LIT_TEST_MARKER.batch" || s=1; done; exit ${s:-0}'
        return ["sh", "-c", loop.replace('"$0"', '"' + sys.executable + '"'), "sh", "{}"]

    def test_batch_runs_what_must_run_in_one_untraced_command(self):
        self.run_units()
        self.commit({"src/a.py": "ok, changed\n", "tests/test_c.py": TEST.format(name="c"),
                     "src/c.py": "ok\n"})
        result, ran = self.run_units(mode="enforce", batch=self.batch())
        self.assertEqual(ran, ["a", "c"])
        self.assertEqual((result["ran"], result["reused"], result["exit_code"]), (2, 1, 0))
        self.assertEqual(result["batch"]["files"], 2)
        self.assertFalse(result["batch"]["traced_after"])
        # Files run in a batch keep no record, so they run again next time.
        again, ran = self.run_units(mode="enforce", batch=self.batch())
        self.assertEqual(ran, ["a", "c"])

    def test_a_failed_batch_reruns_its_files_traced_for_blame(self):
        self.run_units()
        self.commit({"src/a.py": "bad\n"})
        result, ran = self.run_units(mode="enforce", batch=self.batch())
        self.assertEqual(ran, ["a", "a"])
        self.assertEqual(result["failed"], ["tests/test_a.py"])
        self.assertIn("src/a.py", result["failed_reads"]["tests/test_a.py"]["tracked"])
        self.assertTrue(result["batch"]["traced_after"])
        self.assertEqual(result["exit_code"], 1)

    def bun_batch(self, always_fail=""):
        # Prints what bun prints outside a terminal: a file's path and a colon only before its failures, then a summary.
        loop = ('for f; do if "$0" "$f" && [ "$f" != "' + always_fail + '" ]; then :; '
                'else printf "%s:\\n(fail) t\\n" "$f"; s=1; fi; done; '
                'printf "Ran %d tests across %d files. [1.00ms]\\n" $# $#; exit ${s:-0}')
        return ["sh", "-c", loop.replace('"$0"', '"' + sys.executable + '"'), "sh", "{}"]

    def bun_units(self, **kw):
        if not (self.work / "tests/a.test.ts").exists():
            self.commit({f"tests/{n}.test.ts": TEST.format(name=n) for n in ("a", "b")})
        self.marker.write_text("")
        result = cached_units(
            self.work, "unit", self.store, [sys.executable, "{}"],
            discover=["tests/*.test.ts"], log=os.devnull, mode="enforce", **kw,
        )
        return result, sorted(self.marker.read_text().split())

    def test_a_failed_bun_batch_traces_only_the_files_bun_names(self):
        self.commit({"src/a.py": "bad\n"})
        result, ran = self.bun_units(batch=self.bun_batch(), batch_report="bun")
        self.assertEqual(ran, ["a", "a", "b"])  # a and b in the batch, then only a traced
        self.assertEqual(result["failed"], ["tests/a.test.ts"])
        self.assertIn("src/a.py", result["failed_reads"]["tests/a.test.ts"]["tracked"])
        self.assertEqual((result["ran"], result["batch"]["traced"], result["exit_code"]), (2, 1, 1))

    def test_a_file_bun_named_that_passes_alone_counts_as_passed(self):
        result, ran = self.bun_units(batch=self.bun_batch("tests/a.test.ts"), batch_report="bun")
        self.assertEqual(ran, ["a", "a", "b"])
        self.assertEqual((result["failed"], result["exit_code"], result["ran"]), ([], 0, 2))

    def test_without_a_bun_summary_or_report_every_file_is_traced(self):
        self.commit({"src/a.py": "bad\n"})
        result, ran = self.bun_units(batch=self.batch(), batch_report="bun")  # no summary line
        self.assertEqual(ran, ["a", "a", "b", "b"])
        self.assertEqual(result["batch"]["traced"], 2)
        self.commit({"src/b.py": "ok, changed\n"})  # b's traced pass left a record
        result, ran = self.bun_units(batch=self.bun_batch())  # no batch_report
        self.assertEqual(ran, ["a", "a", "b", "b"])
        self.assertEqual(result["failed"], ["tests/a.test.ts"])

    def test_a_merge_of_two_passing_sides_runs_only_what_read_both(self):
        both = """import os, sys
open(os.environ["LIT_TEST_MARKER"], "a").write("ab\\n")
sys.exit(0 if all("ok" in open(f"src/{n}.py").read() for n in "ab") else 1)
"""
        self.commit({"tests/test_ab.py": both})
        self.run_units()                      # main's records at the base
        pr_store = Path(self.tmp.name) / "pr-store"
        shutil.copytree(self.store, pr_store)
        self.git("checkout", "-q", "-b", "pr")
        self.commit({"src/a.py": "ok from the pr\n"})
        self.marker.write_text("")
        cached_units(self.work, "unit", pr_store, [sys.executable, "{}"],
                     discover=["tests/test_*.py"], log=os.devnull, mode="enforce")
        self.git("checkout", "-q", "main")
        self.commit({"src/b.py": "ok from main\n"})
        self.run_units(mode="enforce")        # main's own run records b and ab
        self.git("merge", "-q", "--no-edit", "pr")
        alone, _ = self.run_units(mode="plan", host=False)
        self.assertEqual(sorted(alone["decisions"]), ["tests/test_a.py", "tests/test_ab.py"])
        proved, ran = self.run_units(mode="plan", host=False, proofs=[pr_store])
        self.assertEqual(ran, [])
        self.assertEqual(sorted(proved["decisions"]), ["tests/test_ab.py"])
        self.assertEqual(proved["reusable_by_proof"], 1)

    def test_runtime_data_directory_is_not_an_input(self):
        appender = """import os
os.makedirs("data", exist_ok=True)
if os.path.exists("data/log.jsonl"):
    open("data/log.jsonl").read()
open("data/log.jsonl", "a").write("{name}\\n")
open(os.environ["LIT_TEST_MARKER"], "a").write("{name}\\n")
assert "ok" in open("src/{name}.py").read()
"""
        self.commit({".gitignore": "data/\n", "tests/test_a.py": appender.format(name="a"),
                     "tests/test_b.py": appender.format(name="b")})
        (self.work / "data").mkdir()
        (self.work / "data" / "log.jsonl").write_text("left by an earlier run\n")
        self.run_units()
        again, _ = self.run_units()
        # Without scratch the log is read and then rewritten, so no file ever gets a footprint.
        self.assertEqual(again["predicted_reusable"], 0)
        self.run_units(scratch=["data"])
        reused, ran = self.run_units(scratch=["data"], mode="enforce")
        self.assertEqual((reused["predicted_reusable"], ran), (2, []))
        self.commit({"src/a.py": "ok, changed\n"})
        changed, ran = self.run_units(scratch=["data"], mode="enforce")
        self.assertEqual(ran, ["a"])
        # A file the commit tracks under scratch is still an input.
        self.run_units(scratch=["data", "src"])
        self.commit({"src/b.py": "ok, changed too\n"})
        tracked, ran = self.run_units(scratch=["data", "src"], mode="enforce")
        self.assertEqual(ran, ["b"])
        # A store recorded with scratch is not reused by a run without it.
        plain, _ = self.run_units(mode="plan")
        self.assertEqual(plain["first_reasons"].get("scratch"), 2)

    def test_scratch_must_be_a_plain_repository_path(self):
        with self.assertRaises(ValueError):
            self.run_units(scratch=["../x"])

    def test_plan_runs_nothing_and_lists_skippable_files(self):
        self.run_units()
        self.commit({"src/b.py": "ok again\n"})
        plan, ran = self.run_units(mode="plan")
        self.assertEqual(ran, [])
        self.assertEqual(plan["reusable"], ["tests/test_a.py"])
        self.assertEqual(sorted(plan["decisions"]), ["tests/test_b.py"])


class BunLogTests(unittest.TestCase):
    def test_failed_files_are_the_ones_bun_names(self):
        from concurrent_merge.bun_log import failed_files, score

        log = "bun test v1.4.4\nsrc/two.test.ts:\nerror: expect(received).toBe(expected)\n" \
              "(fail) y [0.15ms]\nsrc/three.test.tsx:\n# Unhandled error between tests\n" \
              " 2 pass\n 1 fail\nRan 3 tests across 3 files. [5.00ms]\n"
        self.assertEqual(failed_files(log), {"src/two.test.ts", "src/three.test.tsx"})
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(log)
        self.addCleanup(os.unlink, f.name)
        plan = dict(commit="c", units=3, reusable=["src/one.test.ts", "src/two.test.ts"],
                    seconds_predicted_reusable=1.0, first_reasons={})
        self.assertEqual(score(plan, [f.name])["unsound"], ["src/two.test.ts"])


class ShardTests(unittest.TestCase):
    def test_shards_cover_every_name_once_and_balance_seconds(self):
        from concurrent_merge.unit_store import shard_of

        weights = {"a": 60, "b": 30, "c": 30, "d": 1, "e": 1}
        parts = [shard_of(weights, weights, i, 2) for i in range(2)]
        self.assertEqual(sorted(parts[0] + parts[1]), sorted(weights))
        self.assertEqual(parts[0], ["a", "d"])
        self.assertEqual(parts[1], ["b", "c", "e"])

    def test_pinned_names_all_go_to_shard_zero(self):
        from concurrent_merge.unit_store import shard_of

        weights = {"a": 60, "b": 30, "c": 30, "d": 1, "e": 1}
        parts = [shard_of(weights, weights, i, 2, pinned=["d", "e"]) for i in range(2)]
        self.assertEqual(sorted(parts[0] + parts[1]), sorted(weights))
        self.assertTrue({"d", "e"} <= set(parts[0]))


if __name__ == "__main__":
    unittest.main()
