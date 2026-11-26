import unittest

from concurrent_merge import readers
from reader_conformance import ReaderConformance

PACKAGE = b'''{
  "name": "@acme/widgets",
  "version": "1.4.2",
  "scripts": {
    "build": "tsc -p .",
    "test": "jest --coverage"
  },
  "dependencies": {
    "express": "^4.18.2",
    "zod": "^3.22.0"
  },
  "files": ["dist", "README.md"]
}
'''

TSCONFIG = b'''{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "strict": true,
    "paths": { "@/*": ["src/*"] }
  },
  "include": ["src"]
}
'''

APPSETTINGS = b'''{
  "Logging": {
    "LogLevel": {
      "Default": "Information",
      "Microsoft.AspNetCore": "Warning"
    }
  },
  "AllowedHosts": "*",
  "ConnectionStrings": {
    "Default": "Server=db;Database=app;Trusted_Connection=True;"
  }
}
'''

COMPACT = b'{"a":1,"b":{"c":2},"d":[1,2,{"e":3}]}'


def with_dep(blob, name, version):
    return blob.replace(b'"zod": "^3.22.0"\n', b'"zod": "^3.22.0",\n    "' + name + b'": "' + version + b'"\n')


class JsonReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "package.json"
    SAMPLES = (PACKAGE, TSCONFIG, APPSETTINGS, COMPACT,
               APPSETTINGS.replace(b"\n", b"\r\n"),
               '{\n  "name": "café",\n  "emoji": "\U0001f600"\n}\n'.encode())
    KEYS = (
        (0, ["name", "version", "scripts", "dependencies", "files"]),
        (3, ["a", "b", "d"]),
    )
    BROKEN = (
        b'{"a": 1,}',
        b'{"a": 1',
        b'{"a": 1, "a": 2}',
        b'{"o": {"x": 1, "x": 2}}',
        b'[1, 2, 3]',
        b'"text"',
        b'{"a": NaN}',
        br'{"items": [{"\u0061": 1, "a": 2}]}',
        b'{"x": 01}',
        b'{"x": 1.}',
        b'{"x": 1e+}',
        b'{"a": "open}',
        b'\xff\xfe{"a": 1}',
        b'',
    )
    MERGES = (
        # Adjacent members edited on both sides.
        (PACKAGE, PACKAGE.replace(b"1.4.2", b"1.5.0"), PACKAGE.replace(b"@acme/widgets", b"@acme/gadgets"),
         PACKAGE.replace(b"1.4.2", b"1.5.0").replace(b"@acme/widgets", b"@acme/gadgets")),
        # Both sides append a different member at the end of the object.
        (PACKAGE, PACKAGE.replace(b'"files": ["dist", "README.md"]\n', b'"files": ["dist", "README.md"],\n  "license": "MIT"\n'),
         PACKAGE.replace(b'"files": ["dist", "README.md"]\n', b'"files": ["dist", "README.md"],\n  "private": true\n'),
         PACKAGE.replace(b'"files": ["dist", "README.md"]\n', b'"files": ["dist", "README.md"],\n  "license": "MIT",\n  "private": true\n')),
        # Both sides add a different dependency to the nested object.
        (PACKAGE, with_dep(PACKAGE, b"axios", b"^1.6.0"), with_dep(PACKAGE, b"pino", b"^8.0.0"),
         PACKAGE.replace(b'"zod": "^3.22.0"\n', b'"zod": "^3.22.0",\n    "axios": "^1.6.0",\n    "pino": "^8.0.0"\n')),
        # One side edits a script, the other adds a dependency.
        (PACKAGE, PACKAGE.replace(b"jest --coverage", b"vitest run"), with_dep(PACKAGE, b"pino", b"^8.0.0"),
         with_dep(PACKAGE.replace(b"jest --coverage", b"vitest run"), b"pino", b"^8.0.0")),
        # Same member changed differently: refused.
        (PACKAGE, PACKAGE.replace(b"1.4.2", b"1.5.0"), PACKAGE.replace(b"1.4.2", b"2.0.0"), None),
        # Same dependency bumped to different versions: refused.
        (PACKAGE, PACKAGE.replace(b"^4.18.2", b"^4.19.0"), PACKAGE.replace(b"^4.18.2", b"^5.0.0"), None),
        # Both sides add the same key with different values: refused.
        (PACKAGE, with_dep(PACKAGE, b"pino", b"^8.0.0"), with_dep(PACKAGE, b"pino", b"^9.0.0"), None),
        # Nested compiler options: one edits, one adds.
        (TSCONFIG, TSCONFIG.replace(b'"strict": true', b'"strict": false'),
         TSCONFIG.replace(b'"target": "ES2022",', b'"target": "ES2022",\n    "lib": ["ES2022"],'),
         TSCONFIG.replace(b'"strict": true', b'"strict": false').replace(b'"target": "ES2022",', b'"target": "ES2022",\n    "lib": ["ES2022"],')),
        # Both sides insert at the very start of the object: both members kept.
        (COMPACT, b'{"x":0,' + COMPACT[1:], b'{"y":0,' + COMPACT[1:], b'{"x":0,"y":0,' + COMPACT[1:]),
        # One side deletes a member the other left alone.
        (PACKAGE, PACKAGE.replace(b'  "version": "1.4.2",\n', b""), PACKAGE.replace(b"jest --coverage", b"vitest run"),
         PACKAGE.replace(b'  "version": "1.4.2",\n', b"").replace(b"jest --coverage", b"vitest run")),
    )

    def test_member_extents_leave_commas_in_the_gap(self):
        blob = b'{\n  "a": 1,\n  "b": 2\n}\n'
        _, (_, _, _, nodes) = readers.outline(self.PATH, blob)
        self.assertEqual([blob[s:e] for _, s, e, _ in nodes], [b'  "a": 1', b'  "b": 2'])

    def test_merge_result_is_valid_json(self):
        import json
        for base, ours, theirs, expected in self.MERGES:
            if expected is not None:
                json.loads(expected)

    def test_non_ascii_offsets_are_bytes(self):
        blob = '{\n  "name": "café",\n  "emoji": "\U0001f600"\n}\n'.encode()
        _, (_, _, _, nodes) = readers.outline(self.PATH, blob)
        self.assertEqual([blob[s:e] for _, s, e, _ in nodes], ['  "name": "café"'.encode(), '  "emoji": "\U0001f600"'.encode()])

    def test_bom_stays_in_the_gap(self):
        blob = b"\xef\xbb\xbf" + COMPACT
        _, (_, _, _, nodes) = readers.outline(self.PATH, blob)
        self.assertEqual(nodes[0][1], 4)

    def test_suffix_is_json_only(self):
        self.assertIsNone(readers.reader_for("settings.jsonc"))

    def test_escaped_surrogate_keys_remain_distinct(self):
        blob = br'{"\ud800": 1, "\ud801": 2, "\ud800\udc00": 3}'
        nodes = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual([n[0] for n in nodes], ["\ud800", "\ud801", "\U00010000"])

    def test_deep_json_preserves_byte_extents(self):
        depth = 600
        blob = b'{"a":' * depth + b'0' + b'}' * depth
        nodes = readers.outline(self.PATH, blob)[1][3]
        for level in range(depth):
            ((key, start, end, nodes),) = nodes
            self.assertEqual((key, start, end), ("a", 5 * level + 1, len(blob) - level - 1))
        self.assertEqual(nodes, [])
        self.assertIsNone(readers.outline(self.PATH, b'{"a":' * 2000 + b'0' + b'}' * 2000))


if __name__ == "__main__":
    unittest.main()
