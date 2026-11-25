"""YAML reader: a conservative, indentation-based outline of block mappings.

Units are block-mapping keys: the top-level keys, and, below a key with no
inline value, the keys one indentation level down (and so on). A unit spans its
key line and every following line that is more indented, a block scalar's text,
or a sequence written at the key's own indentation; trailing blank and comment
lines stay in the text between units. Sequences, scalars and flow collections
are leaf content.

The reader refuses (None) whenever it is not sure: tabs in indentation, more
than one document, anchors, aliases, merge keys, flow collections or quoted
scalars spanning lines, block scalars with an indentation indicator, duplicate
keys, directives, or a document that is not a block mapping.
"""

import re

NAME = "yaml"
SUFFIXES = (".yml", ".yaml")


class Unsure(Exception):
    pass


KEY = re.compile(
    r'''(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\n]|'')*'|[^\s#'"\[\]{},&*!|>%@`?:\-][^\n]*?)\s*:(?=\s|$)'''
)
BLOCK = re.compile(r"[|>][-+0-9]*")
TAG = re.compile(r"!\S*\s*")
ANCHOR = re.compile(r"[&*][\w\-]")


class Line:
    __slots__ = ("start", "end", "kind", "indent", "dash", "key", "empty")

    def __init__(self, start, end, kind, indent=0, dash=False, key=None, empty=False):
        self.start, self.end, self.kind = start, end, kind
        self.indent, self.dash, self.key, self.empty = indent, dash, key, empty


def quoted_end(value):
    """Index just after the closing quote of a value that starts with a quote."""
    quote = value[0]
    i = 1
    while i < len(value):
        c = value[i]
        if quote == '"' and c == "\\":
            i += 2
            continue
        if c == quote:
            if quote == "'" and value[i + 1:i + 2] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    raise Unsure


def check_rest(rest):
    rest = rest.strip()
    if rest and not rest.startswith("#"):
        raise Unsure


def flow_end(value):
    """Check a one-line flow collection; returns nothing, refuses if unbalanced."""
    depth = 0
    i = 0
    plain = []
    while i < len(value):
        c = value[i]
        if c in "\"'" and (i == 0 or value[i - 1] in " \t[{,:"):
            i = quoted_end(value[i:]) + i
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth < 0:
                raise Unsure
            if depth == 0:
                check_rest(value[i + 1:])
                return "".join(plain)
        elif c == "#" and i > 0 and value[i - 1] in " \t":
            break
        plain.append(c)
        i += 1
    raise Unsure


def scalar_kind(value):
    """What a value is: 'empty', 'block' (block scalar header) or 'other'."""
    value = value.strip(" ")
    if not value or value.startswith("#"):
        return "empty"
    first = value[0]
    if first in "\"'":
        check_rest(value[quoted_end(value):])
        return "other"
    if first in "[{":
        inner = flow_end(value)
        for part in re.split(r"[\[\]{},:]\s*", inner):
            if ANCHOR.match(part.strip()) :
                raise Unsure
        return "other"
    plain = re.split(r"\s#", value, maxsplit=1)[0].strip()
    probe = plain
    while probe.startswith("!"):
        probe = TAG.sub("", probe, count=1)
    if ANCHOR.match(probe):
        raise Unsure
    if probe == "" and plain:
        return "other"
    if BLOCK.fullmatch(plain):
        if any(c.isdigit() for c in plain):
            raise Unsure
        return "block"
    return "other"


def key_text(raw):
    if raw[0] == '"':
        inner = raw[1:-1]
        if "\\" in inner:
            raise Unsure
        return inner
    if raw[0] == "'":
        return raw[1:-1].replace("''", "'")
    if "#" in raw:
        raise Unsure
    return raw


def classify(text):
    pieces = re.findall(r"[^\n]*\n|[^\n]+", text)
    position = 0
    if text.startswith("﻿"):
        pieces[0] = pieces[0][1:]
        position = 3
    lines = []
    scalar_owner = None
    scalar_indent = None
    content = False
    marker = False
    for piece in pieces:
        end = position + len(piece.encode("utf-8"))
        line = piece.rstrip("\r\n")
        stripped = line.strip(" \t\r")
        if scalar_owner is not None:
            if not stripped:
                lines.append(Line(position, end, "blank"))
                position = end
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent > scalar_owner:
                if scalar_indent is None:
                    scalar_indent = indent
                elif indent < scalar_indent:
                    raise Unsure
                lines.append(Line(position, end, "scalar", indent))
                position = end
                continue
            scalar_owner = None
        if not stripped:
            kind = "blank"
            lines.append(Line(position, end, kind))
            position = end
            continue
        indent = len(line) - len(line.lstrip(" "))
        if line[indent] in "\t":
            raise Unsure
        if stripped.startswith("#"):
            lines.append(Line(position, end, "comment"))
            position = end
            continue
        if indent == 0 and (line.startswith("---") or line.startswith("...") or line.startswith("%")):
            if line.startswith("---") and (len(line) == 3 or line[3] in " \t") and not content and not marker:
                check_rest(line[3:])
                marker = True
                lines.append(Line(position, end, "comment"))
                position = end
                continue
            raise Unsure
        content = True
        rest = line[indent:]
        column = indent
        dash = False
        dash_column = indent
        while rest == "-" or rest.startswith("- "):
            dash = True
            dash_column = column
            step = 1
            while rest[step:step + 1] == " ":
                step += 1
            column += step
            rest = rest[step:]
        if rest and rest[0] == "\t":
            raise Unsure
        match = KEY.match(rest)
        key = None
        if match:
            key = key_text(rest[: rest.rindex(":", 0, match.end())].rstrip())
            if key == "<<":
                raise Unsure
            value = rest[match.end():]
            owner = column
        else:
            value = rest
            owner = dash_column
        kind = scalar_kind(value)
        if kind == "block":
            scalar_owner, scalar_indent = owner, None
        lines.append(Line(position, end, "text", indent, dash, key, kind == "empty"))
        position = end
    return lines, position


def level(lines, lo, hi, indent):
    nodes = []
    seen = set()
    i = lo
    while i < hi:
        line = lines[i]
        if line.kind in ("blank", "comment"):
            i += 1
            continue
        if line.kind != "text" or line.indent != indent or line.dash or line.key is None:
            raise Unsure
        if line.key in seen:
            raise Unsure
        seen.add(line.key)
        end = i + 1
        j = i + 1
        while j < hi:
            other = lines[j]
            if other.kind in ("blank", "comment"):
                j += 1
                continue
            if other.kind == "scalar" or other.indent > indent or (
                line.empty and other.dash and other.indent == indent
            ):
                end = j + 1
                j += 1
                continue
            break
        children = []
        if line.empty:
            first = next((k for k in range(i + 1, end) if lines[k].kind == "text"), None)
            if first is not None and not lines[first].dash and lines[first].indent > indent:
                if lines[first].key is not None:
                    children = level(lines, first, end, lines[first].indent)
        nodes.append((line.key, i, end, children))
        i = end
    return nodes


def outline(blob, suffix):
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        lines, total = classify(text)
        first = next((k for k, line in enumerate(lines) if line.kind == "text"), None)
        if first is None:
            return []
        nodes = level(lines, 0, len(lines), lines[first].indent)
    except Unsure:
        return None

    def convert(found):
        out = []
        for key, a, b, children in found:
            out.append((key, lines[a].start, lines[b - 1].end, convert(children)))
        return out

    return convert(nodes)
