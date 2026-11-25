import unittest

from concurrent_merge import readers
from reader_conformance import ReaderConformance

README = b'''# lit

Agent-native merging.

[![build](https://example.invalid/badge.svg)](https://example.invalid)

## Install

```sh
# not a heading
pip install lit
```

## Usage

Run `lit merge`.

### Options

- `--verbose`
- `--dry-run`

### Exit codes

Zero on success.

## License

MIT
'''

CHANGELOG = b'''# Changelog

All notable changes are listed here.

## [1.2.0] - 2026-03-01

### Added

- Readers for YAML.

## [1.1.0] - 2026-02-01

### Fixed

- Tab handling.
'''


class MarkdownReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "README.md"
    SAMPLES = (README, CHANGELOG, b"Intro only\n\n## One\ntext\n## Two\ntext", b"~~~~\n# x\n~~~\n# y\n~~~~\n\n# real\n")
    KEYS = (
        (0, ["h1 lit"]),
        (1, ["h1 Changelog"]),
        (3, ["h1 real"]),
    )
    BROKEN = (
        b"# Title\n\n```\ncode never closed\n",
        b"# Title\n\n~~~\n# inside\n```\n",
        b"# Title\n\n<!-- open comment\n## Hidden\n",
        b"\xff\xfe# Title\n",
    )
    MERGES = (
        # Edits in two different sections.
        (README, README.replace(b"Agent-native merging.", b"Agent-native merging, in Python."),
         README.replace(b"MIT", b"Apache-2.0"),
         README.replace(b"Agent-native merging.", b"Agent-native merging, in Python.").replace(b"MIT", b"Apache-2.0")),
        # Edits in sibling sub-sections.
        (README, README.replace(b"- `--verbose`", b"- `--verbose`\n- `--json`"), README.replace(b"Zero on success.", b"Zero on success, one on conflicts."),
         README.replace(b"- `--verbose`", b"- `--verbose`\n- `--json`").replace(b"Zero on success.", b"Zero on success, one on conflicts.")),
        # Both sides add a different section at the end.
        (README, README + b"\n## Contributing\n\nSend a PR.\n", README + b"\n## Security\n\nEmail us.\n",
         README + b"\n## Contributing\n\nSend a PR.\n## Security\n\nEmail us.\n"),
        # Both sides add a release on top of the changelog: ours first.
        (CHANGELOG, CHANGELOG.replace(b"## [1.1.0]", b"## [1.3.0] - 2026-04-01\n\n- Ours.\n\n## [1.1.0]"),
         CHANGELOG.replace(b"## [1.1.0]", b"## [1.2.1] - 2026-03-15\n\n- Theirs.\n\n## [1.1.0]"),
         CHANGELOG.replace(b"## [1.1.0]", b"## [1.3.0] - 2026-04-01\n\n- Ours.\n\n## [1.2.1] - 2026-03-15\n\n- Theirs.\n\n## [1.1.0]")),
        # Same section changed on both sides.
        (README, README.replace(b"Run `lit merge`.", b"Run `lit merge a b c`."), README.replace(b"Run `lit merge`.", b"Run `lit resolve`."), None),
        # Same sub-section changed on both sides.
        (README, README.replace(b"Zero on success.", b"Zero."), README.replace(b"Zero on success.", b"0 on success."), None),
        # A new sub-section on each side under the same parent.
        (README, README.replace(b"## License", b"### Environment\n\nNone.\n\n## License"),
         README.replace(b"Run `lit merge`.", b"Run `lit merge`, then `lit check`."),
         README.replace(b"Run `lit merge`.", b"Run `lit merge`, then `lit check`.").replace(b"## License", b"### Environment\n\nNone.\n\n## License")),
    )

    def test_sections_nest_by_level(self):
        tree = readers.outline(self.PATH, README)[1][3]
        h1 = tree[0]
        self.assertEqual([c[0] for c in h1[3]], ["h2 Install", "h2 Usage", "h2 License"])
        usage = h1[3][1]
        self.assertEqual([c[0] for c in usage[3]], ["h3 Options", "h3 Exit codes"])

    def test_section_runs_to_next_heading_of_same_or_higher_level(self):
        tree = readers.outline(self.PATH, README)[1][3]
        install = tree[0][3][0]
        self.assertTrue(README[install[1]:install[2]].startswith(b"## Install"))
        self.assertTrue(README[install[1]:install[2]].endswith(b"pip install lit\n```\n\n"))

    def test_fenced_hashes_and_closing_hashes(self):
        blob = b"## Title ##\n\n````\n```\n# no\n````\n\n#hashtag\n\n   ### Indented  \n"
        tree = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual([n[0] for n in tree], ["h2 Title"])
        self.assertEqual([c[0] for c in tree[0][3]], ["h3 Indented"])

    def test_preface_stays_in_the_gap(self):
        blob = b"Intro\n\n# A\ntext\n"
        tree = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual(tree[0][1], len(b"Intro\n\n"))

    def test_front_matter_is_not_headings(self):
        blob = b"---\ntitle: x\n# comment\n---\n# Real\n"
        tree = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual([n[0] for n in tree], ["h1 Real"])

    def test_non_ascii_offsets_are_bytes(self):
        blob = "# café\n\ntext \U0001f600\n\n## next\n".encode()
        tree = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual(blob[tree[0][3][0][1]:], b"## next\n")

    def test_duplicates_are_kept_and_refused_by_the_merge(self):
        from concurrent_merge.structure import outline_merge
        blob = b"# A\n## Notes\none\n## Notes\ntwo\n"
        self.assertIsNotNone(readers.outline(self.PATH, blob))
        self.assertIsNone(outline_merge(self.PATH, [blob, blob.replace(b"one", b"1"), blob.replace(b"two", b"2")]))


if __name__ == "__main__":
    unittest.main()
