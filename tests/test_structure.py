from pathlib import Path
import subprocess
import tempfile
import unittest

from concurrent_merge.engine import Engine, Repo
from concurrent_merge.structure import has_driver, python_merge, resolve_file

BASE = b'''import os


def first():
    return 1


def second():
    return 2


class Box:
    size = 1

    @property
    def width(self):
        return 1

    def height(self):
        return 1
'''


class PythonMergeTests(unittest.TestCase):
    def test_adjacent_functions_merge(self):
        ours = BASE.replace(b"return 1\n\n\ndef second", b"return 10\n\n\ndef second")
        theirs = BASE.replace(b"return 2", b"return 20")
        merged = python_merge([BASE, ours, theirs])
        self.assertEqual(merged, ours.replace(b"return 2", b"return 20"))

    def test_methods_and_decorators_merge_inside_a_class(self):
        ours = BASE.replace(b"    @property\n", b"    @property\n    @staticmethod\n")
        theirs = BASE.replace(b"    def height(self):\n        return 1", b"    def height(self):\n        return 9")
        merged = python_merge([BASE, ours, theirs])
        self.assertIn(b"@staticmethod\n    def width", merged)
        self.assertIn(b"return 9", merged)

    def test_top_level_code_and_a_function_merge(self):
        ours = BASE.replace(b"import os\n", b"import os\nimport sys\n")
        theirs = BASE.replace(b"return 1\n\n\ndef second", b"return 7\n\n\ndef second")
        merged = python_merge([BASE, ours, theirs])
        self.assertTrue(merged.startswith(b"import os\nimport sys\n"))
        self.assertIn(b"return 7", merged)

    def test_both_sides_change_one_function(self):
        ours = BASE.replace(b"return 2", b"return 3")
        theirs = BASE.replace(b"return 2", b"return 4")
        self.assertIsNone(python_merge([BASE, ours, theirs]))

    def test_added_or_renamed_definitions_are_left_to_git(self):
        ours = BASE + b"\n\ndef third():\n    return 3\n"
        theirs = BASE.replace(b"return 2", b"return 20")
        self.assertIsNone(python_merge([BASE, ours, theirs]))

    def test_unparsable_input_or_result(self):
        broken = BASE.replace(b"def first():", b"def first(:")
        self.assertIsNone(python_merge([BASE, broken, BASE]))
        self.assertIsNone(python_merge([b"\x00", b"\x00", b"\x00"]))

    def test_duplicate_names_are_ambiguous(self):
        base = b"def f():\n    return 1\n\n\ndef f():\n    return 2\n"
        ours = base.replace(b"return 1", b"return 3")
        theirs = base.replace(b"return 2", b"return 4")
        self.assertIsNone(python_merge([base, ours, theirs]))

    def test_crlf_and_encoding_cookie(self):
        base = BASE.replace(b"\n", b"\r\n")
        base = b"# -*- coding: latin-1 -*-\r\n" + base.replace(b"return 1\r\n\r\n\r\ndef second", b"return '\xe9'\r\n\r\n\r\ndef second")
        ours = base.replace(b"'\xe9'", b"'\xe8'")
        theirs = base.replace(b"return 2", b"return 5")
        self.assertEqual(python_merge([base, ours, theirs]), ours.replace(b"return 2", b"return 5"))

    def test_registration(self):
        self.assertTrue(has_driver("pkg/mod.py"))
        self.assertFalse(has_driver("web/app.ts"))
        self.assertEqual(resolve_file("web/app.ts", [BASE] * 3), (None, None))
        ours = BASE.replace(b"return 2", b"return 3")
        self.assertEqual(resolve_file("m.py", [BASE, ours, BASE]), (ours, "python-ast"))


class EnginePythonTests(unittest.TestCase):
    def test_adjacent_python_edits_land_in_one_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Repo(root / "repo.git")
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", repo.path], check=True)
            repo.run("config", "user.name", "Test Author")
            repo.run("config", "user.email", "author@localhost")

            def commit(parent, data):
                blob = repo.out("hash-object", "-w", "--stdin", data=data)
                env = {"GIT_INDEX_FILE": str(root / "index")}
                if parent:
                    repo.run("read-tree", parent, env=env)
                repo.run("update-index", "--add", "--cacheinfo", "100644", blob, "m.py", env=env)
                tree = repo.out("write-tree", env=env)
                return repo.out("commit-tree", tree, *(["-p", parent] if parent else []), "-m", "c")

            base = b"def a():\n    return 0\ndef b():\n    return 0\n"
            start = commit(None, base)
            repo.run("update-ref", "refs/heads/main", start)
            changes = [
                dict(number=1, author="x", head=commit(start, base.replace(b"return 0\ndef b", b"return 1\ndef b"))),
                dict(number=2, author="x", head=commit(start, base.replace(b"def b():", b"def b(x):"))),
            ]
            engine = Engine(repo, root / "state")
            final, accepted = engine.batch(start, changes, lambda _: "pass")
            self.assertEqual(accepted, {1, 2})
            self.assertEqual(repo.run("show", final + ":m.py").stdout, b"def a():\n    return 1\ndef b(x):\n    return 0\n")
            self.assertEqual(engine.history[0]["resolved"][0]["method"], "python-ast")


if __name__ == "__main__":
    unittest.main()
