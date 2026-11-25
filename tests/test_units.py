import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from concurrent_merge.engine import Repo
from concurrent_merge.footprint import parse, select, split_units, trace_units

RUNNER = b"""import os, subprocess, sys
code = 0
for name in sorted(os.listdir("tests")):
    if name.startswith("test_"):
        code |= subprocess.run([sys.executable, "tests/" + name]).returncode
sys.exit(code)
"""


def hexed(text):
    return "".join("\\x%02x" % b for b in text.encode())


def traced():
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:
        return False
    return any(l.split()[1:] != ["0"] for l in status.splitlines() if l.startswith("TracerPid:"))


class SplitTests(unittest.TestCase):
    def test_attributes_reads_to_the_unit_whose_exec_started_them(self):
        def exe(*argv):
            args = ", ".join(f'"{hexed(a)}"' for a in argv)
            return f'execve("{hexed(argv[0])}", [{args}], 0x1 /* 1 vars */) = 0'

        def opened(path):
            return f'openat(AT_FDCWD<{hexed("/w")}>, "{hexed(path)}", O_RDONLY) = 3<{hexed("/w/" + path)}>'

        log = "\n".join(
            [
                f"1 100.0 {exe('/bin/runner')}",
                f"1 100.1 {opened('shared.txt')}",
                "1 100.2 clone(child_stack=NULL) = 2",
                f"2 100.3 {exe('/bin/t', 'tests/test_a.py')}",
                "2 100.4 clone(child_stack=NULL) = 3",
                f"3 100.5 {opened('a.txt')}",
                "3 100.6 +++ exited with 0 +++",
                "2 101.5 +++ exited with 0 +++",
                "1 101.6 clone(child_stack=NULL) = 4",
                f"4 101.7 {exe('/bin/t', 'tests/test_b.py')}",
                f"4 101.8 {opened('b.txt')}",
                "4 102.0 +++ exited with 2 +++",
                "1 102.1 +++ exited with 2 +++",
            ]
        ).encode()
        units = split_units(parse(log, record=True), r"(tests/test_\w+\.py)")
        self.assertEqual(set(units), {"tests/test_a.py", "tests/test_b.py"})
        a, b = units["tests/test_a.py"], units["tests/test_b.py"]
        self.assertIn("/w/a.txt", a.first)
        self.assertNotIn("/w/shared.txt", a.first)
        self.assertNotIn("/w/b.txt", a.first)
        self.assertEqual((a.exit_code, a.seconds), (0, 1.2))
        self.assertEqual((b.exit_code, b.seconds), (2, 0.3))


