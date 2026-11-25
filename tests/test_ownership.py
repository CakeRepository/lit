"""lit works on a checkout owned by another user, as in a CI container."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from concurrent_merge.blame import changed_paths
from concurrent_merge.engine import Repo
from concurrent_merge.footprint import checkout


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root to chown")
class OwnershipTests(unittest.TestCase):
    def setUp(self):
        # A runner may trust every directory globally (ci.yml does); this test needs git's own default.
        env = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", self.root, *a], check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        (self.root / "a.txt").write_text("one\n")
        run("add", "a.txt")
        run("commit", "-qm", "one")
        (self.root / "a.txt").write_text("two\n")
        run("commit", "-qam", "two")
        # Checked out by the runner's user, read by root in the container.
        subprocess.run(["chown", "-R", "4321:4321", self.root], check=True)
        plain = subprocess.run(["git", "-C", self.root, "rev-parse", "HEAD"], capture_output=True)
        self.assertNotEqual(plain.returncode, 0, "git should refuse the repository without lit's trust")

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_blame_and_checkout_trust_the_repository(self):
        repo = Repo(self.root)
        head = repo.out("rev-parse", "HEAD")
        self.assertEqual(len(head), 40)
        self.assertEqual(changed_paths(self.root, "HEAD~1", "HEAD"), {"a.txt"})
        target = Path(self.tmp.name) / "work"
        checkout(repo, head, str(target))
        self.assertEqual((target / "a.txt").read_text(), "two\n")


if __name__ == "__main__":
    unittest.main()
