"""lit releases itself from main's VERSION file: check which tag the release workflow's plan step picks."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TEXT = (REPO / ".github" / "workflows" / "release.yml").read_text()


def block(after, indent):
    """The lines under the first line that is `after`, while they are indented more than `indent` spaces."""
    lines = TEXT.split("\n" + after + "\n", 1)[1].splitlines()
    kept = []
    for line in lines:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        kept.append(line[indent + 2:])
    return "\n".join(kept) + "\n"


def plan(event, ref_type, ref_name, version="v0.1.2", tags="", git_fails=False):
    """Run the plan step with a fake git; return (exit code, tag it wrote, stdout)."""
    script = block("        run: |", 8).split("  build:", 1)[0]
    with tempfile.TemporaryDirectory() as work:
        work = pathlib.Path(work)
        (work / "VERSION").write_text(version + "\n")
        fake = work / "bin"
        fake.mkdir()
        (fake / "git").write_text("#!/bin/sh\n" + ("exit 128\n" if git_fails else 'printf "%s" "$FAKE_TAGS"\n'))
        (fake / "git").chmod(0o755)
        out = work / "out"
        env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "GITHUB_OUTPUT": str(out),
               "GITHUB_REPOSITORY": "o/r", "EVENT": event, "REF_TYPE": ref_type, "REF_NAME": ref_name,
               "FAKE_TAGS": tags}
        done = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env, text=True, capture_output=True)
        written = out.read_text() if out.exists() else ""
        return done.returncode, written.removeprefix("tag=").strip(), done.stdout


@unittest.skipUnless(shutil.which("bash"), "bash required")
class ReleasePlan(unittest.TestCase):
    def test_a_new_version_on_main_is_released(self):
        self.assertEqual(plan("push", "branch", "main")[:2], (0, "v0.1.2"))

    def test_a_version_that_is_already_tagged_is_not_released_again(self):
        code, tag, said = plan("push", "branch", "main", tags="abc\trefs/tags/v0.1.2\n")
        self.assertEqual((code, tag), (0, ""))
        self.assertIn("already tagged", said)

    def test_a_pushed_tag_publishes_itself(self):
        self.assertEqual(plan("push", "tag", "v0.2.0", tags="x")[:2], (0, "v0.2.0"))

    def test_a_manual_run_builds_without_publishing(self):
        self.assertEqual(plan("workflow_dispatch", "branch", "main")[:2], (0, ""))

    def test_a_malformed_version_or_unreadable_tags_fail_the_run(self):
        self.assertNotEqual(plan("push", "branch", "main", version="0.1.2")[0], 0)
        self.assertNotEqual(plan("push", "branch", "main", git_fails=True)[0], 0)

    def test_publish_runs_only_with_a_tag_and_tags_a_version_release_at_its_commit(self):
        publish = TEXT.split("\n  publish:\n", 1)[1]
        self.assertIn("    if: needs.plan.outputs.tag != ''\n", publish)
        self.assertIn('--target "$GITHUB_SHA"', publish)
        self.assertIn("--verify-tag", publish)
        self.assertIn("  push:\n    branches: [main]\n    paths: [VERSION]\n    tags: [\"v*\"]\n", TEXT)


if __name__ == "__main__":
    unittest.main()
