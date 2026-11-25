import subprocess
import tempfile
import unittest
from pathlib import Path

from concurrent_merge.cli import publish
from concurrent_merge.engine import Repo

IDENTITY = {
    "GIT_AUTHOR_NAME": "lit", "GIT_AUTHOR_EMAIL": "lit@example.invalid",
    "GIT_COMMITTER_NAME": "lit", "GIT_COMMITTER_EMAIL": "lit@example.invalid",
}


class PublishTests(unittest.TestCase):
    """Landing moves main by compare-and-swap: a batch tested on an old main never lands."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "--bare", str(root / "remote.git")], check=True)
        subprocess.run(["git", "init", "-q", "--bare", str(root / "batch.git")], check=True)
        self.remote = Repo(root / "remote.git")
        self.batch = Repo(root / "batch.git")
        self.destination = str(self.remote.path)
        self.base = self.commit(self.batch, "base")
        self.batch.run("push", "-q", self.destination, self.base + ":refs/heads/main")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, repo, message, parent=None):
        tree = repo.out("mktree", data=b"")
        args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
        return repo.out(*args, env=IDENTITY)

    def main(self):
        return self.remote.out("rev-parse", "refs/heads/main")

    def test_a_batch_on_the_current_main_lands(self):
        final = self.commit(self.batch, "batch", self.base)
        publish(self.batch, self.destination, "refs/heads/main", self.base, final)
        self.assertEqual(self.main(), final)

    def test_a_batch_on_a_main_that_moved_lands_nothing(self):
        final = self.commit(self.batch, "batch", self.base)
        # Someone else lands first, after the eligibility check and before the push.
        other = self.commit(self.batch, "other", self.base)
        self.batch.run("push", "-q", self.destination, other + ":refs/heads/main")
        with self.assertRaisesRegex(ValueError, "moved since the batch was tested"):
            publish(self.batch, self.destination, "refs/heads/main", self.base, final)
        self.assertEqual(self.main(), other)

    def test_a_fast_forward_on_a_moved_main_is_still_refused(self):
        # The batch descends from what main now holds, but it was tested on the old main.
        other = self.commit(self.batch, "other", self.base)
        final = self.commit(self.batch, "batch", other)
        self.batch.run("push", "-q", self.destination, other + ":refs/heads/main")
        with self.assertRaisesRegex(ValueError, "moved since the batch was tested"):
            publish(self.batch, self.destination, "refs/heads/main", self.base, final)
        self.assertEqual(self.main(), other)

    def test_other_push_failures_are_not_reported_as_a_race(self):
        final = self.commit(self.batch, "batch", self.base)
        missing = str(Path(self.tmp.name) / "missing.git")
        with self.assertRaises(RuntimeError):
            publish(self.batch, missing, "refs/heads/main", self.base, final)
        self.assertEqual(self.main(), self.base)


if __name__ == "__main__":
    unittest.main()
