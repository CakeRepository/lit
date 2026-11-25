# Contributing to lit

Thanks for helping. lit is meant to spread, and the fastest way it gets better is agents and people teaching it the languages they work in.

## Teach lit a language

When two changes touch the same file, lit first tries Git's line merge. If the lines overlap, lit asks a reader for that language to outline the file, and merges declaration by declaration: two agents can each add a function, or each change a different method, and both land. A file with no reader keeps the line merge, and a real overlap goes back to the change that caused it.

When lit meets a code file it has no reader for, it says so in its output and in the conflict record (`hint`). If you are an agent and that happens to you:

1. Open an issue at https://github.com/CakeRepository/lit/issues saying which language and file suffixes you need.
2. Send a pull request that adds a reader, as described below.

### What a reader is

A reader is one Python module in `concurrent_merge/readers/`. lit finds it on its own; you don't register it anywhere. It uses only the Python standard library and calls no outside program.

```python
NAME = "go"                 # shown as the merge method, "go-outline"
SUFFIXES = (".go",)         # lower case, each claimed by one reader only

def outline(blob: bytes, suffix: str):
    """Return the file's outline, or None when you are not sure."""
```

`outline` returns a list of nodes. A node is a tuple `(key, start, end, children)`:

- `key` names the declaration so the same thing can be found in another version of the file, such as `"func Parse"` or `"type Config"`. Keys must be unique among siblings.
- `start` and `end` are byte offsets into `blob`.
- `children` is a list of nodes inside it, such as a class's methods. Function bodies have no children.

The rules lit checks for you:

- Siblings are sorted, don't overlap, and sit inside their parent.
- A node that has only whitespace before it on its line starts at the beginning of that line, and one that has only whitespace after it ends after the newline.
- Decorators, attributes and modifiers belong to the node. Comments before a node belong to the gap before it.

### The one rule that matters: fail safe

When your reader isn't sure, it returns `None`. A file it can't fully understand, a syntax it doesn't know, a construct that might hide a declaration: return `None`. lit then sends the overlap back to the change's author, which is always safe. A reader that guesses can merge two changes into code that neither author wrote. Raising `ValueError`, `UnicodeError` or `RecursionError` also counts as not sure.

### The shared tests

Every reader passes the same merge tests. Add `tests/test_reader_<name>.py`:

```python
import unittest
from reader_conformance import ReaderConformance

class GoReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "cmd/main.go"
    SAMPLES = [b"package main\n\nfunc A() {}\n\nfunc B() {}\n"]
    KEYS = ["func A", "func B"]          # top-level keys of SAMPLES[0]
    BROKEN = [b"package main\n\nfunc A( {\n"]
    MERGES = [
        # (base, ours, theirs, expected merge or None)
        (base, ours, theirs, merged),
        (base, ours, conflicting_theirs, None),
    ]
```

The suite checks that the reader is registered, that every outline keeps the rules above, that outlines move with the text, that broken and cut-off files are refused or still valid, and that each merge gives the expected result in both directions. Include at least one merge that succeeds and one that must be refused. Run everything with:

```
python3 -m unittest discover -s tests
```

## How pull requests land

lit lands its own pull requests: the maintainers batch them with lit, test the combined tree, and push.

Pull requests from outside contributors get a review first: correctness, the fail-safe rule, the shared tests, and that nothing reaches outside the repository (no network or file access beyond the blob it was given, and no changes to CI, release or credential settings). After the review, a maintainer decides whether it lands. lit's own `collect --hold-outside` keeps outside PRs out of a batch until they are passed with `--allow-outside`.

## License

By contributing you agree that your work is licensed under the Apache License 2.0, the same as lit.
