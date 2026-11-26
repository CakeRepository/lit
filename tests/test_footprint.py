import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from concurrent_merge.engine import Repo
from concurrent_merge.footprint import parse, predict, trace, verify

JOB = b"""import os, sys
print(open("src/app.py").read())
print(sorted(os.listdir("tests")))
print(os.path.exists("config.local"))
os.makedirs("build", exist_ok=True)
open("build/out.txt", "w").write("generated")
print(open("build/out.txt").read())
sys.exit(1 if os.environ.get("LIT_TEST_BREAK") else 0)
"""


def hexed(text):
    return "".join("\\x%02x" % b for b in text.encode())


def traced():
    # ptrace allows one tracer, so these tests cannot run inside a traced job.
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        return False
    return any(l.split()[1:] != ["0"] for l in status.splitlines() if l.startswith("TracerPid:"))


@unittest.skipUnless(shutil.which("strace") and not traced(), "needs strace and no outer tracer")
class FootprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = Repo(self.root / "repo.git")
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", self.repo.path], check=True
        )
        self.base = self.commit(
            None,
            {
                "src/app.py": b"x = 1\n",
                "src/unused.py": b"y = 1\n",
                "docs/guide.md": b"guide\n",
                "tests/run.py": JOB,
            },
        )
        self.command = [sys.executable, "tests/run.py"]
        with patch.dict(os.environ):
            os.environ.pop("LIT_TEST_BREAK", None)
            self.manifest = trace(self.repo, self.base, self.command, log=os.devnull)

    def commit(self, parent, changes):
        env = {"GIT_INDEX_FILE": str(self.root / "index")}
        if parent:
            self.repo.run("read-tree", parent, env=env)
        else:
            self.repo.run("read-tree", "--empty", env=env)
        for path, data in changes.items():
            blob = self.repo.out("hash-object", "-w", "--stdin", data=data)
            self.repo.run(
                "update-index", "--add", "--cacheinfo", "100644," + blob + "," + path,
                env=env,
            )
        tree = self.repo.out("write-tree", env=env)
        args = ["commit-tree", tree, "-m", "change"]
        if parent:
            args += ["-p", parent]
        return self.repo.out(
            *args,
            env=dict(
                env,
                GIT_AUTHOR_NAME="Test",
                GIT_AUTHOR_EMAIL="test@localhost",
                GIT_COMMITTER_NAME="Test",
                GIT_COMMITTER_EMAIL="test@localhost",
            ),
        )

    def test_records_reads_listings_and_missing_probes_but_not_outputs(self):
        m = self.manifest
        self.assertEqual(m["exit_code"], 0)
        self.assertEqual(m["incomplete"], [])
        self.assertEqual(set(m["tracked"]), {"src/app.py", "tests/run.py"})
        self.assertIn("tests", m["listings"])
        self.assertEqual(m["absent"], ["config.local"])
        self.assertFalse(any(p.startswith("build") for p in m["absent"] + m["unknown"]))
        self.assertEqual(m["untracked"], {})
        self.assertFalse(m["reads_git_metadata"])

    def test_predicts_reuse_only_when_every_consumed_input_matches(self):
        cases = {
            "docs": ({"docs/guide.md": b"changed\n"}, None),
            "unused": ({"src/unused.py": b"y = 2\n"}, None),
            "source": ({"src/app.py": b"x = 2\n"}, "changed"),
            "new test": ({"tests/test_new.py": b""}, "listing"),
            "probe": ({"config.local": b""}, "appeared"),
        }
        for label, (files, reason) in cases.items():
            with self.subTest(label):
                result = predict(self.repo, self.manifest, self.commit(self.base, files))
                if reason is None:
                    self.assertEqual(result["verdict"], "reusable")
                    self.assertEqual(result["expected_exit_code"], 0)
                else:
                    self.assertEqual(result["verdict"], "run")
                    self.assertEqual(result["reasons"][0]["kind"], reason)

    def test_verification_catches_an_unrecorded_environment_input(self):
        candidate = self.commit(self.base, {"docs/guide.md": b"changed\n"})
        with patch.dict(os.environ, {"LIT_TEST_BREAK": "1"}):
            scored, actual = verify(self.repo, self.manifest, candidate)
            self.assertEqual(scored["verdict"], "reusable")
            self.assertEqual(actual["exit_code"], 1)
            self.assertFalse(scored["sound"])
            checked = predict(
                self.repo, self.manifest, candidate, env=dict(os.environ)
            )
        self.assertEqual(checked["verdict"], "run")
        self.assertIn(
            dict(kind="environment", name="LIT_TEST_BREAK"), checked["reasons"]
        )

    def test_a_job_that_reads_its_own_git_metadata_is_revision_specific(self):
        script = b"import subprocess\nsubprocess.run(['git', 'rev-parse', 'HEAD'], check=True)\n"
        base = self.commit(self.base, {"tests/head.py": script})
        manifest = trace(self.repo, base, [sys.executable, "tests/head.py"], log=os.devnull)
        self.assertTrue(manifest["reads_git_metadata"])
        same = predict(self.repo, manifest, base)
        self.assertEqual(same["verdict"], "reusable")
        later = predict(
            self.repo, manifest, self.commit(base, {"docs/guide.md": b"later\n"})
        )
        self.assertEqual(later["reasons"][0]["kind"], "revision")


    def test_looking_for_the_git_root_and_its_excludes_is_not_revision_specific(self):
        script = (b"import os\n"
                  b"assert os.path.isdir('.git')\n"
                  b"p = '.git/info/exclude'\n"
                  b"open(p).read() if os.path.exists(p) else None\n")
        base = self.commit(self.base, {"tests/root.py": script})
        later = self.commit(base, {"docs/guide.md": b"later\n"})
        work = self.root / "work"
        subprocess.run(["git", "clone", "-q", str(self.repo.path), str(work)], check=True)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", base], check=True)
        manifest = trace(self.repo, base, [sys.executable, "tests/root.py"], log=os.devnull,
                         workdir=str(work))
        self.assertFalse(manifest["reads_git_metadata"])
        self.assertIn(".git", manifest["git_static"])
        subprocess.run(["git", "-C", str(work), "checkout", "-q", later], check=True)
        self.assertEqual(predict(self.repo, manifest, later, workdir=str(work))["verdict"], "reusable")
        (work / ".git/info/exclude").write_text("*.log\n")
        changed = predict(self.repo, manifest, later, workdir=str(work))
        self.assertIn(dict(kind="changed", path=".git/info/exclude"), changed["reasons"])
        self.assertEqual(predict(self.repo, manifest, later)["reasons"][0]["kind"], "revision")


