import pathlib
import unittest

ACTIONS = pathlib.Path(__file__).resolve().parent.parent / "actions"


class ActionTests(unittest.TestCase):
    def test_actions_find_lit_inside_container_jobs(self):
        # In a job that runs in a container, github.action_path is the runner host's path,
        # which the container can't see. GITHUB_ACTION_PATH is mapped into the container.
        files = sorted(ACTIONS.glob("*/action.yml"))
        self.assertTrue(files)
        for f in files:
            text = f.read_text()
            self.assertNotIn("${{ github.action_path", text, f.parent.name)
            if "concurrent_merge.cli" in text:
                self.assertIn('"$GITHUB_ACTION_PATH/../.."', text, f.parent.name)

    def test_actions_write_no_bytecode_into_their_checkout(self):
        # A container job runs as root; a __pycache__ it leaves in the action's checkout is
        # root-owned, and the runner then fails to clean its actions folder for every later job.
        for f in sorted(ACTIONS.glob("*/action.yml")):
            for line in f.read_text().splitlines():
                if "concurrent_merge.cli" in line:
                    self.assertIn("python3 -B ", line, f.parent.name)

    def test_actions_source_the_runner_guard_before_running_lit(self):
        # The guard also covers Python that lit starts and files lit leaves anywhere in its own folders.
        for f in sorted(ACTIONS.glob("*/action.yml")):
            text = f.read_text()
            if "concurrent_merge.cli" not in text:
                continue
            guard = text.find('source "$GITHUB_ACTION_PATH/../lib/guard.sh"')
            self.assertNotEqual(guard, -1, f.parent.name)
            self.assertLess(guard, text.find("concurrent_merge.cli"), f.parent.name)


if __name__ == "__main__":
    unittest.main()
