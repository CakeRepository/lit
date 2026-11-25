"""Conservative three-way merge of declaration extents, for Python and PowerShell sources.

Both merges are lit's own: Python files are parsed with the standard library's
`ast`, PowerShell files with PowerShell's own parser. A file in any other
language keeps Git's line merge.
"""

import ast
import codecs
import json
from pathlib import Path
import shutil
import subprocess


def choose(base, ours, theirs):
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    return None


def ps_parse(texts):
    proc = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(Path(__file__).with_name("ps_nodes.ps1")),
        ],
        input=json.dumps(texts).encode(),
        capture_output=True,
        check=True,
    )
    records = json.loads(proc.stdout)
    if not all(record["valid"] for record in records):
        return None
    return records


def extent_tree(text, nodes):
    # PowerShell offsets count UTF-16 code units, including both halves of emoji.
    raw = text.encode("utf-16-le")
    root = ("<file>", 0, len(raw), [])
    stack = [root]
    for item in sorted(nodes, key=lambda n: (n["start"], -n["end"])):
        node = (item["key"], 2 * item["start"], 2 * item["end"], [])
        while len(stack) > 1 and node[1] >= stack[-1][2]:
            stack.pop()
        stack[-1][3].append(node)
        stack.append(node)
    return raw, root


def merge_extent(parts):
    same = choose(*(raw[node[1] : node[2]] for raw, node in parts))
    if same is not None:
        return same
    names = [[child[0] for child in node[3]] for _, node in parts]
    if (
        not names[0]
        or names[0] != names[1]
        or names[0] != names[2]
        or len(set(names[0])) != len(names[0])
    ):
        return None
    output = []
    starts = [node[1] for _, node in parts]
    for children in zip(*(node[3] for _, node in parts)):
        gap = choose(
            *(
                raw[start : child[1]]
                for (raw, _), start, child in zip(parts, starts, children)
            )
        )
        body = merge_extent([(raw, child) for (raw, _), child in zip(parts, children)])
        if gap is None or body is None:
            return None
        output.extend([gap, body])
        starts = [child[2] for child in children]
    tail = choose(*(raw[start : node[2]] for (raw, node), start in zip(parts, starts)))
    return None if tail is None else b"".join([*output, tail])


def powershell_merge(blobs):
    if not shutil.which("pwsh"):
        return None

    def encoding(blob):
        for prefix, enc in [
            (codecs.BOM_UTF8, "utf-8"),
            (codecs.BOM_UTF16_LE, "utf-16-le"),
            (codecs.BOM_UTF16_BE, "utf-16-be"),
        ]:
            if blob.startswith(prefix):
                return prefix, enc
        return b"", "utf-8"

    formats = [encoding(blob) for blob in blobs]
    if len(set(formats)) != 1:
        return None
    prefix, enc = formats[0]
    try:
        texts = [blob[len(prefix) :].decode(enc) for blob in blobs]
    except UnicodeError:
        return None
    records = ps_parse(texts)
    if records is None:
        return None
    merged = merge_extent([extent_tree(t, r["nodes"]) for t, r in zip(texts, records)])
    if merged is None:
        return None
    text = merged.decode("utf-16-le")
    return prefix + text.encode(enc) if ps_parse([text]) is not None else None


def python_tree(blob):
    """(raw, root) extent tree of a Python file's definitions, or None if it does not parse.

    A node is a def, async def or class, keyed by kind and name, and spans whole
    lines from its first decorator to its last line. Compound statements never
    share a line, so line extents never overlap a sibling.
    """
    try:
        module = ast.parse(blob)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    starts = [0]
    for line in blob.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    kinds = {ast.FunctionDef: "def", ast.AsyncFunctionDef: "async def", ast.ClassDef: "class"}

    def nodes(parent):
        found = []
        for child in ast.iter_child_nodes(parent):
            kind = kinds.get(type(child))
            if kind is None:
                found.extend(nodes(child))
                continue
            first = min([child.lineno, *(d.lineno for d in child.decorator_list)])
            found.append(
                (f"{kind} {child.name}", starts[first - 1], starts[child.end_lineno], nodes(child))
            )
        return sorted(found, key=lambda n: n[1])

    return blob, ("<file>", 0, len(blob), nodes(module))


def python_merge(blobs):
    trees = [python_tree(blob) for blob in blobs]
    if None in trees:
        return None
    merged = merge_extent(trees)
    return merged if merged is not None and python_tree(merged) is not None else None


PS_FILES = {".ps1", ".psm1", ".psd1"}
PY_FILES = {".py", ".pyi", ".pyw"}


def has_driver(path):
    suffix = Path(path).suffix.lower()
    return suffix in PY_FILES or (suffix in PS_FILES and bool(shutil.which("pwsh")))


def resolve_file(path, blobs):
    suffix = Path(path).suffix.lower()
    if suffix in PS_FILES:
        merged = powershell_merge(blobs)
        return (merged, "powershell-ast") if merged is not None else (None, None)
    if suffix in PY_FILES:
        merged = python_merge(blobs)
        return (merged, "python-ast") if merged is not None else (None, None)
    return None, None
