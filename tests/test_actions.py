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


if __name__ == "__main__":
    unittest.main()
