"""The .NET program map: a class's key changes exactly when code it can reach changes.

Builds two tiny projects with the .NET SDK (skipped where there is none). The "tests" project declares its own
FactAttribute, which is all the map looks for, so no package restore is needed."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from concurrent_merge import dotnet_units

LIB = """
namespace Lib;
public static class Math2 { public static int Add(int a, int b) => a + b; public static int Mul(int a, int b) => a * b; }
public abstract class Shape { public abstract int Area(); }
public sealed class Square : Shape { public int Side = 2; public override int Area() => Side * Side; }
public sealed class Unused { public int Ping() => 1; }
public sealed class Config { public Inner Inner { get; set; } = new(); }
public sealed class Inner { public int Value { get => 3; set { } } }
public static class Lookup { public static object? Make(string name) => System.Type.GetType(name); }
"""

TESTS = """
namespace Tests;
[System.AttributeUsage(System.AttributeTargets.Method)] public sealed class FactAttribute : System.Attribute { }
public class AddTests { [Fact] public void Adds() { if (Lib.Math2.Add(1, 2) != 3) throw new System.Exception(); } }
public class ShapeTests { [Fact] public void Area() { Lib.Shape s = new Lib.Square(); if (s.Area() != 4) throw new System.Exception(); } }
public class ConfigTests { [Fact] public void Named() { var t = typeof(Lib.Config); if (t == null) throw new System.Exception(); } }
public class LookupTests { [Fact] public void Looks() { Lib.Lookup.Make("Lib.Unused"); } }
public class DataTests { [Fact] public void Reads() { System.IO.File.ReadAllText("fixture.txt"); } }
"""

PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net{major}.0</TargetFramework><Nullable>enable</Nullable><Deterministic>true</Deterministic></PropertyGroup>
  {extra}
</Project>"""


