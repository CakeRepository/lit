"""Run lit's composite actions the way a runner does and check they leave the runner cleanable.

A self-hosted runner deletes its _work/_actions folder before each job. If lit leaves a file in its own action
checkout, or leaves one the runner's user doesn't own, every later job on that runner fails at Set up job. That
broke all three FlowRMM orchestrator runners on Nov 25 2026: the batcher ran as root in a container job and
importing lit wrote root-owned __pycache__ into the action folder.

These tests copy lit into a scratch "action folder", run each action's own step script from its action.yml
with the environment a runner gives it, and compare the folder before and after. When the tests run as root
(lit's CI runs them in a container, like FlowRMM's jobs), the folders belong to another user first, so they
also prove that nothing is left that the runner's user couldn't remove.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
RUNNER_UID = 4242  # stands in for the runner's own user when the tests run as root
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def step(action):
    """The env block and run script of an action's single composite step, read from its action.yml."""
    lines = (REPO / "actions" / action / "action.yml").read_text().splitlines()
    env, script, i = {}, [], 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "env:":
            indent = len(lines[i]) - len(lines[i].lstrip())
            i += 1
            while i < len(lines) and len(lines[i]) - len(lines[i].lstrip()) > indent:
                key, _, value = lines[i].strip().partition(": ")
                env[key] = value
                i += 1
            continue
        if stripped == "run: |":
            indent = len(lines[i]) - len(lines[i].lstrip())
            i += 1
            body = []
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                body.append(lines[i])
                i += 1
            pad = min(len(b) - len(b.lstrip()) for b in body if b.strip())
            script = [b[pad:] for b in body]
            continue
        i += 1
    return env, "\n".join(script) + "\n"


def snapshot(root):
    return {
        str(p.relative_to(root)): (p.stat().st_uid, p.stat().st_mtime_ns if p.is_file() else 0)
        for p in sorted(root.rglob("*"))
    }


class ActionHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="lit-hygiene-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # What the runner downloads for "uses: CakeRepository/lit/actions/x@sha".
        self.root = self.tmp / "_actions" / "CakeRepository" / "lit" / "sha"
        for part in ("actions", "concurrent_merge"):
            shutil.copytree(
                REPO / part, self.root / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        self.runner_temp = self.tmp / "_temp"
        self.home = self.runner_temp / "_github_home"
        self.home.mkdir(parents=True)
        for name in ("output", "summary"):
            (self.runner_temp / name).touch()
        if AS_ROOT:
            for p in [self.tmp, *self.tmp.rglob("*")]:
                os.chown(p, RUNNER_UID, RUNNER_UID)
        self.before = snapshot(self.root)

    def run_action(self, action, inputs, extra_env=None):
        template, script = step(action)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "GITHUB_", "RUNNER_"))}
        for key, value in template.items():
            name = value.removeprefix("${{ inputs.").removesuffix(" }}")
            env[key] = inputs.get(name, "")
        env.update(
            GITHUB_ACTION_PATH=str(self.root / "actions" / action),
            GITHUB_OUTPUT=str(self.runner_temp / "output"),
            GITHUB_STEP_SUMMARY=str(self.runner_temp / "summary"),
            RUNNER_TEMP=str(self.runner_temp),
            HOME=str(self.home),
        )
        env.update(extra_env or {})
        # The runner's own shell line for "shell: bash".
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            env=env, capture_output=True, text=True, timeout=300,
        )

    def assert_runner_can_clean(self, done):
        log = done.stdout + done.stderr
        self.assertEqual(snapshot(self.root), self.before, "lit changed its action folder\n" + log)
        self.assertNotIn("::warning title=lit wrote", log)
        self.assertNotIn("::error title=lit left", log)
        if AS_ROOT:
            foreign = [str(p) for p in self.tmp.rglob("*") if p.lstat().st_uid != RUNNER_UID]
            self.assertEqual(foreign, [], "files the runner's user could not remove\n" + log)

    def test_cached_run_leaves_the_runner_cleanable(self):
        # The test command itself imports lit without -B: the guard must cover Python that lit starts too.
        probe = f"python3 -c 'import sys; sys.path.insert(0, \"{self.root}\"); import concurrent_merge.engine'"
        done = self.run_action(
            "cached-run",
            {"job": "probe", "run": probe, "store": str(self.tmp / "store"), "mode": "shadow",
             "audit-rate": "0.1", "working-directory": str(self.tmp), "extra-args": ""},
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assert_runner_can_clean(done)

    def test_batcher_leaves_the_runner_cleanable_even_when_it_fails(self):
        # No GitHub here: the pass fails at its first API call, after importing lit and creating its Git cache
        # under HOME, which is the part that has to stay cleanable.
        done = self.run_action(
            "batcher",
            {"token": "x", "context": "CI / tests", "label": "lit-ready", "branch": "main",
             "repository": "example/repo", "cache-dir": "", "max-batch": ""},
            {"GITHUB_API_URL": "http://127.0.0.1:9", "LIT_GITHUB_API": "rest"},
        )
        self.assertNotEqual(done.returncode, 0)
        self.assert_runner_can_clean(done)

    def test_guard_removes_what_lit_leaves_and_says_so(self):
        script = (
            'source "$GITHUB_ACTION_PATH/../lib/guard.sh"\n'
            'mkdir -p "$GITHUB_ACTION_PATH/../../concurrent_merge/__pycache__"\n'
            'touch "$GITHUB_ACTION_PATH/../../concurrent_merge/__pycache__/engine.cpython-313.pyc"\n'
        )
        env = dict(os.environ, GITHUB_ACTION_PATH=str(self.root / "actions" / "batcher"),
                   RUNNER_TEMP=str(self.runner_temp), HOME=str(self.home))
        done = subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("::warning title=lit wrote into its action folder::", done.stdout)
        self.assertEqual(snapshot(self.root), self.before)

    @unittest.skipUnless(AS_ROOT, "needs root, like a container job")
    def test_guard_hands_back_files_it_cannot_avoid(self):
        script = (
            'source "$GITHUB_ACTION_PATH/../lib/guard.sh"\n'
            'touch "$GITHUB_ACTION_PATH/../../concurrent_merge/__init__.py"\n'  # existing file, now root-touched
            'chown 0:0 "$GITHUB_ACTION_PATH/../../concurrent_merge/__init__.py"\n'
            'mkdir -p "$HOME/.cache/lit" "$RUNNER_TEMP/lit-work"\n'
        )
        env = dict(os.environ, GITHUB_ACTION_PATH=str(self.root / "actions" / "batcher"),
                   RUNNER_TEMP=str(self.runner_temp), HOME=str(self.home))
        done = subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        foreign = [str(p) for p in self.tmp.rglob("*") if p.lstat().st_uid != RUNNER_UID]
        self.assertEqual(foreign, [])


if __name__ == "__main__":
    unittest.main()
