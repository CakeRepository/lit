import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from benchmarks import replay_failures as replay
from e2e_github import git


class FailureReplay(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-qb", "main")
        (self.repo / "common").write_text("original\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.rows = []
        for i in range(1, 5):
            git(self.repo, "checkout", "-qb", f"topic-{i}", self.base)
            (self.repo / f"feature {i}").write_bytes(bytes([0, i, 255]))
            if i == 2:
                (self.repo / "common").unlink()
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", "change")
            head = git(self.repo, "rev-parse", "HEAD")
            git(self.repo, "checkout", "-q", "main")
            before = git(self.repo, "rev-parse", "HEAD")
            git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge pull request #{i}", head)
            self.rows.append(dict(number=i, base=before, head=head, merge=git(self.repo, "rev-parse", "HEAD")))

    def test_oracle_restores_deletions_and_preserves_other_binary_blobs(self):
        expected = replay.expected_tree(self.repo, self.rows, self.rows[1], self.root)
        self.assertEqual(git(self.repo, "ls-tree", "--name-only", expected).splitlines(),
                         ["common", "feature 1", "feature 3", "feature 4"])
        self.assertEqual(git(self.repo, "rev-parse", f"{expected}:common"),
                         git(self.repo, "rev-parse", f"{self.base}:common"))
        for n in (1, 3, 4):
            self.assertEqual(git(self.repo, "rev-parse", f"{expected}:feature {n}"),
                             git(self.repo, "rev-parse", f"HEAD:feature {n}"))

    def test_pair_is_red_together_and_green_on_each_source(self):
        a, b, first, second = replay.select_pair(self.repo, self.rows)
        self.assertEqual((a, b), (0, 3))
        for i, marker in [(a, first), (b, second)]:
            self.assertTrue(replay.fires(self.repo, self.rows[i]["head"], marker))
            self.assertFalse(replay.fires(self.repo, self.rows[i]["head"], first | second))
        self.assertTrue(replay.fires(self.repo, "main", first | second))
        probe = subprocess.run([sys.executable, replay.__file__, "--probe", self.root / "missing"],
                               capture_output=True)
        self.assertEqual(probe.returncode, 2)

    @unittest.skipUnless(os.environ.get("LIT_BIN"), "requires a native lit binary")
    def test_both_orders_and_positive_control_against_native_binary(self):
        archive, output = self.root / "archive.git", self.root / "results.json"
        git(self.root, "clone", "-q", "--bare", str(self.repo), str(archive))
        refs = git(archive, "show-ref")
        self.assertEqual(replay.main([str(archive), "--sizes", "4", "--lit", os.environ["LIT_BIN"],
                                      "--out", str(output)]), 0)
        rows = json.loads(output.read_bytes())
        self.assertEqual([row["returned"] for row in rows], [[], [1], [4], [1]])
        self.assertTrue(all(row["same_tree"] and row["probes_match"] and row["correct"] for row in rows))
        self.assertEqual([r["test_runs"] for r in rows], [1, 5, 3, 3])
        self.assertEqual([r["attempts"] for r in rows], [1, 5, 5, 5])
        self.assertEqual(git(archive, "show-ref"), refs)
