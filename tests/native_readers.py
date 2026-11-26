"""Run the existing language suites with the native readers, and compare byte extents."""
import importlib
import json
import os
from pathlib import Path
import pkgutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from concurrent_merge import readers
from reader_conformance import ReaderConformance


def suite(binary):
    suffixes = subprocess.check_output([binary, "--internal-outline", "--list"], text=True).split()
    readers.native_program = binary
    readers.native_suffixes = suffixes
    tests = unittest.TestSuite()
    exercised = set()
    for module in pkgutil.iter_modules([str(Path(__file__).resolve().parent)]):
        if not module.name.startswith("test_reader_"):
            continue
        for cls in vars(importlib.import_module(module.name)).values():
            if not isinstance(cls, type) or not issubclass(cls, ReaderConformance) or not cls.PATH:
                continue
            suffix = Path(cls.PATH).suffix.lower()
            if suffix not in suffixes:
                continue
            language = readers.reader_for(cls.PATH)
            for ext in language.SUFFIXES:
                assert ext in suffixes, ext
                exercised.add(ext)
                samples = [*cls.SAMPLES, *cls.BROKEN]
                for base, ours, theirs, expected in cls.MERGES:
                    samples.extend((base, ours, theirs))
                    if expected is not None:
                        samples.append(expected)
                for blob in set(samples):
                    actual = subprocess.check_output([binary, "--internal-outline", ext], input=blob)
                    expected = None if b"\x00" in blob else language.outline(blob, ext)
                    assert json.loads(actual) == json.loads(json.dumps(expected)), (ext, blob[:100])
            tests.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    if not suffixes or exercised != set(suffixes):
        raise ValueError(f"Missing native conformance suite for {set(suffixes) - exercised}")
    return tests


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(suite(os.environ["LIT_BIN"]))
    sys.exit(not result.wasSuccessful())
