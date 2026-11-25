import unittest

from reader_conformance import ReaderConformance

BASE = b'''"""Module."""
import os
from pathlib import Path

LIMIT = 3


@cache
def load(name):
    return Path(name).read_text()


class Store:
    def get(self, key):
        return key

    def put(self, key, value):
        self.key = value
'''


class PythonReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "pkg/store.py"
    SAMPLES = (BASE, b"x = 1\n\ndef f():\n    pass\n")
    KEYS = ((0, ["import :os", "import pathlib:Path", "def load", "class Store"]),)
    BROKEN = (b"def f(:\n", b"class A:\n    def g(self):\n  return\n", b's = "open\n')
    MERGES = (
        (BASE, BASE.replace(b"return key", b"return key * 2"), BASE.replace(b"self.key = value", b"self.key = value or 0"),
         BASE.replace(b"return key", b"return key * 2").replace(b"self.key = value", b"self.key = value or 0")),
        (BASE, BASE.replace(b"return key", b"return 1"), BASE.replace(b"return key", b"return 2"), None),
        (BASE, BASE + b"\n\ndef a():\n    pass\n", BASE + b"\n\ndef b():\n    pass\n",
         BASE + b"\n\ndef a():\n    pass\n\n\ndef b():\n    pass\n"),
    )


if __name__ == "__main__":
    unittest.main()