class ParserTests(unittest.TestCase):
    def test_joins_interrupted_calls_and_resolves_moves_and_relative_paths(self):
        cwd = hexed("/work")
        log = "\n".join(
            [
                f'10 openat(AT_FDCWD<{cwd}>, "{hexed("a.txt")}", O_RDONLY <unfinished ...>',
                "11 clone(child_stack=NULL) = 12",
                '10 <... openat resumed>) = 3<' + hexed("/work/a.txt") + ">",
                f'10 chdir("{hexed("sub")}") = 0',
                f'10 stat("{hexed("b.txt")}", 0x1) = -1 ENOENT (No such file or directory)',
                f'10 rename("{hexed("c.txt")}", "{hexed("d.txt")}") = 0',
                f'10 symlinkat("{hexed("target")}", AT_FDCWD<{hexed("/work/sub")}>, "{hexed("link")}") = 0',
                f'10 readlinkat(AT_FDCWD<{hexed("/work/sub")}>, "{hexed("e")}", "{hexed("ignored")}", 4096) = 7',
            ]
        ).encode()
        events = parse(log)
        self.assertEqual(events.first["/work/a.txt"], "read")
        self.assertIn("/work/sub/b.txt", events.missing)
        self.assertEqual(events.first["/work/sub/c.txt"], "read")
        self.assertEqual(events.first["/work/sub/d.txt"], "write")
        self.assertEqual(events.first["/work/sub/link"], "write")
        self.assertNotIn("/work/sub/target", events.first)
        self.assertNotIn("/work/sub/ignored", events.first)
        self.assertEqual(events.first["/work/sub/e"], "read")
        self.assertEqual(events.unresolved, [])


if __name__ == "__main__":
    unittest.main()


class HostTests(unittest.TestCase):
    def test_same_tick_tool_rewrite_changes_the_digest(self):
        from concurrent_merge.footprint import file_digest
        with tempfile.TemporaryDirectory() as root:
            tool = Path(root, "tool")
            tool.write_bytes(b"v1")
            same_tick = tool.stat()
            with patch("os.fstat", return_value=same_tick), patch(
                "time.time_ns", return_value=same_tick.st_ctime_ns
            ):
                first = file_digest(tool)
                tool.write_bytes(b"v2")
                self.assertNotEqual(file_digest(tool), first)

    def manifest(self, external):
        return dict(incomplete=[], network=False, reads_git_metadata=False, commit="c",
                    tracked={}, directories=[], listings={}, absent=[], untracked={},
                    external=external)

    def test_machine_local_files_never_count_as_changed_host_inputs(self):
        from concurrent_merge.footprint import compare

        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("real input\n")
        self.addCleanup(os.unlink, f.name)
        recorded = {"/etc/hosts": "stale", "/etc/resolv.conf": "stale",
                    "/var/cache/fontconfig/x-le64.cache-12": "stale", f.name: "stale"}
        reasons, checked = compare(self.manifest(recorded), "c", {}, host=True)
        self.assertIn("host", checked)
        self.assertEqual(reasons, [dict(kind="host", path=f.name)])
