"""Markdown reader: ATX heading sections, nested by heading level.

A section runs from its heading line to the next heading of the same or a
higher level, so it holds its own text and its sub-sections. Text before the
first heading stays in the gap. Headings inside fenced code blocks, HTML
comments and YAML front matter are not headings.
"""

import re

NAME = "markdown"
SUFFIXES = (".md", ".markdown")

HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
CLOSING = re.compile(r"[ \t]+#+$")
FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


def outline(blob, suffix):
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    pieces = re.findall(r"[^\n]*\n|[^\n]+", text)
    position = 3 if text.startswith("﻿") else 0
    if pieces and text.startswith("﻿"):
        pieces[0] = pieces[0][1:]
    lines = []
    for piece in pieces:
        lines.append((position, piece.rstrip("\r\n")))
        position += len(piece.encode("utf-8"))
    total = position

    headings = []
    fence = None
    comment = False
    first = 0
    if lines and lines[0][1].rstrip() == "---":
        for number in range(1, len(lines)):
            if lines[number][1].rstrip() in ("---", "..."):
                first = number + 1
                break
    for number in range(first, len(lines)):
        start, line = lines[number]
        if fence is not None:
            closing = re.match(r"^ {0,3}(" + re.escape(fence[0]) + "{%d,})[ \\t]*$" % len(fence), line)
            if closing:
                fence = None
            continue
        if comment:
            if "-->" in line:
                comment = False
            continue
        opened = FENCE.match(line)
        if opened and not (opened.group(2)[0] == "`" and "`" in opened.group(3)):
            fence = opened.group(2)
            continue
        if "<!--" in line and "-->" not in line[line.rindex("<!--"):]:
            comment = True
            continue
        found = HEADING.match(line)
        if found:
            title = CLOSING.sub("", found.group(2) or "")
            if re.fullmatch(r"#+", found.group(2) or ""):
                title = ""
            title = " ".join(title.split())
            level = len(found.group(1))
            headings.append((level, f"h{level} {title}".strip(), start))
    if fence is not None or comment:
        return None

    def build(index, parent):
        nodes = []
        while index < len(headings) and headings[index][0] > parent:
            level, key, start = headings[index]
            children, following = build(index + 1, level)
            end = headings[following][2] if following < len(headings) else total
            nodes.append((key, start, end, children))
            index = following
        return nodes, index

    return build(0, 0)[0]
