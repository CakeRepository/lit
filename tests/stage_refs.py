"""Real stage refs backed by an isolated repository and read-only GitHub fixtures."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from e2e_github import FakeGitHub, git, lit


@unittest.skipUnless(os.environ.get("LIT_BIN"), "requires the native stage operator")
class StageRefs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.gh = FakeGitHub(self.root)
        self.addCleanup(self.gh.close)
        self.work = self.root / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        policy = dict(repository=self.gh.repository, stages=[
            dict(name="lab", workflow=".github/workflows/test.yml", checks=["product"], environment=None),
            dict(name="prod", workflow=".github/workflows/test.yml", checks=["product"], environment="Production"),
        ])
        (self.work / ".lit").mkdir()
        (self.work / ".lit/stages.json").write_text(json.dumps(policy))
        git(self.work, "add", ".")
        git(self.work, "commit", "-qm", "first main snapshot")
        self.a = git(self.work, "rev-parse", "HEAD")
        git(self.work, "commit", "--allow-empty", "-qm", "second main snapshot")
        self.b = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-q", str(self.gh.remote), "main")
        self.original = self.gh.answer
        self.gh.answer = self.answer
        self.reset_evidence(self.a)

    def reset_evidence(self, sha):
        repository = dict(full_name=self.gh.repository)
        self.run = dict(id=123, workflow_id=7, head_sha=sha, repository=repository,
                        head_repository=repository, head_branch="main", event="push",
                        path=".github/workflows/test.yml", run_attempt=1, status="completed", conclusion="success")
        self.latest = self.run.copy()
        self.jobs = [dict(name="product", id=22, run_id=123, head_sha=sha, status="completed", conclusion="success")]
        self.reviewers = [dict(type="User", reviewer=dict(id=37))]
        self.reviews = [dict(state="approved", user=dict(type="User", id=37, login="reviewer"),
                             environments=[dict(id=19, name="Production")])]
        self.on_latest = None

    def answer(self, raw):
        path = urlsplit(raw).path.split("/" + self.gh.repository + "/")[-1]
        if path == "actions/runs/123":
            return 200, self.run
        if path == f"actions/runs/123/attempts/{self.run['run_attempt']}/jobs":
            return 200, dict(jobs=self.jobs)
        if path == "environments/Production":
            return 200, dict(id=19, name="Production",
                             protection_rules=[dict(type="required_reviewers", reviewers=self.reviewers)])
        if path == "actions/runs/123/approvals":
            return 200, self.reviews
        if path == "actions/workflows/7/runs":
            if self.on_latest:
                self.on_latest()
            return 200, dict(workflow_runs=[self.latest])
        return self.original(raw)

    def invoke(self, *args, ok=True):
        result = lit(self.root, self.gh.env(), "stage", *args, "--repo", str(self.gh.remote))
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        return result

    def promote(self, name, sha, seen="empty", ok=True):
        return self.invoke("promote", name, sha, "--expected", seen, "--run", "123", ok=ok)

    def test_each_bridge_has_its_own_history_evidence_and_rollback(self):
        self.promote("prod", self.a, ok=False)
        self.promote("lab", self.a)
        self.reset_evidence(self.b)
        self.promote("lab", self.b, self.a)
        self.reset_evidence(self.a)
        self.promote("prod", self.a)  # A historical snapshot can advance.
        record = json.loads(git(self.gh.remote, "show", "-s", "--format=%B", "refs/lit/stage-history/prod"))
        self.assertEqual(record["evidence"]["checks"], {"product": 22})
        self.assertEqual(record["evidence"]["approval"]["reviewer"], "reviewer")
        self.invoke("rollback", "lab", "--expected", self.a, ok=False)
        self.invoke("rollback", "lab", "--expected", self.b)
        self.reset_evidence(self.b)
        self.promote("prod", self.b, self.a, ok=False)  # Removed from the source history.
        self.assertEqual(self.gh.ref("refs/lit/stages/lab"), self.a)
        self.assertEqual(self.gh.ref("refs/lit/stages/prod"), self.a)
        self.assertEqual(self.gh.ref("refs/heads/main"), self.b)
        self.assertEqual(git(self.gh.remote, "rev-list", "--count", "refs/lit/stage-history/lab"), "3")

    def test_evidence_fails_closed_and_a_concurrent_ref_wins(self):
        self.promote("lab", self.a)
        for change in [
            lambda: self.run.update(head_sha=self.b),
            lambda: self.run.update(event="pull_request_target"),
            lambda: self.run.update(path=".github/workflows/other.yml"),
            lambda: self.run.update(head_repository={"full_name": "other/fork"}),
            lambda: self.latest.update(id=124),
            lambda: self.jobs.clear(),
            lambda: self.jobs.append(self.jobs[0]),
            lambda: self.jobs[0].update(conclusion="failure"),
            lambda: self.jobs[0].update(head_sha=self.b),
            lambda: self.reviewers.clear(),
            lambda: self.reviews.clear(),
            lambda: self.reviews[0]["user"].update(type="Bot"),
            lambda: self.reviews[0]["user"].update(id=38),
            lambda: self.reviews[0]["environments"][0].update(id=20),
            lambda: self.reviews[0].update(state="rejected"),
            lambda: self.run.update(run_attempt=2),
        ]:
            with self.subTest(change=change.__code__.co_firstlineno):
                self.reset_evidence(self.a)
                change()
                self.promote("prod", self.a, ok=False)
                self.assertIsNone(self.gh.ref("refs/lit/stages/prod"))
                self.assertIsNone(self.gh.ref("refs/lit/stage-history/prod"))
        self.reset_evidence(self.a)
        self.on_latest = lambda: git(self.gh.remote, "update-ref", "refs/lit/stages/prod", self.b)
        self.promote("prod", self.a, ok=False)
        self.assertEqual(self.gh.ref("refs/lit/stages/prod"), self.b)
        self.assertIsNone(self.gh.ref("refs/lit/stage-history/prod"))
