import unittest

from concurrent_merge import readers
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
    SAMPLES = (BASE, b"x = 1\n\ndef f():\n    pass\n",
               b'def render[T](items: list[T]):\n    return f"{",".join(items)}"\n',
               b'def f(x, x): pass\n',  # ast.parse outlines before compilation's semantic checks.
               b'from ..library import item as renamed\nimport sys; import os\n')
    KEYS = ((0, ["import :os", "import pathlib:Path", "def load", "class Store"]),)
    BROKEN = (b"def f(:\n", b"class A:\n    def g(self):\n  return\n", b's = "open\n',
              b'x=' + b'(' * 250 + b'1' + b')' * 250, b'# coding: no-such-codec\nx=1\n')
    MERGES = (
        (BASE, BASE.replace(b"return key", b"return key * 2"), BASE.replace(b"self.key = value", b"self.key = value or 0"),
         BASE.replace(b"return key", b"return key * 2").replace(b"self.key = value", b"self.key = value or 0")),
        (BASE, BASE.replace(b"return key", b"return 1"), BASE.replace(b"return key", b"return 2"), None),
        (BASE, BASE + b"\n\ndef a():\n    pass\n", BASE + b"\n\ndef b():\n    pass\n",
         BASE + b"\n\ndef a():\n    pass\n\n\ndef b():\n    pass\n"),
    )

    def test_source_encoding_and_nested_declarations_preserve_original_bytes(self):
        src = "# coding: latin-1\r\n@wrap\r\nasync def café():\r\n    if ok:\r\n        class Nested: pass\r\n"
        blob = src.encode("latin-1")
        ((key, start, end, children),) = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual((key, start, end), ("async def café", blob.index(b"@wrap"), len(blob)))
        self.assertEqual(children, [("class Nested", blob.index(b"        class"), len(blob), [])])


if __name__ == "__main__":
    unittest.main()
