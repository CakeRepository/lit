"""lit's language readers: where each declaration in a source file starts and ends.

A reader is lit's own code for one language. It does not build a full syntax
tree. It finds the declarations a merge can treat as units (functions, classes,
methods, imports, type declarations, ...) and the byte range each one covers.
lit's structural merge (structure.tree_merge) then merges two edits of a file
unit by unit, and hands anything a reader cannot vouch for back to the author.

Writing a reader (see CONTRIBUTING.md):

- A reader is a module in this package with SUFFIXES (lower-case file suffixes,
  with the dot), NAME (the language, for receipts) and outline(blob, suffix).
- outline returns the file's top-level nodes, or None whenever the reader is
  not sure it read the file right: undecodable bytes, a NUL byte, an
  unterminated string, comment or template, unbalanced brackets, or any
  declaration-level construct it does not understand. None never breaks code:
  the file falls back to Git's line merge and a real overlap goes back to the
  change that caused it. Guessing can.
- A node is (key, start, end, children): a string key, byte offsets into the
  blob with start < end, and child nodes for containers (a class's members, a
  namespace's declarations). Siblings are sorted and never overlap, and
  children lie inside their parent. A function body has no children.
- Keys name the unit, such as "function load" or "method render", and should
  be unique among siblings. The merge refuses a level with duplicate keys.
- Extents: a node starts at the start of its line when only whitespace comes
  before it on that line, and ends after its line's newline when only
  whitespace follows it. Otherwise it covers exactly its own text. Decorators,
  attributes, export and modifiers belong to the node; comments before it
  belong to the text between nodes.
- Every reader passes tests/reader_conformance.py.
"""

import importlib
import json
from pathlib import Path
import pkgutil
import subprocess

READERS = {}
for _info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    _module = importlib.import_module(f"{__name__}.{_info.name}")
    for _suffix in _module.SUFFIXES:
        if _suffix in READERS:
            raise ImportError(f"Two lit readers claim {_suffix}")
        READERS[_suffix] = _module

ISSUES = "https://github.com/CakeRepository/lit/issues"

# The binary supplies its own path and the native readers it contains. The
# Python entry point and the reference side of parity keep their own readers.
native_program = None
native_suffixes = ()


def reader_for(path):
    return READERS.get(Path(path).suffix.lower())


def outline(path, blob):
    """(blob, root node) for a file lit has a reader for, or None."""
    reader = reader_for(path)
    if reader is None or b"\x00" in blob:
        return None
    try:
        suffix = Path(path).suffix.lower()
        if native_program and suffix in native_suffixes:
            run = subprocess.run([native_program, "--internal-outline", suffix],
                                 input=blob, stdout=subprocess.PIPE, check=True)
            nodes = json.loads(run.stdout)
        else:
            nodes = reader.outline(blob, suffix)
    except (RecursionError, MemoryError, ValueError, UnicodeError):
        return None
    if nodes is None or not valid(nodes, 0, len(blob)):
        return None
    return blob, ("<file>", 0, len(blob), nodes)


def valid(nodes, low, high):
    """Siblings sorted, non-empty, non-overlapping and inside [low, high]."""
    position = low
    for node in nodes:
        if len(node) != 4 or not isinstance(node[0], str):
            return False
        key, start, end, children = node
        if not (position <= start < end <= high) or not valid(children, start, end):
            return False
        position = end
    return True


PLAIN_TEXT = ("", ".txt", ".text", ".log")


def missing_reader_hint(path):
    """What lit tells an agent about a code file it could only compare line by line.

    None for plain text, which has no language to teach.
    """
    suffix = Path(path).suffix.lower()
    if suffix in PLAIN_TEXT:
        return None
    return (
        f"lit has no reader for {suffix} files yet, so it could only compare {path} "
        f"line by line. Agents: you can teach lit this language. Open an issue at {ISSUES} "
        "and send a PR that adds a reader under concurrent_merge/readers/ (see CONTRIBUTING.md)."
    )
