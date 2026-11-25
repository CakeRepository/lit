import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

from concurrent_merge import js_symbols
from concurrent_merge.cli import write_shadow
from concurrent_merge.engine import Repo
from concurrent_merge.shadow import MERGED, Contents, by_file, program, shadow, writes
from concurrent_merge.symbols import digests

IDENTITY = {
    "GIT_AUTHOR_NAME": "lit", "GIT_AUTHOR_EMAIL": "lit@example.invalid",
    "GIT_COMMITTER_NAME": "lit", "GIT_COMMITTER_EMAIL": "lit@example.invalid",
}

DB = "export function query() { return 1 }\nexport function close() { return 2 }\n"


class ShadowTests(unittest.TestCase):
    """What Frontier would decide for a real snapshot, from a real unit store. Changes nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.work = root / "work"
        self.store = root / "store"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)], check=True)
        self.repo = Repo(self.work)
        self.base = self.commit({
            "src/db.ts": DB,
            "src/a.py": "a = 1\n",
            "src/b.py": "b = 1\n",
            "tests/test_db.ts": "query()\n",
            "tests/test_a.py": "import a\n",
            "tests/test_b.py": "import b\n",
            "README.md": "lit\n",
        })
        # The store: each test's passing footprint on main. test_db ran only query() in db.ts.
        self.record({
            "tests/test_db.ts": (["src/db.ts", "tests/test_db.ts"], {"src/db.ts": ["query"]}),
            "tests/test_a.py": (["src/a.py", "tests/test_a.py"], None),
            "tests/test_b.py": (["src/b.py", "tests/test_b.py"], None),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.work), "-c", "commit.gpgSign=false", *args],
                              check=True, capture_output=True, text=True,
                              env={**os.environ, **IDENTITY}).stdout.strip()

    def commit(self, files, parent=None, branch="main"):
        """Commit `files` on `branch`, started from `parent` when given (None content deletes)."""
        if parent:
            self.git("checkout", "-q", "-B", branch, parent)
        elif self.git("branch", "--list", "main"):
            self.git("checkout", "-q", "main")
        for path, text in files.items():
            target = self.work / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", branch)
        return self.git("rev-parse", "HEAD")

    def oid(self, commit, path):
        return self.git("rev-parse", f"{commit}:{path}")

    def record(self, units):
        job = self.store / "suite"
        (job / "units").mkdir(parents=True)
        (job / "symbols").mkdir()
        tracked = {}
        for i, (name, (reads, ran)) in enumerate(units.items()):
            for path in reads:
                oid = self.oid(self.base, path)
                tracked[path] = ["100644", oid]
                if ran and path in ran:
                    table = js_symbols.table((self.work / path).read_bytes(), ".ts")
                    (job / "symbols" / (oid + ".json")).write_text(json.dumps(digests(table)))
            unit = dict(tracked=reads, seconds=1.0)
            if ran:
                unit["symbols"] = ran
            bundle = dict(kind="lit-unit-footprints", commit=self.base,
                          tracked={p: tracked[p] for p in reads}, units={name: unit})
            (job / "units" / f"{i}.json").write_text(json.dumps(bundle))

    def snapshot(self, heads, base=None):
        return dict(base=base or self.base,
                    changes=[dict(number=n, head=h, author="agent") for n, h in heads.items()])

    def decide(self, heads, base=None, receipt=None):
        return shadow(self.repo, self.snapshot(heads, base), self.store, "suite", receipt)

    def test_independent_changes_form_their_own_components(self):
        one = self.commit({"src/a.py": "a = 2\n"}, self.base, "one")
        two = self.commit({"src/b.py": "b = 2\n"}, self.base, "two")
        docs = self.commit({"README.md": "lit, faster\n"}, self.base, "docs")
        d = self.decide({1: one, 2: two, 3: docs})["decision"]
        groups = {tuple(c["members"]): c for c in d["components"]}
        self.assertEqual(set(groups), {(1,), (2,), (3,)})
        self.assertEqual(groups[(1,)]["needs"], ["tests/test_a.py"])
        self.assertEqual(groups[(2,)]["needs"], ["tests/test_b.py"])
        # No stored unit read the README, so Frontier would land it without running anything.
        self.assertEqual(groups[(3,)]["needs"], [])
        self.assertEqual(d["unrun"], [3])
        self.assertEqual(d["stale"], [])

    def test_a_function_no_test_ran_needs_nothing_and_one_it_ran_needs_its_test(self):
        close = self.commit({"src/db.ts": DB.replace("return 2", "return 3")}, self.base, "close")
        query = self.commit({"src/db.ts": DB.replace("return 1", "return 4")}, self.base, "query")
        d = self.decide({1: close})["decision"]
        self.assertEqual(d["unrun"], [1])
        d = self.decide({2: query})["decision"]
        self.assertEqual(d["components"][0]["needs"], ["tests/test_db.ts"])

    def test_a_unit_shared_by_two_changes_joins_them(self):
        one = self.commit({"src/a.py": "a = 2\n"}, self.base, "one")
        both = self.commit({"src/a.py": "a = 1\n# x\n", "tests/test_a.py": "import a  # y\n"}, self.base, "both")
        d = self.decide({1: one, 2: both})["decision"]
        # The two PRs write the same file, so the later one is a write-write conflict.
        self.assertEqual(d["conflicts"], {2: dict(inputs=["src/a.py"], held_by=[1])})
        three = self.commit({"tests/test_a.py": "import a  # z\n"}, self.base, "three")
        d = self.decide({1: one, 3: three})["decision"]
        self.assertEqual([c["members"] for c in d["components"]], [[1, 3]])
        self.assertEqual(d["components"][0]["overlaps"], ["tests/test_a.py"])

    def test_a_test_whose_inputs_changed_on_main_is_stale(self):
        main = self.commit({"src/b.py": "b = 3\n"})
        one = self.commit({"src/a.py": "a = 2\n"}, main, "one")
        d = self.decide({1: one}, base=main)["decision"]
        self.assertEqual(d["stale"], ["tests/test_b.py"])
        self.assertEqual(d["components"][0]["needs"], ["tests/test_a.py"])

    def test_an_input_main_and_the_change_both_changed_is_unproven(self):
        old = self.commit({"src/a.py": "a = 2\n"}, self.base, "old")
        main = self.commit({"src/a.py": "a = 3\n"}, self.base, "newmain")
        contents = Contents(self.repo)
        f = program(self.repo, self.store, "suite", main, contents)
        w = writes(self.repo, contents, by_file(f.readers), main, old)
        # What lands is neither side's content, so no certificate can match it.
        self.assertEqual(list(w), ["src/a.py"])
        self.assertTrue(w["src/a.py"].startswith(MERGED))
        d = self.decide({1: old}, base=main)["decision"]
        self.assertEqual(d["components"][0]["needs"], ["tests/test_a.py"])

    def test_the_command_prints_the_decision(self):
        one = self.commit({"src/a.py": "a = 2\n"}, self.base, "one")
        path = Path(self.tmp.name) / "queue.json"
        path.write_text(json.dumps(self.snapshot({1: one})))
        out = subprocess.run(
            [sys.executable, "-m", "concurrent_merge", "shadow", str(path), "--repo", str(self.work),
             "--store", str(self.store), "--job", "suite"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertEqual(json.loads(out)["components"][0]["needs"], ["tests/test_a.py"])

    def test_beside_a_run_it_reports_what_frontier_would_have_skipped_that_failed(self):
        docs = self.commit({"README.md": "lit, faster\n"}, self.base, "docs")
        one = self.commit({"src/a.py": "a = 2\n"}, self.base, "one")
        receipt = dict(tested=[2])
        result = self.decide({1: docs, 2: one}, receipt=receipt)
        self.assertEqual(result["outcome"]["passed"], [2])
        self.assertEqual(result["outcome"]["failed"], [1])
        self.assertEqual(result["outcome"]["unrun_but_failed"], [1])
        self.assertEqual(result["outcome"]["component_runs"], 1)

    def test_a_run_keeps_its_outcome_when_the_shadow_fails(self):
        directory = Path(self.tmp.name) / "batch-x"
        directory.mkdir()
        one = self.commit({"src/a.py": "a = 2\n"}, self.base, "one")
        snapshot = self.snapshot({1: one})
        (directory / "result.json").write_text(json.dumps(dict(snapshot=snapshot, tested=[1])))
        (directory / "attempts.json").write_text(json.dumps([dict(result="pass", members=[1])]))
        write_shadow(self.repo, snapshot, self.store, "suite", directory)
        written = json.loads((directory / "shadow.json").read_text())
        self.assertEqual(written["outcome"]["suite_runs"], 1)
        self.assertEqual(written["outcome"]["unrun_but_failed"], [])
        bad = dict(snapshot, base="0" * 40)
        write_shadow(self.repo, bad, self.store, "suite", directory)
        self.assertIn("error", json.loads((directory / "shadow.json").read_text()))


if __name__ == "__main__":
    unittest.main()
