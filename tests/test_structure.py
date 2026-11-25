from pathlib import Path
import subprocess
import tempfile
import unittest

from concurrent_merge.engine import Engine, Repo
from concurrent_merge.structure import has_driver, outline_merge, resolve_file


def python_merge(blobs):
    return outline_merge("m.py", blobs)

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

    def test_definitions_added_on_both_sides_are_kept_in_order(self):
        ours = BASE + b"\n\ndef third():\n    return 3\n"
        theirs = BASE.replace(b"return 2", b"return 20") + b"\n\ndef fourth():\n    return 4\n"
        self.assertEqual(
            python_merge([BASE, ours, theirs]),
            BASE.replace(b"return 2", b"return 20")
            + b"\n\ndef third():\n    return 3\n\n\ndef fourth():\n    return 4\n",
        )

    def test_imports_added_on_both_sides_merge(self):
        ours = BASE.replace(b"import os\n", b"import os\nimport re\n")
        theirs = BASE.replace(b"import os\n", b"import os\nimport sys\n")
        self.assertEqual(python_merge([BASE, ours, theirs]), BASE.replace(b"import os\n", b"import os\nimport re\nimport sys\n"))
        self.assertEqual(python_merge([BASE, ours, ours]), ours)

    def test_the_same_name_added_differently_is_a_conflict(self):
        ours = BASE + b"\n\ndef third():\n    return 3\n"
        theirs = BASE + b"\n\ndef third():\n    return 30\n"
        self.assertIsNone(python_merge([BASE, ours, theirs]))

    def test_deleting_what_the_other_side_changed_is_a_conflict(self):
        gone = BASE.replace(b"def second():\n    return 2\n\n\n", b"")
        changed = BASE.replace(b"return 2", b"return 20")
        self.assertIsNone(python_merge([BASE, gone, changed]))
        other = BASE.replace(b"return 1\n\n\ndef second", b"return 10\n\n\ndef second")
        self.assertEqual(python_merge([BASE, gone, other]), gone.replace(b"return 1\n", b"return 10\n", 1))

    def test_reordered_definitions_are_a_conflict(self):
        swapped = BASE.replace(b"def first():\n    return 1\n\n\ndef second():\n    return 2\n", b"def second():\n    return 2\n\n\ndef first():\n    return 1\n")
        self.assertIsNone(python_merge([BASE, swapped, BASE.replace(b"size = 1", b"size = 2")]))

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

    def test_code_after_the_last_definition_stays_once(self):
        base = b"def a():\n    pass\n\nprint(1)\n"
        ours = base + b"\ndef b():\n    pass\n"
        theirs = base + b"\ndef c():\n    pass\n"
        self.assertEqual(
            python_merge([base, ours, theirs]),
            base + b"\ndef b():\n    pass\n\ndef c():\n    pass\n",
        )

    def test_one_import_added_on_both_sides_comes_out_once(self):
        base = b"import a\n\n\ndef f():\n    pass\n"
        ours = base.replace(b"import a\n", b"import a\nimport os\n")
        theirs = base.replace(b"import a\n", b"import a\nimport os\nimport sys\n")
        self.assertEqual(python_merge([base, ours, theirs]), theirs)

    def test_text_between_definitions_merges_by_whole_lines_only(self):
        base = b"def a():\n    pass\n\nX = 1  # one\nY = 2\n\ndef b():\n    pass\n"
        ours = base.replace(b"# one", b"# first")
        theirs = base.replace(b"X = 1", b"X = 10")
        self.assertIsNone(python_merge([base, ours, theirs]))
        both_lines = base.replace(b"Y = 2", b"Y = 20")
        self.assertIsNone(python_merge([base, ours, both_lines]))
        apart = b"def a():\n    pass\n\nX = 1\n\n\nY = 2\n\ndef b():\n    pass\n"
        self.assertEqual(
            python_merge([apart, apart.replace(b"X = 1", b"X = 2"), apart.replace(b"Y = 2", b"Y = 3")]),
            apart.replace(b"X = 1", b"X = 2").replace(b"Y = 2", b"Y = 3"),
        )

    def test_plain_lines_inserted_at_one_spot_are_a_conflict(self):
        base = b"def a():\n    pass\n\nX = 1\n\ndef b():\n    pass\n"
        ours = base.replace(b"X = 1\n", b"X = 1\nY = 2\n")
        theirs = base.replace(b"X = 1\n", b"X = 1\nZ = 3\n")
        self.assertIsNone(python_merge([base, ours, theirs]))

    def test_registration(self):
        self.assertTrue(has_driver("pkg/mod.py"))
        self.assertFalse(has_driver("web/app.rb"))
        self.assertEqual(resolve_file("web/app.ts", [BASE] * 3), (None, None))
        ours = BASE.replace(b"return 2", b"return 3")
        self.assertEqual(resolve_file("m.py", [BASE, ours, BASE]), (ours, "python-outline"))


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
            self.assertEqual(engine.history[0]["resolved"][0]["method"], "python-outline")


if __name__ == "__main__":
    unittest.main()
