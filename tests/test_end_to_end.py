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
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from e2e_github import FakeGitHub, git, lit
from test_footprint import traced

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
        with patch.dict(s.env, LIT_PYTHON=str(s.root / "no-python")):
            snapshot = s.collect(*range(1, 10))
            receipt, result, attempts = s.run()
        self.assertEqual(snapshot["base"], s.base)
        self.assertEqual({c["number"]: c["head"] for c in snapshot["changes"]}, s.heads)

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

        if os.environ.get("LIT_BIN"):
            comparison = s.lit("parity", "--", "land", str(receipt), "--repo", "batch.git")
            self.assertIn("matches between Python and Rust (exit 0)", comparison.stdout)
            self.assertEqual(s.remote_main(), s.base, "parity must only push to its private remotes")
            s.env["LIT_PYTHON"] = str(s.root / "no-python")
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
        if os.environ.get("LIT_BIN"):
            proc = s.lit("parity", "land", str(receipt), "--repo", "batch.git")
            self.assertIn("matches between Python and Rust (exit 2)", proc.stdout)
        proc = s.lit("land", str(receipt), "--repo", "batch.git", ok=(2,))
        self.assertIn("collect and test again", proc.stderr)
        self.assertEqual(s.remote_main(), moved, "the remote keeps the commit that won the race")

    @unittest.skipUnless(os.name == "posix" and os.environ.get("LIT_BIN"), "requires the native binary and a Git-only PATH")
    def test_collect_run_and_land_with_only_git_on_path(self):
        s = self.s
        programs = s.root / "programs"
        programs.mkdir()
        (programs / "git").symlink_to(shutil.which("git"))
        with patch.dict(s.env, PATH=str(programs), LIT_PYTHON=str(s.root / "no-python")):
            s.collect(1, 2, 3)
            run = s.lit("run", "queue.json", "--repo", "batch.git", "--state", "state",
                        "--test", "git", "ls-files", "--error-unmatch", "calc.py")
            receipt = Path(run.stdout.strip())
            result = json.loads(receipt.read_text())
            self.assertEqual(result["tested"], [1, 2, 3])
            self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)
            s.lit("land", str(receipt), "--repo", "batch.git")
        self.assertEqual(s.remote_main(), result["final"])

    @unittest.skipUnless(os.environ.get("LIT_BIN"), "requires the parity harness")
    def test_run_records_infrastructure_failure_and_shadow_observation(self):
        s = self.s
        s.collect(1, 2)
        flags = ["run", "queue.json", "--repo", "batch.git", "--state", "state"]
        for extra, expected in [(["--test", "git", "rev-parse", "--verify", "refs/heads/missing"], 2),
                                (["--shadow-store", "empty-store", "--shadow-job", "tests",
                                  "--test", "git", "diff", "--exit-code"], 0)]:
            result = s.lit("parity", *flags, *extra)
            self.assertIn(f"matches between Python and Rust (exit {expected})", result.stdout)

    @unittest.skipUnless(os.environ.get("LIT_BIN"), "requires the binary parity harness")
    def test_proposes_only_the_tested_topic_and_recovers_the_draft(self):
        s = self.s
        s.collect(1, 2)
        receipt, result, _ = s.run()
        head = "lit/batch-example"
        (s.root / "notes.md").write_text("Reviewed together.\n")
        args = ["propose", str(receipt), "--repo", "batch.git", "--head", head,
                "--title", "Two reviewed changes", "--notes", "notes.md"]
        s.env["LIT_GITHUB_API"] = "auto"
        comparison = s.lit("parity", *args)
        self.assertIn("matches between Python and Rust (exit 0)", comparison.stdout)
        self.assertFalse(s.gh.drafts)
        self.assertIsNone(s.gh.ref("refs/heads/" + head))
        s.env["LIT_PYTHON"] = str(s.root / "no-python")
        first = s.lit(*args)
        self.assertEqual(first.stdout.strip(), s.gh.drafts[head]["html_url"])
        self.assertTrue(s.gh.drafts[head]["draft"])
        self.assertEqual(s.gh.drafts[head]["title"], "Two reviewed changes")
        self.assertIn(result["final"], s.gh.drafts[head]["body"])
        self.assertIn("Reviewed together.", s.gh.drafts[head]["body"])
        self.assertEqual(s.lit(*args).stdout, first.stdout)
        self.assertEqual(s.gh.calls.count(("POST", f"/repos/{s.gh.repository}/pulls")), 1)
        self.assertEqual(s.gh.ref("refs/heads/" + head), result["final"])
        self.assertEqual(s.remote_main(), s.base)

        def graphql(query, variables):
            if "createPullRequest(" in query:
                self.assertEqual(variables["base"], "main")
                self.assertEqual(variables["head"], "lit/another")
                self.assertIn(result["final"], variables["body"])
                return dict(data=dict(createPullRequest=dict(pullRequest=dict(url="local-draft-url"))))
            if "headRefName:$head" in query:
                return dict(data=dict(repository=dict(pullRequests=dict(nodes=[]))))
            if "repository(owner:$o,name:$n){id}" in query:
                return dict(data=dict(repository=dict(id="R1")))
            prs = [dict(number=n, headRefOid=s.heads[n], author=dict(login="agent"),
                        isDraft=False, baseRefName="main") for n in (1, 2)]
            return dict(data=dict(repository=dict(ref=dict(target=dict(oid=s.base)),
                        pullRequests=dict(nodes=prs, pageInfo=dict(hasNextPage=False)))))

        s.gh.graphql = graphql
        s.env["LIT_GITHUB_API"] = "graphql"
        args[args.index(head)] = "lit/another"
        self.assertEqual(s.lit(*args).stdout.strip(), "local-draft-url")

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
    def test_preview_matches_the_queue_anchor_and_refuses_a_changed_source(self):
        s = self.s
        git(s.root, "init", "-q", "--bare", "preview.git")
        predecessor = s.heads[1]
        tree = git(s.gh.remote, "rev-parse", predecessor + "^{tree}")
        reads = 0
        move_source = False

        def graphql(query, variables):
            nonlocal reads
            if "mergeQueue(" in query:
                entry = dict(state="AWAITING_CHECKS", headCommit=dict(oid=predecessor, tree=dict(oid=tree)),
                             pullRequest=dict(number=1, headRefOid=predecessor, author=dict(login="alice"),
                                              isDraft=False, state="OPEN", baseRefName="main"))
                queue = dict(entries=dict(nodes=[entry], pageInfo=dict(hasNextPage=False)))
                return dict(data=dict(repository=dict(ref=dict(target=dict(oid=s.base)), mergeQueue=queue)))
            reads += 1
            found = {}
            for n in (3, 6):
                oid = s.base if move_source and reads % 2 == 0 else s.heads[n]
                found[f"p{n}"] = dict(number=n, headRefOid=oid, author=dict(login=f"agent{n}"),
                                      state="OPEN", baseRefName="main", isInMergeQueue=False,
                                      isCrossRepository=False, headRepository=dict(nameWithOwner=s.gh.repository),
                                      reviewThreads=dict(nodes=[], pageInfo=dict(hasNextPage=False)))
                found[f"r{n}"] = None
            return dict(data=dict(repository=found))

        s.gh.graphql = graphql
        args = ["preview", s.gh.repository, "6", "3", "--repo", "preview.git", "--out", "prediction.json"]
        self.assertIn("matches between Python and Rust (exit 0)", s.lit("parity", *args).stdout)
        move_source = True
        self.assertIn("matches between Python and Rust (exit 2)", s.lit("parity", *args).stdout)
        move_source = False
        s.env["LIT_PYTHON"] = str(s.root / "no-python")
        prediction = json.loads(s.lit(*args).stdout)
        self.assertEqual(prediction["anchor"], predecessor)
        self.assertEqual(prediction["main"], s.base)
        self.assertNotEqual(prediction["from_main"]["tree"], prediction["from_queue"]["tree"])
        self.assertEqual(s.remote_main(), s.base)

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



