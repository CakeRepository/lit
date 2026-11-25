"""The shared test suite every lit reader must pass.

A reader's own test module subclasses ReaderConformance (with unittest.TestCase)
and sets:

- PATH: a file name with the reader's suffix, such as "app.ts".
- SAMPLES: realistic source files (bytes) the reader must outline.
- KEYS: [(sample index, [top-level keys in order])] for some samples.
- BROKEN: files the reader must refuse (return None): truncated, unterminated
  strings or comments, unbalanced brackets and so on.
- MERGES: [(base, ours, theirs, expected)] where expected is the merged bytes,
  or None when the merge must refuse. Cover at least: edits to two adjacent
  declarations; a declaration both sides add; the same declaration changed on
  both sides (None); an edit inside a container's member.

The shared checks then hold the reader to the contract in
concurrent_merge/readers/__init__.py.
"""

from concurrent_merge import readers
from concurrent_merge.structure import outline_merge


class ReaderConformance:
    PATH = None
    SAMPLES = ()
    KEYS = ()
    BROKEN = ()
    MERGES = ()

    def walk(self, nodes):
        for node in nodes:
            yield node
            yield from self.walk(node[3])

    def test_conformance_reader_is_registered(self):
        reader = readers.reader_for(self.PATH)
        self.assertIsNotNone(reader)
        self.assertTrue(reader.NAME)
        self.assertTrue(all(s.startswith(".") and s == s.lower() for s in reader.SUFFIXES))

    def test_conformance_samples_outline_within_the_contract(self):
        self.assertTrue(self.SAMPLES, "give the reader some samples")
        for sample in self.SAMPLES:
            with self.subTest(sample=sample[:60]):
                tree = readers.outline(self.PATH, sample)
                self.assertIsNotNone(tree, "the reader refused a sample")
                nodes = tree[1][3]
                self.assertTrue(nodes, "a sample produced no units")
                for node in self.walk(nodes):
                    self.assertIsInstance(node[0], str)
                    self.assertTrue(node[0].strip())

    def test_conformance_keys(self):
        for index, keys in self.KEYS:
            nodes = readers.outline(self.PATH, self.SAMPLES[index])[1][3]
            self.assertEqual([n[0] for n in nodes], keys)

    def test_conformance_outline_moves_with_the_text(self):
        # Text added before the file shifts every unit and changes no key.
        for sample in self.SAMPLES:
            before = readers.outline(self.PATH, sample)[1][3]
            shifted = readers.outline(self.PATH, b"\n\n" + sample)
            self.assertIsNotNone(shifted)
            self.assertEqual(
                [(k, s + 2, e + 2) for k, s, e, _ in self.walk(before)],
                [(k, s, e) for k, s, e, _ in self.walk(shifted[1][3])],
            )

    def test_conformance_broken_files_are_refused(self):
        self.assertTrue(self.BROKEN, "show the reader refusing what it cannot read")
        for blob in self.BROKEN:
            with self.subTest(blob=blob[:60]):
                self.assertIsNone(readers.outline(self.PATH, blob))
        for sample in self.SAMPLES:
            self.assertIsNone(readers.outline(self.PATH, sample + b"\x00"))

    def test_conformance_unsure_readers_never_merge_truncated_files(self):
        # Cutting a sample anywhere inside a unit must never produce a merge
        # that loses text: either the reader refuses, or the merge is exact.
        for sample in self.SAMPLES:
            for _, start, end, _ in self.walk(readers.outline(self.PATH, sample)[1][3]):
                cut = sample[: (start + end) // 2]
                tree = readers.outline(self.PATH, cut)
                if tree is not None:
                    self.assertTrue(readers.valid(tree[1][3], 0, len(cut)))

    def test_conformance_merges(self):
        self.assertTrue(self.MERGES, "give the reader some merges")
        refused = merged = 0
        for base, ours, theirs, expected in self.MERGES:
            with self.subTest(ours=ours[:60], theirs=theirs[:60]):
                result = outline_merge(self.PATH, [base, ours, theirs])
                self.assertEqual(result, expected)
                if expected is None:
                    refused += 1
                else:
                    merged += 1
                    # Merging is symmetric up to the order of additions.
                    self.assertIsNotNone(outline_merge(self.PATH, [base, theirs, ours]))
        self.assertTrue(refused and merged, "show at least one merge and one refusal")

    def test_conformance_identity_merges(self):
        for sample in self.SAMPLES:
            self.assertEqual(outline_merge(self.PATH, [sample, sample, sample]), sample)
