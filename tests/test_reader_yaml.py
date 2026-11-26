import unittest

from concurrent_merge import readers
from concurrent_merge.structure import outline_merge
from reader_conformance import ReaderConformance

WORKFLOW = b'''name: CI

on:
  push:
    branches: [main]
  pull_request:

env:
  PYTHON: "3.12"

# Jobs run in parallel.
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: |
          python -m pip install -e .
          cp *.txt out/   # a glob is fine inside a block scalar
      - run: python3 -m unittest discover -s tests

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ruff check .
'''

COMPOSE = b'''version: "3.9"
services:
  web:
    image: ghcr.io/acme/web:latest
    ports:
      - "8080:80"
    environment:
      LOG_LEVEL: info
    depends_on:
      - db
  db:
    image: postgres:16
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata: {}
'''

SEQ_AT_KEY = b'''matrix:
- a
- b
other: 1
'''


def job(name):
    return b"\n  " + name + b":\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo " + name + b"\n"


class YamlReaderTests(ReaderConformance, unittest.TestCase):
    PATH = ".github/workflows/ci.yml"
    SAMPLES = (WORKFLOW, COMPOSE, SEQ_AT_KEY, b"---\n# header\na: 1\nb:\n  c: 2\n", WORKFLOW.replace(b"\n", b"\r\n"))
    KEYS = (
        (0, ["name", "on", "env", "jobs"]),
        (1, ["version", "services", "volumes"]),
        (2, ["matrix", "other"]),
    )
    BROKEN = (
        b"a: 1\n---\nb: 2\n",
        b"a:\n\tb: 1\n",
        b"base: &base\n  x: 1\nother:\n  <<: *base\n",
        b"a: *alias\n",
        b"a: !tag &alias value\n",
        b"a: { nested: [*alias] }\n",
        b"a: |\n    first line\n  shallower line\n",
        b"a: 1\na: 2\n",
        b"top:\n  x: 1\n  x: 2\n",
        b"a: [1,\n  2]\n",
        b"a: \"never closed\n",
        b"a: |2\n    keep\n",
        b"- just\n- a list\n",
        b"plain scalar\n",
        b"a: 1\n...\n",
        b"\xff\xfea: 1\n",
        b"top:\n    a: 1\n  b: 2\n",
    )
    MERGES = (
        # Different jobs edited on both sides.
        (WORKFLOW, WORKFLOW.replace(b"ruff check .", b"ruff check --fix ."), WORKFLOW.replace(b"3.12", b"3.13"),
         WORKFLOW.replace(b"ruff check .", b"ruff check --fix .").replace(b"3.12", b"3.13")),
        # Both sides add a different job at the end.
        (WORKFLOW, WORKFLOW + job(b"docs"), WORKFLOW + job(b"audit"), WORKFLOW + job(b"docs") + job(b"audit")),
        # Both sides add a different service: ours first.
        (COMPOSE, COMPOSE.replace(b"volumes:\n  pgdata", b"  cache:\n    image: redis:7\nvolumes:\n  pgdata"),
         COMPOSE.replace(b"volumes:\n  pgdata", b"  mail:\n    image: mailhog/mailhog\nvolumes:\n  pgdata"),
         COMPOSE.replace(b"volumes:\n  pgdata", b"  cache:\n    image: redis:7\n  mail:\n    image: mailhog/mailhog\nvolumes:\n  pgdata")),
        # Edits inside two sibling services.
        (COMPOSE, COMPOSE.replace(b"web:latest", b"web:1.2"), COMPOSE.replace(b"postgres:16", b"postgres:17"),
         COMPOSE.replace(b"web:latest", b"web:1.2").replace(b"postgres:16", b"postgres:17")),
        # One edits a nested key, the other adds a new key to the same service.
        (COMPOSE, COMPOSE.replace(b"LOG_LEVEL: info", b"LOG_LEVEL: debug"),
         COMPOSE.replace(b"    depends_on:", b"    restart: always\n    depends_on:"),
         COMPOSE.replace(b"LOG_LEVEL: info", b"LOG_LEVEL: debug").replace(b"    depends_on:", b"    restart: always\n    depends_on:")),
        # Same key changed on both sides.
        (COMPOSE, COMPOSE.replace(b"postgres:16", b"postgres:17"), COMPOSE.replace(b"postgres:16", b"postgres:15"), None),
        # Both sides edit the same sequence (leaf content).
        (WORKFLOW, WORKFLOW.replace(b"ruff check .", b"ruff check src"), WORKFLOW.replace(b"ruff check .", b"ruff check tests"), None),
        # A merge that would introduce a refused construct is refused.
        (COMPOSE, COMPOSE.replace(b"web:latest", b"web:1"), COMPOSE + b"extra: &a 1\n", None),
    )

    def test_extents_are_whole_lines_and_leave_comments_in_the_gap(self):
        nodes = readers.outline(self.PATH, WORKFLOW)[1][3]
        jobs = nodes[3]
        self.assertTrue(WORKFLOW[jobs[1]:jobs[2]].startswith(b"jobs:\n  test:"))
        self.assertTrue(WORKFLOW[jobs[1]:jobs[2]].endswith(b"ruff check .\n"))
        env = nodes[2]
        self.assertTrue(WORKFLOW[env[2]:jobs[1]].startswith(b"\n# Jobs run in parallel.\n"))
        self.assertEqual([c[0] for c in jobs[3]], ["test", "lint"])
        self.assertEqual([c[0] for c in jobs[3][0][3]], ["runs-on", "steps"])
        steps = jobs[3][0][3][1]
        self.assertEqual(steps[3], [])

    def test_block_scalar_text_is_never_parsed(self):
        blob = b"run: |\n  a: 1\n  a: 2\n  - &x\n  ---\nother: 1\n"
        self.assertEqual([n[0] for n in readers.outline(self.PATH, blob)[1][3]], ["run", "other"])

    def test_quoted_keys_and_comments(self):
        blob = b'"on": # trigger\n  \'push\': {}\n# tail\n'
        nodes = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual([n[0] for n in nodes], ["on"])
        self.assertEqual([c[0] for c in nodes[0][3]], ["push"])
        self.assertEqual(nodes[0][2], len(blob) - len(b"# tail\n"))

    def test_leading_document_marker_is_allowed(self):
        self.assertIsNotNone(readers.outline(self.PATH, b"---\na: 1\n"))

    def test_non_ascii_offsets_are_bytes(self):
        blob = "name: caf\u00e9 \U0001f600\nnext: 1\n".encode()
        nodes = readers.outline(self.PATH, blob)[1][3]
        self.assertEqual(blob[nodes[1][1]:nodes[1][2]], b"next: 1\n")

    def test_merged_files_still_outline(self):
        merged = outline_merge(self.PATH, [WORKFLOW, WORKFLOW + job(b"docs"), WORKFLOW + job(b"audit")])
        self.assertEqual([c[0] for c in readers.outline(self.PATH, merged)[1][3][3][3]], ["test", "lint", "docs", "audit"])


if __name__ == "__main__":
    unittest.main()
