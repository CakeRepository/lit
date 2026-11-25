import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from concurrent_merge.cache import cached_run, report

JOB = b"""import os, sys
open(os.environ["LIT_TEST_MARKER"], "a").write("ran\\n")
print(open("src/app.py").read(), open("deps/lib.txt").read())
print(sorted(os.listdir("tests")))
sys.exit(1 if "broken" in open("src/app.py").read() else 0)
"""


def traced():
    # ptrace allows one tracer, so these tests cannot run inside a traced job.
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        return False
    return any(l.split()[1:] != ["0"] for l in status.splitlines() if l.startswith("TracerPid:"))


@unittest.skipUnless(shutil.which("strace") and not traced(), "needs strace and no outer tracer")
class CachedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work, self.store = root / "work", root / "store"
        self.marker = root / "marker"
        self.git("init", "-q", "-b", "main", str(self.work), cwd=root)
        self.write({"src/app.py": "x = 1\n", "docs/a.md": "a\n", "tests/run.py": JOB.decode()})
        (self.work / ".gitignore").write_text("deps/\n")
        self.commit()
        (self.work / "deps").mkdir()
        (self.work / "deps" / "lib.txt").write_text("v1\n")
        self.command = [sys.executable, "tests/run.py"]
        env = patch.dict(os.environ, {"LIT_TEST_MARKER": str(self.marker)})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args, cwd=None):
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args],
            cwd=cwd or self.work,
            check=True,
            capture_output=True,
        )

    def write(self, files):
        for path, text in files.items():
            target = self.work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit(self, files=None):
        if files:
            self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")

    def run_job(self, **kw):
        return cached_run(
            self.work, "unit", self.store, self.command, log=os.devnull, **kw
        )

    def runs(self):
        return self.marker.read_text().count("ran") if self.marker.exists() else 0

    def test_shadow_records_then_scores_a_matching_prediction(self):
        first = self.run_job()
        self.assertEqual((first["decision"], first["predicted_reuse"]), ("ran", False))
        self.assertEqual(len(list((self.store / "unit" / "footprints").glob("*.json"))), 1)
        self.commit({"docs/a.md": "b\n"})
        second = self.run_job()
        self.assertTrue(second["predicted_reuse"])
        self.assertTrue(second["sound"])
        self.assertEqual(self.runs(), 2)
        summary = report(self.store)["unit"]
        self.assertEqual(summary["predicted_reusable"], 1)
        self.assertEqual(summary["unsound"], 0)

    def test_enforce_skips_only_when_every_input_matches(self):
        self.run_job()
        self.commit({"docs/a.md": "b\n"})
        reused = self.run_job(mode="enforce", audit_rate=0.0)
        self.assertEqual((reused["decision"], reused["exit_code"]), ("reused", 0))
        self.assertEqual(self.runs(), 1)
        (self.work / "deps" / "lib.txt").write_text("v2\n")
        changed = self.run_job(mode="enforce", audit_rate=0.0)
        self.assertEqual(changed["decision"], "ran")
        self.assertEqual(self.runs(), 2)
        audited = self.run_job(mode="enforce", audit_rate=1.0)
        self.assertEqual(audited["decision"], "audited")
        self.assertTrue(audited["sound"])
        self.assertEqual(self.runs(), 3)
        self.assertEqual(report(self.store, "unit")["unit"]["reused"], 1)

    def test_failures_are_never_stored_or_reused(self):
        self.commit({"src/app.py": "broken\n"})
        failed = self.run_job(mode="enforce")
        self.assertEqual(failed["exit_code"], 1)
        self.assertFalse((self.store / "unit" / "footprints").exists())
        again = self.run_job(mode="enforce")
        self.assertEqual((again["decision"], again["exit_code"]), ("ran", 1))

    def test_a_new_test_file_forces_a_run(self):
        self.run_job()
        self.commit({"tests/test_new.py": ""})
        result = self.run_job(mode="enforce", audit_rate=0.0)
        self.assertEqual(result["decision"], "ran")

    def test_rejects_unsafe_job_names(self):
        with self.assertRaises(ValueError):
            cached_run(self.work, "../x", self.store, self.command)


if __name__ == "__main__":
    unittest.main()