def sdk_major():
    if not shutil.which("dotnet"):
        return None
    try:
        out = subprocess.run(["dotnet", "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
        return int(out.split(".")[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


MAJOR = sdk_major()


@unittest.skipUnless(MAJOR and MAJOR >= 10, "needs the .NET 10 SDK")
class ProgramMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="lit-dotnet-test-"))
        cls.store = cls.root / "store"
        cls.tool = dotnet_units.tool(cls.store, None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, lib_source):
        work = Path(tempfile.mkdtemp(dir=self.root))
        (work / "Lib").mkdir()
        (work / "Tests").mkdir()
        (work / "Lib" / "Lib.cs").write_text(lib_source)
        (work / "Lib" / "Lib.csproj").write_text(PROJECT.format(major=MAJOR, extra=""))
        (work / "Tests" / "Tests.cs").write_text(TESTS)
        (work / "Tests" / "Tests.csproj").write_text(PROJECT.format(
            major=MAJOR, extra='<ItemGroup><ProjectReference Include="../Lib/Lib.csproj" /></ItemGroup>'))
        done = subprocess.run(["dotnet", "build", str(work / "Tests"), "-v", "q", "-nologo", "-o", str(work / "out")],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout[-3000:])
        out = work / "out"
        own, _ = dotnet_units.own_assemblies(out, out / "Tests.dll")
        self.assertEqual(sorted(Path(p).name for p in own), ["Lib.dll", "Tests.dll"])
        return dotnet_units.run_map(self.tool, out / "Tests.dll", own, ["fixture.txt"], work / "map.json")["units"]

    def keys(self, units):
        return {name: unit["key"] for name, unit in units.items()}

    def test_a_change_reruns_only_the_classes_that_reach_it(self):
        base = self.build(LIB)
        self.assertEqual(sorted(base), ["Tests.AddTests", "Tests.ConfigTests", "Tests.DataTests",
                                        "Tests.LookupTests", "Tests.ShapeTests"])
        changed = self.build(LIB.replace("a * b", "a * b + 1"))  # Mul: nobody calls it
        # Only the class that looks types up by name (and so depends on the whole program) sees it.
        self.assertEqual({n for n in base if base[n]["key"] != changed[n]["key"]}, {"Tests.LookupTests"})
        added = self.build(LIB.replace("=> a + b;", "=> b + a;"))
        moved = {n for n in base if base[n]["key"] != added[n]["key"]}
        self.assertIn("Tests.AddTests", moved)
        self.assertNotIn("Tests.ShapeTests", moved)
        self.assertNotIn("Tests.DataTests", moved)

    def test_a_virtual_override_of_a_created_type_counts(self):
        base = self.build(LIB)
        changed = self.build(LIB.replace("=> Side * Side;", "=> Side + Side;"))
        self.assertNotEqual(base["Tests.ShapeTests"]["key"], changed["Tests.ShapeTests"]["key"])
        self.assertEqual(base["Tests.AddTests"]["key"], changed["Tests.AddTests"]["key"])

    def test_a_type_named_as_data_counts_whole_with_its_member_types(self):
        base = self.build(LIB)
        changed = self.build(LIB.replace("get => 3;", "get => 4;"))  # Inner.Value, reached only by a serializer
        self.assertNotEqual(base["Tests.ConfigTests"]["key"], changed["Tests.ConfigTests"]["key"])

    def test_a_string_type_lookup_depends_on_the_whole_program(self):
        base = self.build(LIB)
        self.assertTrue(base["Tests.LookupTests"]["dynamic"])
        changed = self.build(LIB.replace("Ping() => 1", "Ping() => 2"))
        self.assertNotEqual(base["Tests.LookupTests"]["key"], changed["Tests.LookupTests"]["key"])
        self.assertEqual(base["Tests.AddTests"]["key"], changed["Tests.AddTests"]["key"])

    def test_a_data_file_belongs_to_the_classes_that_name_it(self):
        base = self.build(LIB)
        self.assertEqual(base["Tests.DataTests"]["files"], ["fixture.txt"])
        self.assertEqual(base["Tests.AddTests"]["files"], [])


# Stands in for `dotnet test`: each class it is asked to run reads the files that class's tests read.
FAKE_RUNNER = r"""
import os, sys
reads = {"Tests.DataTests": ["data/fixture.txt"], "Tests.AddTests": [], "Tests.ShapeTests": ["data"],
         "Tests.ConfigTests": [], "Tests.LookupTests": []}
names = [part.split("~")[1].rstrip(".") for part in sys.argv[sys.argv.index("--filter") + 1].split("|")] \
    if "--filter" in sys.argv else list(reads)
for name in names:
    for rel in reads[name]:
        if os.path.isdir(rel):
            os.listdir(rel)
        else:
            open(rel).read()
    if name == "Tests.ConfigTests":
        os.path.exists("data/optional.txt")
fail = os.environ.get("FAKE_FAIL")
sys.exit(1 if fail and fail in names else 0)
"""


@unittest.skipUnless(MAJOR and MAJOR >= 10 and shutil.which("strace"), "needs the .NET 10 SDK and strace")
class Records(unittest.TestCase):
    """refresh and sweep record each class alone; enforce then reruns exactly the classes whose inputs moved."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="lit-dotnet-record-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        work = self.root / "repo"
        for d in ("Lib", "Tests", "data"):
            (work / d).mkdir(parents=True)
        (work / "Lib" / "Lib.cs").write_text(LIB)
        (work / "Lib" / "Lib.csproj").write_text(PROJECT.format(major=MAJOR, extra=""))
        (work / "Tests" / "Tests.cs").write_text(TESTS)
        (work / "Tests" / "Tests.csproj").write_text(PROJECT.format(
            major=MAJOR, extra='<ItemGroup><ProjectReference Include="../Lib/Lib.csproj" /></ItemGroup>'))
        (work / "data" / "fixture.txt").write_text("one")
        (work / "data" / "other.txt").write_text("x")
        (work / "runner.py").write_text(FAKE_RUNNER)
        (work / ".gitignore").write_text("out/\nbin/\nobj/\n")
        self.work = work
        self.git("init", "-q")
        self.commit()
        self.store = self.root / "store"

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.work), *args], check=True, capture_output=True)

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c", "--allow-empty")
        done = subprocess.run(["dotnet", "build", str(self.work / "Tests"), "-v", "q", "-nologo",
                               "-o", str(self.work / "out")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout[-3000:])

    def run_units(self, mode):
        return dotnet_units.run_dotnet_units(
            self.store, "t", self.work, self.work / "out" / "Tests.dll",
            ["python3", "runner.py", "{filter}"], mode=mode, jobs=2)

    def test_each_class_keeps_only_what_it_read(self):
        swept = self.run_units("sweep")
        self.assertEqual((swept["ran"], swept["recorded"], swept["exit_code"]), (5, 5, 0))
        record = json.loads((self.store / "t" / "dotnet-record.json").read_text())
        files = {n: c["files"] for n, c in record["classes"].items()}
        self.assertIn("repo:data/fixture.txt", files["Tests.DataTests"])
        self.assertNotIn("repo:data/fixture.txt", files["Tests.AddTests"])
        self.assertIn("dir:data", files["Tests.ShapeTests"])
        self.assertEqual(files["Tests.ConfigTests"].get("absent:data/optional.txt")
                         or files["Tests.ConfigTests"].get("stat:data/optional.txt"), "missing")

        self.assertEqual(self.run_units("enforce")["ran"], 0)

        (self.work / "data" / "fixture.txt").write_text("two")
        self.commit()
        self.assertEqual(self.run_units("enforce")["run"], ["Tests.DataTests"])

        (self.work / "data" / "new.txt").write_text("n")   # a new entry in a listed directory
        (self.work / "data" / "optional.txt").write_text("o")   # a path one class found missing
        self.commit()
        self.assertEqual(set(self.run_units("enforce")["run"]),
                         {"Tests.DataTests", "Tests.ShapeTests", "Tests.ConfigTests"})

    def test_a_failing_class_is_not_recorded_and_the_rest_are(self):
        os.environ["FAKE_FAIL"] = "Tests.AddTests"
        self.addCleanup(os.environ.pop, "FAKE_FAIL", None)
        swept = self.run_units("sweep")
        self.assertEqual((swept["exit_code"], swept["failed"], swept["recorded"]), (1, ["Tests.AddTests"], 4))
        os.environ.pop("FAKE_FAIL")
        self.assertEqual(self.run_units("enforce")["run"], ["Tests.AddTests"])
        refreshed = self.run_units("refresh")
        self.assertEqual((refreshed["run"], refreshed["recorded"]), (["Tests.AddTests"], 1))
        self.assertEqual(self.run_units("enforce")["ran"], 0)


class Inputs(unittest.TestCase):
    def test_filter_names_whole_classes(self):
        self.assertEqual(dotnet_units.class_filter(["N.B", "N.A"]),
                         "FullyQualifiedName~N.A.|FullyQualifiedName~N.B.")

    def test_traced_inputs_keep_repository_and_content_files_outside_build_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "scripts").mkdir()
            (root / "scripts" / "a.sh").write_text("a")
            out = root / "agent" / "bin" / "Debug"
            out.mkdir(parents=True)
            (out / "settings.json").write_text("{}")
            log = root / "t.log"
            log.write_text(
                f'12 openat(AT_FDCWD, "{root}/scripts/a.sh", O_RDONLY|O_CLOEXEC) = 3\n'
                f'12 openat(AT_FDCWD, "{root}/scripts", O_RDONLY|O_DIRECTORY) = 4\n'
                f'12 openat(AT_FDCWD, "{root}/agent/bin/x.dll", O_RDONLY) = 4\n'
                f'12 openat(AT_FDCWD, "settings.json", O_RDONLY) = 5\n'
                f'12 openat(AT_FDCWD, "{root}/scripts/gone.sh", O_RDONLY) = -1 ENOENT (No such file or directory)\n'
                f'12 newfstatat(AT_FDCWD, "{root}/scripts/a.sh", {{st_mode=S_IFREG|0644, st_size=1, ...}}, 0) = 0\n'
                f'13 openat(AT_FDCWD, "/usr/lib/x", O_RDONLY) = 5\n')
            inputs = dotnet_units.traced_inputs(log, root, out, {"scripts/a.sh", "agent/bin/x.dll"},
                                                {"settings.json": "s1"})
            self.assertEqual(set(inputs), {"repo:scripts/a.sh", "dir:scripts", "out:settings.json",
                                           "absent:scripts/gone.sh"})
            self.assertEqual(inputs["repo:scripts/a.sh"], dotnet_units.file_sha(root / "scripts" / "a.sh"))
            self.assertEqual(inputs["out:settings.json"], "s1")
            self.assertEqual(inputs["absent:scripts/gone.sh"], "missing")


if __name__ == "__main__":
    unittest.main()
