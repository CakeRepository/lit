"""End to end: collect, run and land a set of concurrent PRs, as the README says lit does.

GitHub is replaced by tests/e2e_github.py (local bare repositories and a small REST
server), so everything else is real: Git, the batch engine, the test command and
the landing push. Set LIT_BIN to run the same checks against the lit binary.

The PRs, all opened against the same main:
  #1 alice  adds a file nobody else touches
  #2 bob    changes add() in calc.py
  #3 carol  changes sub() in calc.py, in the same file as #2
  #4 dave   breaks mul(): fails on its own
  #5 erin   changes add() differently from #2: a real overlap
  #6 frank  renames square() to sq() and updates its caller
  #7 grace  adds a new caller of square(): passes alone, fails with #6
  #8 heidi  adds div() after mul(), right before the square() that #6 renames
  #9 ivan   adds mod() after mul(), at the same place as #8
"""

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from e2e_github import FakeGitHub, git, lit

CALC = textwrap.dedent('''\
    def add(a, b):
        return a + b


    def sub(a, b):
        return a - b


    def mul(a, b):
        return a * b


    def square(x):
        return x * x
    ''')
RUN_TESTS = textwrap.dedent('''\
    import importlib, pathlib, sys
    sys.path.insert(0, ".")
    import calc
    assert calc.add(2, 3) == 5
    assert calc.sub(5, 3) == 2
    assert calc.mul(2, 3) == 6
    for f in sorted(pathlib.Path("extras").glob("[!_]*.py")):
        importlib.import_module("extras." + f.stem).check()
    ''')
TEST = ["--test", "python3", "-B", "run_tests.py"]
ADD = "def add(a, b):\n    return a + b\n"
MUL = "def mul(a, b):\n    return a * b\n"