@unittest.skipUnless(shutil.which("strace") and not traced(), "needs strace and no outer tracer")
class UnitFootprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = Repo(self.root / "repo.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.repo.path], check=True)
        self.base = self.commit(
            None,
            {
                "run_all.py": RUNNER,
                "src/a.py": b"A = 1\n",
                "src/b.py": b"B = 1\n",
                "tests/test_a.py": b"import sys\nsys.exit(0 if 'A = 1' in open('src/a.py').read() else 1)\n",
                "tests/test_b.py": b"print(open('src/b.py').read())\n",
            },
        )
        self.bundle = trace_units(
            self.repo, self.base, [sys.executable, "run_all.py"],
            r"(tests/test_\w+\.py)", log=os.devnull,
        )

    def commit(self, parent, changes):
        env = {"GIT_INDEX_FILE": str(self.root / "index")}
        self.repo.run("read-tree", parent or "--empty", env=env)
        for path, data in changes.items():
            blob = self.repo.out("hash-object", "-w", "--stdin", data=data)
            self.repo.run(
                "update-index", "--add", "--cacheinfo", "100644," + blob + "," + path, env=env
            )
        tree = self.repo.out("write-tree", env=env)
        args = ["commit-tree", tree, "-m", "change"] + (["-p", parent] if parent else [])
        ident = dict(
            GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@localhost",
            GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@localhost",
        )
        return self.repo.out(*args, env=dict(env, **ident))

    def subtree(self, commit):
        # The tests directory of commit without tests/test_b.py.
        kept = "".join(
            line + "\n" for line in self.repo.out("ls-tree", commit + ":tests").splitlines()
            if not line.endswith("\ttest_b.py")
        )
        return "040000 tree " + self.repo.out("mktree", data=kept.encode()) + "\ttests\n"

    def test_each_unit_records_only_its_own_inputs(self):
        units = self.bundle["units"]
        self.assertEqual(set(units), {"tests/test_a.py", "tests/test_b.py"})
        self.assertIn("src/a.py", units["tests/test_a.py"]["tracked"])
        self.assertNotIn("src/b.py", units["tests/test_a.py"]["tracked"])
        self.assertNotIn("run_all.py", units["tests/test_a.py"]["tracked"])
        self.assertEqual(units["tests/test_a.py"]["exit_code"], 0)
        self.assertIsNotNone(units["tests/test_a.py"]["seconds"])
        self.assertEqual(units["tests/test_a.py"]["incomplete"], [])

    def test_each_mode_runs_every_unit_under_its_own_trace(self):
        each = {
            name: [sys.executable, name] for name in ("tests/test_a.py", "tests/test_b.py")
        }
        bundle = trace_units(self.repo, self.base, each=each, log=os.devnull)
        for name, unit in bundle["units"].items():
            self.assertEqual(unit["tracked"], self.bundle["units"][name]["tracked"])
            self.assertEqual(unit["exit_code"], 0)
        later = self.commit(self.base, {"src/a.py": b"A = 1\n# edit\n"})
        chosen = select(self.repo, bundle, later)
        self.assertEqual((chosen["run"], chosen["reusable"]), (["tests/test_a.py"], ["tests/test_b.py"]))

    def test_names_mode_ignores_unrelated_files_added_to_a_listed_directory(self):
        # The runner lists tests/, but each unit lists src/ only if it enumerates it.
        lister = b"import os\nprint(sorted(os.listdir('src')))\nopen('src/a.py').read()\n"
        base = self.commit(self.base, {"tests/test_list.py": lister})
        bundle = trace_units(
            self.repo, base, each={"tests/test_list.py": [sys.executable, "tests/test_list.py"]},
            log=os.devnull,
        )
        unit = bundle["units"]["tests/test_list.py"]
        self.assertIn("src", unit["listings"])
        self.assertEqual(unit["listing_lookups"]["src"], ["a"])
        unrelated = self.commit(base, {"src/c.py": b""})
        shadow = self.commit(base, {"src/a.pyc": b""})
        for candidate, names in ((unrelated, []), (shadow, ["tests/test_list.py"])):
            exact = select(self.repo, bundle, candidate)
            self.assertEqual(exact["run"], ["tests/test_list.py"])
            chosen = select(self.repo, bundle, candidate, listings="names")
            self.assertEqual(chosen["run"], names)

    def test_names_mode_matches_module_files_by_the_import_that_could_resolve_them(self):
        # Like a bundler's resolver: list the directory, then open the module an import named.
        reader = b"import os\nos.listdir('web')\nopen('web/x.ts').read()\n"
        base = self.commit(self.base, {"tests/test_import.py": reader, "web/x.ts": b"", "web/y.ts": b""})
        bundle = trace_units(
            self.repo, base, each={"tests/test_import.py": [sys.executable, "tests/test_import.py"]},
            log=os.devnull,
        )
        self.assertIn("x", bundle["units"]["tests/test_import.py"]["listing_keys"]["web"])
        cases = (
            ({"web/x.postgres.test.ts": b""}, []),  # a new test file next to x.ts
            ({"web/z.ts": b""}, []),
            ({"web/x.tsx": b""}, ["tests/test_import.py"]),  # can shadow x.ts for "./x"
            ({"web/x/index.ts": b""}, ["tests/test_import.py"]),  # a directory x can too
        )
        for files, expected in cases:
            chosen = select(self.repo, bundle, self.commit(base, files), listings="names")
            self.assertEqual(chosen["run"], expected, files)

    def test_names_mode_ignores_tool_outputs_that_share_a_config_stem(self):
        # tsc -b writes tsconfig.server.tsbuildinfo next to the tsconfig.json a test runner read.
        reader = b"import os\nos.listdir('web')\nopen('web/tsconfig.json').read()\n"
        base = self.commit(self.base, {"tests/test_cfg.py": reader, "web/tsconfig.json": b"{}"})
        bundle = trace_units(
            self.repo, base, each={"tests/test_cfg.py": [sys.executable, "tests/test_cfg.py"]},
            log=os.devnull,
        )
        cases = (
            ({"web/tsconfig.server.tsbuildinfo": b""}, []),
            ({"web/tsconfig.ts": b""}, ["tests/test_cfg.py"]),
            ({"web/package.json": b"{}"}, ["tests/test_cfg.py"]),  # a resolver looks for it everywhere
        )
        for older in (False, True):
            if older:
                del bundle["units"]["tests/test_cfg.py"]["listing_keys"]
            for files, expected in cases:
                chosen = select(self.repo, bundle, self.commit(base, files), listings="names")
                self.assertEqual(chosen["run"], expected, (older, files))

    def test_names_mode_reads_older_records_by_stem(self):
        reader = b"import os\nos.listdir('web')\nopen('web/x.ts').read()\n"
        base = self.commit(self.base, {"tests/test_import.py": reader, "web/x.ts": b""})
        bundle = trace_units(
            self.repo, base, each={"tests/test_import.py": [sys.executable, "tests/test_import.py"]},
            log=os.devnull,
        )
        del bundle["units"]["tests/test_import.py"]["listing_keys"]
        chosen = select(self.repo, bundle, self.commit(base, {"web/x.postgres.test.ts": b""}), listings="names")
        self.assertEqual(chosen["run"], ["tests/test_import.py"])

    def test_names_mode_keeps_listings_of_enumerated_directories(self):
        reader = b"import os\nfor n in sorted(os.listdir('lib')):\n    open('lib/' + n).read()\n"
        files = {f"lib/m{i}.py": b"" for i in range(6)}
        base = self.commit(self.base, dict(files, **{"tests/test_scan.py": reader}))
        bundle = trace_units(
            self.repo, base, each={"tests/test_scan.py": [sys.executable, "tests/test_scan.py"]},
            log=os.devnull,
        )
        self.assertEqual(bundle["units"]["tests/test_scan.py"]["enumerated"], ["lib"])
        added = self.commit(base, {"lib/zz.py": b""})
        chosen = select(self.repo, bundle, added, listings="names")
        self.assertEqual(chosen["run"], ["tests/test_scan.py"])

    def test_selects_only_units_whose_inputs_changed_plus_new_ones(self):
        later = self.commit(
            self.base, {"src/b.py": b"B = 2\n", "tests/test_c.py": b"", "run_all.py": RUNNER + b"\n"}
        )
        chosen = select(self.repo, self.bundle, later, discover=["tests/test_*.py"])
        self.assertEqual(chosen["run"], ["tests/test_b.py", "tests/test_c.py"])
        self.assertEqual(chosen["reusable"], ["tests/test_a.py"])
        self.assertEqual(chosen["reasons"]["tests/test_c.py"][0]["kind"], "new")
        self.assertEqual(chosen["reasons"]["tests/test_b.py"][0]["kind"], "changed")

    def test_failed_units_always_run_and_deleted_ones_drop(self):
        broken = self.commit(self.base, {"src/a.py": b"A = 2\n"})
        bundle = trace_units(
            self.repo, broken, [sys.executable, "run_all.py"],
            r"(tests/test_\w+\.py)", log=os.devnull,
        )
        self.assertEqual(bundle["units"]["tests/test_a.py"]["exit_code"], 1)
        self.assertEqual(bundle["exit_code"], 1)
        chosen = select(self.repo, bundle, broken)
        self.assertEqual(chosen["run"], ["tests/test_a.py"])
        self.assertEqual(chosen["reasons"]["tests/test_a.py"][0]["kind"], "failed")
        tree = self.repo.out(
            "mktree", data="".join(
                line + "\n" for line in self.repo.out("ls-tree", broken).splitlines()
                if not line.endswith("\ttests")
            ).encode() + self.subtree(broken).encode()
        )
        gone = self.repo.out(
            "commit-tree", tree, "-p", broken, "-m", "rm",
            env=dict(GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@localhost",
                     GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@localhost"),
        )
        chosen = select(self.repo, bundle, gone, discover=["tests/test_*.py"])
        self.assertEqual(chosen["reusable"], [])
        self.assertEqual(chosen["run"], ["tests/test_a.py"])


if __name__ == "__main__":
    unittest.main()