@unittest.skipUnless(os.environ.get("LIT_BIN") and shutil.which("ssh-keygen"), "native signed proof parity")
class SignedProofCLI(unittest.TestCase):
    def test_interoperable_signatures_quorum_book_and_leased_pushes(self):
        import hashlib
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            git(work, "init", "-q", "-b", "main")
            remote = str(root / "remote.git")
            git(root, "init", "-q", "--bare", "-b", "main", remote)
            git(work, "remote", "add", "origin", remote)
            signers = work / ".lit" / "allowed_signers"
            signers.parent.mkdir()
            for who in ("one", "two"):
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(work / who)], check=True)
            signers.write_text("".join(f'{who} namespaces="lit-proof" {(work / (who + ".pub")).read_text()}'
                                       for who in ("one", "two")), encoding="utf8")
            git(work, "add", ".lit")
            git(work, "commit", "-qm", "trusted signers")
            commit = git(work, "rev-parse", "HEAD")
            tree = git(work, "rev-parse", "HEAD^{tree}")
            units = work / "store" / "tests" / "units"
            units.mkdir(parents=True)
            # Cross a DEFLATE block boundary. Small records can produce
            # identical bytes with compressors that diverge on real ledgers.
            tracked = {f"src/{i}.py": ["100644", hashlib.sha256(str(i).encode()).hexdigest()] for i in range(900)}
            bundle = dict(kind="lit-unit-footprints", commit=commit, tree=tree, tracked=tracked,
                          units={"tests/café": dict(exit_code=0, incomplete=[], tracked=list(tracked), seconds=1.25)})
            (units / "pass.json").write_text(json.dumps(bundle), encoding="utf8")
            (units / "stale.json").write_text(json.dumps(dict(bundle, commit="b"*40)), encoding="utf8")

            def command(*args, code=0, compare=True):
                if compare:
                    before = git(work, "ls-remote", "origin")
                    result = lit(work, {}, "parity", "proof", *args)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"(exit {code})", result.stdout)
                    self.assertEqual(git(work, "ls-remote", "origin"), before)
                result = lit(work, {"LIT_PYTHON": str(root / "no-python")}, "proof", *args)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                return result.stdout.strip()

            proof = "proof.json.gz"
            command("pack", "--store", "store", "--job", "tests", "--repository", "o/r",
                    "--image", "image", "--out", proof)
            common = ["--repository", "o/r", "--commit", commit, "--image", "image", "--signers-at", "HEAD"]
            for who in ("one", "two"):
                command("sign", proof, "--key", who)
                self.assertIn("Took 1 records", command("verify", proof, *common, "--into", "local"))
                command("verify", proof, *common, "--repository", "other/repo", "--into", "unused", code=1)
                command("verify", proof, *common, "--max-age-hours", "-1", "--into", "expired", code=1)
                command("verify", proof, *common, "--max-age-hours", "nan", "--into", "invalid", code=2)
                fingerprint = subprocess.check_output(["ssh-keygen", "-lf", str(work / (who + ".pub"))], text=True).split()[1]
                (work / "revoked").write_text(fingerprint)
                command("verify", proof, *common, "--revoked", "revoked", "--into", "revoked-store", code=1)
                reference = command("push", proof, "--remote", "origin")
                self.assertIn(commit, reference)
                self.assertEqual(command("push", proof, "--remote", "origin"), reference)
                if who == "one":
                    command("take", "--remote", "origin", *common, "--quorum", "2", "--into", "waiting")
                    self.assertFalse((work / "waiting/tests/units").exists())
            sig = work / (proof + ".sig")
            signed = sig.read_bytes()
            command("sign", proof, "--key", "missing-key", code=2)
            self.assertEqual(sig.read_bytes(), signed)
            self.assertIn("Took 1 tests files", command("take", "--remote", "origin", *common, "--quorum", "2", "--into", "verified"))
            index = json.loads((work / "verified/tests/proof-signers.json").read_text())
            self.assertEqual(index, {"tests/café": ["one", "two"]})
            (work / "reset.json").write_text(json.dumps(dict(kind="lit-audit-book", signers={
                "one": {"reset": "2099-01-01T00:00:00+00:00"}})))
            command("take", "--remote", "origin", *common, "--quorum", "2", "--audit-book", "reset.json", "--into", "after-reset")
            self.assertFalse((work / "after-reset/tests/units").exists())
            command("book-pull", "book.json", "--remote", "origin")
            command("book-push", "book.json", "--remote", "origin")
            command("book-pull", "fresh.json", "--remote", "origin")
            self.assertEqual(json.loads((work / "book.json").read_text()), json.loads((work / "fresh.json").read_text()))
            original = (work / proof).read_bytes()
            (work / proof).write_bytes(original + b"changed")
            self.assertIn("signature does not match", command("verify", proof, *common, "--into", "bad", code=1))
            self.assertFalse((work / "bad").exists())


class FootprintSelectionCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "src").mkdir()
        (self.repo / "src/a.ts").write_text("pass")
        (self.repo / "tests").mkdir()
        for name in ("one", "two", "three"):
            (self.repo / "tests" / name).write_text(name)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "recorded")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.tracked = {"src/a.ts": ["100644", git(self.repo, "rev-parse", "HEAD:src/a.ts")]}
        self.unit = dict(exit_code=0, seconds=1.25, incomplete=[], network=[], reads_git_metadata=False,
                         directories=["src"], absent=["src/a.tsx"], listings={}, untracked={}, external={})

    def command(self, *args, code=0):
        if os.environ.get("LIT_BIN"):
            pair = lit(self.root, {}, "parity", *args)
            self.assertEqual(pair.returncode, 0, pair.stdout + pair.stderr)
            self.assertIn(f"(exit {code})", pair.stdout)
        proc = lit(self.root, {"LIT_PYTHON": str(self.root / "unavailable")}, *args)
        self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
        return json.loads(proc.stdout) if code != 2 else proc.stderr

    def test_reuse_verifies_the_actual_checkout_and_scores_unsound_predictions(self):
        manifest = dict(self.unit, kind="lit-job-footprint", version=1, commit=self.base,
                        tracked=self.tracked, command=["sh", "-c", 'test "$(cat src/a.ts)" = pass'],
                        ignored_environment=["*"], environment={})
        def write(**updates):
            (self.root / "record.json").write_text(json.dumps(dict(manifest, **updates)))
        write()
        (self.repo / "src/a.ts").write_text("fail")
        (self.repo / "src/a.tsx").write_text("new")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "changed inputs")
        args = ["reuse", "record.json", "--repo", "repo", "--candidate", self.base, "HEAD",
                "--environment", "--host", "--out", "out/result.json"]
        passed, changed = self.command(*args, code=1)
        self.assertEqual(passed["checked"], ["checkout", "environment", "host"])
        self.assertEqual(changed["reasons"], [{"kind": "changed", "path": "src/a.ts"},
                                               {"kind": "appeared", "path": "src/a.tsx"}])
        self.assertEqual(json.loads((self.root / "out/result.json").read_text()), [passed, changed])
        import hashlib
        feature = "ZZ_LIT_FOOTPRINT_FEATURE"
        write(ignored_environment=["[!Z]*", *(k for k in os.environ if k.startswith("Z") and k != feature)],
              environment={feature: hashlib.sha256(b"on").hexdigest()})
        with patch.dict(os.environ, {feature: "on"}):
            self.assertEqual(self.command(*args, code=1)[0]["verdict"], "reusable")
        with patch.dict(os.environ, {feature: "off"}):
            self.assertEqual(self.command(*args, code=1)[0]["reasons"], [{"kind": "environment", "name": feature}])
        write(network=["AF_INET"], incomplete=["lost observation"], git_static={".git": "dir"},
              reads_git_metadata=True, git_metadata=[".git/HEAD"], untracked={"build/input": "absent"},
              external={"/etc/hosts": "wrong", str(self.root / "missing"): "wrong"})
        changed = self.command(*args, code=1)[1]
        self.assertEqual([r["kind"] for r in changed["reasons"]],
                         ["incomplete", "network", "revision", "revision", "changed", "appeared", "untracked", "host"])
        write(network=["AF_INET"])
        self.assertEqual(self.command(*args, "--allow-network", code=1)[0]["verdict"], "reusable")
        if shutil.which("strace") and not traced():
            write()
            passed, changed = self.command(*args, "--verify")
            self.assertEqual([passed["actual_exit_code"], changed["actual_exit_code"]], [0, 1])
            write(tracked={}, absent=[])
            changed = self.command(*args, "--verify", code=1)[1]
            self.assertFalse(changed["sound"])
            self.assertEqual(changed["verdict"], "reusable")

    def test_selects_new_changed_failed_and_legacy_name_lookups(self):
        import hashlib
        digest = lambda b: hashlib.sha256(b).hexdigest()
        unit = dict(self.unit, tracked=list(self.tracked), external=[], listings={"src": digest(b"a.ts")},
                    listing_keys={"src": ["a", "a.ts"]}, untracked={"deps/lock": digest(b"dep")})
        bundle = dict(kind="lit-unit-footprints", version=1, commit=self.base, command=["sh", "tests"],
                      units={"one": unit, "two": dict(unit, exit_code=2), "gone": unit},
                      tracked=self.tracked, external={}, environment={}, ignored_environment=["*"],
                      listing_children={"src": ["a.ts"]})
        def write():
            (self.root / "bundle.json").write_text(json.dumps(bundle))
        write()
        (self.repo / "deps").mkdir()
        (self.repo / "deps/lock").write_text("dep")
        (self.repo / "src/a.test.ts").write_text("irrelevant")
        args = ["select", "bundle.json", "--workdir", "repo", "--listings", "names",
                "--discover", "tests/[!x]*", "--strip", "tests/", "--host", "--environment",
                "--out", "result.json", "--list", "list.txt"]
        def select(*extra):
            summary = self.command(*args, *extra)
            result = json.loads((self.root / "result.json").read_text())
            self.assertEqual(summary["run"], len(result["run"]))
            self.assertEqual((self.root / "list.txt").read_text().splitlines(), result["run"])
            return result
        picked = select()
        self.assertEqual(picked["run"], ["three", "two"])
        self.assertEqual(picked["reusable"], ["one"])
        self.assertEqual(picked["seconds_recorded_reusable"], 1.2)
        self.assertEqual(select("--listings", "exact")["reasons"]["one"], [{"kind": "listing", "path": "src"}])
        unit.pop("listing_keys")
        unit["listing_lookups"] = {"src": ["a"]}
        write()
        self.assertEqual(select()["run"], ["one", "three", "two"])
        (self.repo / "deps/lock").write_text("changed")
        self.assertIn({"kind": "changed", "path": "deps/lock"}, select()["reasons"]["one"])
        (self.repo / "src/a.ts").write_text("dirty")
        self.assertIn("Tracked files differ", self.command(*args, code=2))