class Scenario:
    def __init__(self, root):
        self.root = Path(root)
        self.gh = FakeGitHub(self.root)
        self.work = self.root / "work"
        self.work.mkdir()
        w = self.work
        git(w, "init", "-q", "-b", "main")
        (w / "calc.py").write_text(CALC)
        (w / "run_tests.py").write_text(RUN_TESTS)
        (w / "extras").mkdir()
        (w / "extras/__init__.py").write_text("")
        (w / "extras/base.py").write_text("import calc\n\n\ndef check():\n    assert calc.square(3) == 9\n")
        git(w, "add", ".")
        git(w, "commit", "-qm", "base")
        git(w, "push", "-q", str(self.gh.remote), "main")
        self.base = git(w, "rev-parse", "HEAD")
        self.env = self.gh.env()
        self.heads = {}

    def close(self):
        self.gh.close()

    def edit(self, path, old, new):
        p = self.work / path
        text = p.read_text()
        assert old in text, (path, old)
        p.write_text(text.replace(old, new, 1))

    def write(self, path, text):
        (self.work / path).parent.mkdir(parents=True, exist_ok=True)
        (self.work / path).write_text(text)

    def pr(self, number, author, *edits):
        git(self.work, "checkout", "-q", "-B", f"pr{number}", "main")
        for edit in edits:
            edit()
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", f"change {number}")
        self.heads[number] = git(self.work, "rev-parse", "HEAD")
        self.gh.open_pull(number, author, self.work, f"pr{number}")

    def open_all(self):
        s = self
        s.pr(1, "alice", lambda: s.write("docs/one.md", "# One\n"))
        s.pr(2, "bob", lambda: s.edit("calc.py", ADD, 'def add(a, b):\n    """Sum."""\n    return a + b\n'))
        s.pr(3, "carol", lambda: s.edit("calc.py", "def sub(a, b):\n    return a - b\n",
                                        'def sub(a, b):\n    """Difference."""\n    return a - b\n'))
        s.pr(4, "dave", lambda: s.edit("calc.py", "return a * b", "return a * b + 1"))
        s.pr(5, "erin", lambda: s.edit("calc.py", ADD, "def add(a, b):\n    total = a + b\n    return total\n"))
        s.pr(6, "frank", lambda: s.edit("calc.py", "def square(x):", "def sq(x):"),
             lambda: s.edit("extras/base.py", "calc.square(3)", "calc.sq(3)"))
        s.pr(7, "grace", lambda: s.write("extras/cube.py",
                                         "import calc\n\n\ndef check():\n    assert calc.square(2) * 2 == 8\n"))
        s.pr(8, "heidi", lambda: s.edit("calc.py", MUL, MUL + "\n\ndef div(a, b):\n    return a / b\n"))
        s.pr(9, "ivan", lambda: s.edit("calc.py", MUL, MUL + "\n\ndef mod(a, b):\n    return a % b\n"))

    def lit(self, *args, ok=(0,)):
        proc = lit(self.root, self.env, *args)
        if proc.returncode not in ok:
            raise AssertionError(f"lit {' '.join(args)} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def collect(self, *numbers):
        if not (self.root / "batch.git").exists():
            git(self.root, "clone", "-q", "--bare", str(self.gh.remote), "batch.git")
        self.lit("collect", self.gh.repository, "--repo", "batch.git", "--out", "queue.json",
                 "--prs", *map(str, numbers))
        return json.loads((self.root / "queue.json").read_text())

    def run(self):
        proc = self.lit("run", "queue.json", "--repo", "batch.git", "--state", "state", *TEST, ok=(0, 1))
        receipt = Path(proc.stdout.strip().splitlines()[-1])
        attempts = json.loads((receipt.parent / "attempts.json").read_text())
        return receipt, json.loads(receipt.read_text()), attempts

    def remote_main(self):
        return self.gh.ref("refs/heads/main")

    def passes(self, commit):
        """Run the suite on a fresh checkout of commit, outside lit."""
        check = Path(tempfile.mkdtemp(dir=self.root))
        git(self.root, "clone", "-q", "--shared", "--no-checkout", str(self.root / "batch.git"), str(check))
        git(check, "checkout", "-q", "--detach", commit)
        import subprocess
        return subprocess.run(["python3", "-B", "run_tests.py"], cwd=check, capture_output=True).returncode == 0


def returned(attempts):
    """PR number -> the commit it was handed back on."""
    return {a["returned"]["number"]: a["returned"]["tested_on"] for a in attempts if "returned" in a}


def pending(attempts):
    return {p["number"] for a in attempts for p in a.get("pending", ())}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Scenario(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.s.close)
        self.s.open_all()

    def test_lands_what_passes_together_and_hands_back_the_rest(self):
        s = self.s
        snapshot = s.collect(*range(1, 10))
        self.assertEqual(snapshot["base"], s.base)
        self.assertEqual({c["number"]: c["head"] for c in snapshot["changes"]}, s.heads)

        receipt, result, attempts = s.run()
        # #1 disjoint, #2 and #3 in one file, #6 rename, #8 and #9 adding at the same spot.
        self.assertEqual(sorted(result["tested"]), [1, 2, 3, 6, 8, 9])
        back = returned(attempts)
        passing = {a["candidate"] for a in attempts if a["result"] == "pass"}
        # Each goes back with the commit it failed on top of, which itself passed.
        self.assertEqual(set(back), {4, 7})
        self.assertTrue(set(back.values()) <= passing | {s.base})
        self.assertFalse(s.passes(s.heads[4]), "#4 fails on its own")
        self.assertTrue(s.passes(s.heads[7]), "#7 passes on its own and fails only with #6")
        self.assertEqual(pending(attempts) - set(result["tested"]), {5}, "the real overlap is held")
        conflicts = [json.loads(p.read_text()) for p in (s.root / "state" / "conflicts").glob("*.json")]
        self.assertTrue(conflicts and all(c["incoming"]["number"] == 5 for c in conflicts))
        self.assertTrue(all(c["path"] == "calc.py" for c in conflicts))
        merged = git(s.root / "batch.git", "show", result["final"] + ":calc.py")
        self.assertIn("def div(a, b):", merged)
        self.assertIn("def mod(a, b):", merged)
        self.assertIn('"""Sum."""', merged)
        self.assertIn('"""Difference."""', merged)

        # The result had a passing run on its own commit, and passes again outside lit.
        self.assertIn(result["final"], passing)
        self.assertTrue(s.passes(result["final"]))
        self.assertEqual(s.remote_main(), s.base, "run leaves the target alone")

        s.lit("land", str(receipt), "--repo", "batch.git")
        main = s.remote_main()
        self.assertEqual(main, result["final"])
        log = git(s.gh.remote, "log", "--format=%P%n%B", s.base + ".." + main)
        for n in (1, 2, 3, 6, 8, 9):
            self.assertIn(s.heads[n], log, f"PR #{n}'s own head is a parent on main")
            self.assertIn(f"Pull-Request: #{n}\n", log)
        for n in (4, 5, 7):
            self.assertNotIn(s.heads[n], git(s.gh.remote, "rev-list", main))

    def test_land_refuses_when_main_moved_after_the_run(self):
        s = self.s
        s.collect(1, 2)
        receipt, result, _ = s.run()
        self.assertEqual(sorted(result["tested"]), [1, 2])
        git(s.work, "checkout", "-q", "main")
        s.write("other.txt", "landed meanwhile\n")
        git(s.work, "add", "-A")
        git(s.work, "commit", "-qm", "someone else landed")
        git(s.work, "push", "-q", str(s.gh.remote), "main")
        moved = s.remote_main()
        proc = s.lit("land", str(receipt), "--repo", "batch.git", ok=(2,))
        self.assertIn("collect and test again", proc.stderr)
        self.assertEqual(s.remote_main(), moved, "the remote keeps the commit that won the race")

    def test_land_refuses_a_pr_pushed_after_the_run(self):
        s = self.s
        s.collect(1, 2)
        receipt, result, _ = s.run()
        s.pr(2, "bob", lambda: s.write("late.txt", "pushed after review\n"))
        proc = s.lit("land", str(receipt), "--repo", "batch.git", ok=(2,))
        self.assertIn("collect and test again", proc.stderr)
        self.assertEqual(s.remote_main(), s.base)

    def test_authors_agree_on_the_overlap_and_it_lands(self):
        s = self.s
        s.collect(2, 5)
        _, result, attempts = s.run()
        self.assertEqual(result["tested"], [2])
        conflict = max((s.root / "state" / "conflicts").glob("*.json"), key=lambda p: p.stat().st_mtime)
        record = json.loads(conflict.read_text())
        agreed = s.root / "agreed.py"
        agreed.write_text(CALC.replace(ADD, 'def add(a, b):\n    """Sum."""\n    total = a + b\n    return total\n'))
        for who in record["authors"]:
            s.lit("agree", record["id"], who, str(agreed), "--repo", "batch.git", "--state", "state")
        receipt, result, attempts = s.run()
        self.assertEqual(sorted(result["tested"]), [2, 5])
        self.assertEqual(git(s.root / "batch.git", "show", result["final"] + ":calc.py"), agreed.read_text().rstrip("\n"))
        s.lit("land", str(receipt), "--repo", "batch.git")
        self.assertEqual(s.remote_main(), result["final"])

    @unittest.skipUnless(os.environ.get("LIT_BIN"), "lit parity is part of the binary")
    def test_native_collect_matches_both_apis_and_queue_pagination(self):
        s = self.s
        git(s.root, "init", "-q", "--bare", "batch.git")
        args = ["collect", s.gh.repository, "--out", "snapshot.json", "--repo", "batch.git"]

        def compare(options, expected=0):
            result = s.lit("parity", "--", *args, *options)
            self.assertIn(f"matches between Python and Rust (exit {expected})", result.stdout)

        s.env["LIT_GITHUB_API"] = "auto"
        s.gh.pulls[1].update(body="Agent: alice-2", labels=["ready"], association="NONE")
        options = ["--prs", "1", "--label", "ready", "--hold-outside", "--author-pattern", "^Agent: (.+)$"]
        compare(options)
        compare([*options, "--allow-outside", "1"])
        compare(["--prs", "999"], 2)
        self.assertIn(("POST", "/graphql"), s.gh.calls)
        self.assertTrue(any(method == "GET" for method, _ in s.gh.calls))

        def page(query, variables):
            n = 2 if variables["after"] else 1
            pr = dict(number=n, headRefOid=s.heads[n], author=dict(login="agent"), body="",
                      baseRefName="main", isDraft=False, state="OPEN", authorAssociation="MEMBER",
                      labels=dict(nodes=[]), latestOpinionatedReviews=dict(nodes=[
                          dict(state="APPROVED", author=dict(login="reviewer"), commit=dict(oid=s.heads[n]))
                      ]))
            info = dict(hasNextPage=n == 1, endCursor=str(n))
            data = dict(ref=dict(target=dict(oid=s.base)))
            if "mergeQueue(" in query:
                entry = dict(state="QUEUED", headCommit=dict(oid=s.heads[n], tree=dict(oid=s.base)),
                             pullRequest=pr)
                data["mergeQueue"] = dict(entries=dict(nodes=[entry], pageInfo=info))
            else:
                data["pullRequests"] = dict(nodes=[pr], pageInfo=info)
            return dict(data=dict(repository=data))

        s.gh.graphql = page
        s.gh.calls.clear()
        compare(["--min-reviews", "1"])
        compare([])
        self.assertTrue(all(method == "POST" for method, _ in s.gh.calls))
        s.env["LIT_PYTHON"] = str(s.root / "no-python")
        s.lit(*args)
        snapshot = json.loads((s.root / "snapshot.json").read_text(encoding="utf8"))
        self.assertEqual([g["number"] for g in snapshot["groups"]], [1, 2])
        for oid in (s.base, s.heads[1], s.heads[2]):
            self.assertEqual(git(s.root / "batch.git", "rev-parse", "refs/concurrent-merge/inputs/" + oid), oid)

    @unittest.skipUnless(os.environ.get("LIT_BIN"), "lit parity is part of the binary")
    def test_parity_of_collect_and_run(self):
        """The commands this scenario drives give the same result run twice from the
        same inputs, and, once they move to Rust, the same result in Rust as in Python."""
        s = self.s
        git(s.root, "clone", "-q", "--bare", str(s.gh.remote), "batch.git")
        collect = ["collect", s.gh.repository, "--repo", "batch.git", "--out", "queue.json",
                   "--prs", *map(str, range(1, 10))]
        for args in (collect, ["run", "queue.json", "--repo", "batch.git", "--state", "state", *TEST]):
            proc = lit(s.root, s.env, "parity", "--", *args)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("match", proc.stdout)
            if args is collect:
                s.lit(*collect)


if __name__ == "__main__":
    unittest.main()
