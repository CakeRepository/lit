"""Python reader, built on the standard library's own parser (ast)."""

import ast

NAME = "python"
SUFFIXES = (".py", ".pyi", ".pyw")
KINDS = {ast.FunctionDef: "def", ast.AsyncFunctionDef: "async def", ast.ClassDef: "class"}


def outline(blob, suffix):
    """Defs, classes and their methods, plus each top-level import, as whole lines.

    Compound statements never share a line, so whole-line extents never overlap.
    """
    try:
        module = ast.parse(blob)
    except (SyntaxError, ValueError):
        return None
    starts = [0]
    for line in blob.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def lines(first, last):
        return starts[first - 1], starts[last]

    def nodes(parent, top):
        found = []
        for child in ast.iter_child_nodes(parent):
            kind = KINDS.get(type(child))
            if kind is not None:
                first = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                found.append((f"{kind} {child.name}", *lines(first, child.end_lineno), nodes(child, False)))
            elif top and isinstance(child, (ast.Import, ast.ImportFrom)):
                # A simple statement can share its line (a; b), so only whole-line imports count.
                if child.lineno == child.end_lineno and own_line(blob, starts, child):
                    names = ",".join(a.name + (" as " + a.asname if a.asname else "") for a in child.names)
                    source = ("." * child.level + (child.module or "")) if isinstance(child, ast.ImportFrom) else ""
                    found.append((f"import {source}:{names}", *lines(child.lineno, child.lineno), []))
            elif not isinstance(child, ast.expr):
                found.extend(nodes(child, False) if kind is None and not top else [])
        return sorted(found, key=lambda n: n[1])

    return nodes(module, True)


def own_line(blob, starts, node):
    line = blob[starts[node.lineno - 1]:starts[node.lineno]]
    text = line.decode("utf8", "replace")
    return text[: node.col_offset].strip() == "" and ";" not in text
