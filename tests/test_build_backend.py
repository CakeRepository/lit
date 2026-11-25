import base64
import csv
import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "build_backend"))
import lit_build  # noqa: E402

WHEEL = "concurrent_merge_engine-0.1.0-py3-none-any.whl"
INFO = "concurrent_merge_engine-0.1.0.dist-info"


class BuildBackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        old = os.getcwd()
        os.chdir(REPO)
        self.addCleanup(os.chdir, old)

    def test_no_build_requirements(self):
        self.assertEqual(lit_build.get_requires_for_build_wheel(), [])
        self.assertEqual(lit_build.get_requires_for_build_sdist(), [])
        self.assertEqual(lit_build.get_requires_for_build_editable(), [])

    def test_wheel(self):
        name = lit_build.build_wheel(str(self.out))
        self.assertEqual(name, WHEEL)
        with zipfile.ZipFile(self.out / name) as zf:
            names = set(zf.namelist())
            for module in REPO.joinpath("concurrent_merge").glob("*.py"):
                self.assertIn(f"concurrent_merge/{module.name}", names)
            for extra in (
                "ps_nodes.ps1",
                "dotnet/Program.cs",
                "dotnet/LitIlMap.csproj",
            ):
                self.assertIn(f"concurrent_merge/{extra}", names)
            for meta in ("METADATA", "WHEEL", "RECORD", "entry_points.txt"):
                self.assertIn(f"{INFO}/{meta}", names)
            self.assertIn(f"{INFO}/licenses/LICENSE", names)
            self.assertIn(f"{INFO}/licenses/NOTICE", names)
            self.assertFalse([n for n in names if "__pycache__" in n or n.startswith("tests/")])

            metadata = zf.read(f"{INFO}/METADATA").decode()
            head, _, body = metadata.partition("\n\n")
            self.assertIn("Metadata-Version: 2.4", head)
            self.assertIn("Name: concurrent-merge-engine", head)
            self.assertIn("Version: 0.1.0", head)
            self.assertIn("Summary: Batch reviewed GitHub changes", head)
            self.assertIn("Requires-Python: >=3.12", head)
            self.assertIn("License-File: LICENSE", head)
            self.assertIn("Description-Content-Type: text/markdown", head)
            self.assertEqual(body, (REPO / "README.md").read_text(encoding="utf-8"))

            wheel = zf.read(f"{INFO}/WHEEL").decode()
            for line in (
                "Wheel-Version: 1.0",
                "Generator: lit_build",
                "Root-Is-Purelib: true",
                "Tag: py3-none-any",
            ):
                self.assertIn(line, wheel.splitlines())
            self.assertIn(
                "concurrent-merge = concurrent_merge.cli:main",
                zf.read(f"{INFO}/entry_points.txt").decode(),
            )

            rows = list(csv.reader(io.StringIO(zf.read(f"{INFO}/RECORD").decode())))
            self.assertEqual({r[0] for r in rows}, names)
            for path, digest, size in rows:
                if path == f"{INFO}/RECORD":
                    self.assertEqual((digest, size), ("", ""))
                    continue
                data = zf.read(path)
                expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                self.assertEqual(digest, "sha256=" + expected.decode())
                self.assertEqual(int(size), len(data))

    def test_sdist(self):
        name = lit_build.build_sdist(str(self.out))
        self.assertEqual(name, "concurrent_merge_engine-0.1.0.tar.gz")
        with tarfile.open(self.out / name) as tar:
            names = tar.getnames()
        top = "concurrent_merge_engine-0.1.0"
        self.assertTrue(all(n == top or n.startswith(top + "/") for n in names))
        rel = {n[len(top) + 1 :] for n in names if n != top}
        for expected in (
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "NOTICE",
            "PKG-INFO",
            "build_backend/lit_build.py",
            "concurrent_merge/cli.py",
            "concurrent_merge/js_symbols.py",
            "tests/test_build_backend.py",
        ):
            self.assertIn(expected, rel)
        self.assertFalse([n for n in rel if "__pycache__" in n])

    def test_editable(self):
        name = lit_build.build_editable(str(self.out))
        with zipfile.ZipFile(self.out / name) as zf:
            pth = zf.read("__editable__.concurrent_merge_engine.pth").decode().strip()
            self.assertEqual(Path(pth), REPO)
            self.assertNotIn("concurrent_merge/cli.py", zf.namelist())
            self.assertIn(f"{INFO}/RECORD", zf.namelist())

    def test_prepare_metadata(self):
        info = lit_build.prepare_metadata_for_build_wheel(str(self.out))
        self.assertEqual(info, INFO)
        self.assertTrue((self.out / INFO / "METADATA").is_file())

    def test_deterministic(self):
        a, b = self.out / "a", self.out / "b"
        a.mkdir()
        b.mkdir()
        lit_build.build_wheel(str(a))
        lit_build.build_wheel(str(b))
        self.assertEqual((a / WHEEL).read_bytes(), (b / WHEEL).read_bytes())


if __name__ == "__main__":
    unittest.main()