@unittest.skipUnless(shutil.which("strace") and not traced(), "syscall recording needs an available tracer")
class FootprintRecordingCLI(unittest.TestCase):
    def test_job_cache_shadow_enforce_audit_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp)
            root = outer / "fixtures"
            repo = root / "repo"
            repo.mkdir(parents=True)
            git(repo, "init", "-q", "-b", "main")
            (repo / "input").write_text("ok")
            (repo / "ci.sh").write_text(
                'cat input; printf executed > ran; test -z "$ZZ_LIT_BREAK" || exit 9\n')
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "cacheable job")
            marker = repo / "ran"
            store = outer / "persistent store"
            def run(mode, rate="0", broken=False, code=0):
                argv = ["cached-run", "--workdir", str(repo), "--store", str(store), "--job", "suite",
                        "--audit-rate", rate, "--mode", mode, "--ignore-env", "*", "--log", "ci.log",
                        "--receipt", "receipt.json", "--test", "sh", "ci.sh"]
                env = {"ZZ_LIT_BREAK": "1" if broken else "", "LC_ALL": "C"}
                if os.environ.get("LIT_BIN"):
                    ledger = store / "suite/ledger.jsonl"
                    before = ledger.read_bytes() if ledger.exists() else None
                    pair = lit(root, env, "parity", *argv)
                    self.assertEqual(pair.returncode, 0, pair.stdout + pair.stderr)
                    self.assertIn(f"(exit {code})", pair.stdout)
                    self.assertEqual(ledger.read_bytes() if ledger.exists() else None, before)
                env["LIT_PYTHON"] = str(outer / "unavailable")
                proc = lit(root, env, *argv)
                self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
                return proc, json.loads((root / "receipt.json").read_text())
            _, first = run("shadow")
            self.assertEqual((first["decision"], first["compared"]), ("ran", 0))
            _, shadow = run("shadow")
            self.assertTrue(shadow["predicted_reuse"] and shadow["sound"])
            marker.unlink()
            _, reused = run("enforce")
            self.assertEqual(reused["decision"], "reused")
            self.assertFalse(marker.exists())
            _, audit = run("enforce", rate="1", broken=True, code=9)
            self.assertEqual(audit["decision"], "audited")
            self.assertFalse(audit["sound"])
            self.assertTrue(marker.exists())
            (repo / "input").write_text("modified")
            proc, _ = run("enforce", broken=True, code=9)
            self.assertIn("running the job without a footprint", proc.stderr)
            ledger = [json.loads(s) for s in (store / "suite/ledger.jsonl").read_text().splitlines()]
            self.assertEqual([v["decision"] for v in ledger], ["ran", "ran", "reused", "audited"])

    def test_real_processes_create_compatible_job_and_unit_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            (repo / "src").mkdir()
            (repo / "src/input").write_text("hello\n")
            (repo / "worker").write_text(textwrap.dedent("""\
                set -eu
                cat src/input
                ls src >/dev/null
                test ! -e src/missing
                printf produced > created
                test "$1" != case-b
                """))
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "record inputs")
            def run(*args):
                if os.environ.get("LIT_BIN"):
                    pair = lit(root, {"LC_ALL": "C"}, "parity", *args)
                    self.assertEqual(pair.returncode, 0, pair.stdout + pair.stderr)
                    self.assertIn("(exit 0)", pair.stdout)
                proc = lit(root, {"LC_ALL": "C", "LIT_PYTHON": str(root / "unavailable")}, *args)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                return json.loads(proc.stdout)
            summary = run("trace", "--repo", "repo", "--commit", "HEAD", "--out", "job.json",
                          "--log", "job.log", "--ignore-env", "GITHUB_*", "--test", "sh", "worker", "case-b")
            self.assertEqual(summary["exit_code"], 1)
            job = json.loads((root / "job.json").read_text())
            self.assertEqual(set(job["tracked"]), {"worker", "src/input"})
            self.assertIn("src", job["listings"])
            self.assertIn("src/missing", job["absent"])
            self.assertNotIn("created", job["unknown"])
            self.assertEqual(job["incomplete"], [])
            self.assertEqual((root / "job.log").read_text(), "hello\n")
            self.assertFalse((repo / "created").exists())
            if os.environ.get("LIT_BIN"):
                # The job can write directly to stdout, even without a final newline.
                pair = lit(root, {"LC_ALL": "C"}, "parity", "trace", "--repo", "repo", "--commit", "HEAD",
                           "--out", "job.json", "--test", "sh", "-c", "printf first")
                self.assertEqual(pair.returncode, 0, pair.stdout + pair.stderr)
            (root / "units.txt").write_text("case-a\ncase-b\ncase-a\n")
            for flag, args in [
                ("each", ["--workdir", "repo", "--units-file", "units.txt", "--each", "sh 'worker' \"{}\""]),
                ("pattern", ["--repo", "repo", "--pattern", r"^sh worker (case-[ab])$",
                             "--test", "sh", "-c", "sh worker case-a; sh worker case-b"]),
            ]:
                result = run("trace-units", "--out", "units.json", "--log", flag + ".log", *args)
                self.assertEqual((result["units"], result["failed"]), (2, 1))
                self.assertEqual(result["exit_code"], 1)
                bundle = json.loads((root / "units.json").read_text())
                self.assertEqual(set(bundle["units"]), {"case-a", "case-b"})
                for name, record in bundle["units"].items():
                    self.assertEqual(record["exit_code"], int(name == "case-b"))
                    self.assertIn("src/input", record["tracked"])
                    self.assertEqual(record["incomplete"], [])
                self.assertEqual(bundle["listing_children"]["src"], ["input"])
                self.assertEqual(bundle["in_place"], flag == "each")


if __name__ == "__main__":
    unittest.main()
