"""JSON reader: the members of the top-level object, and of nested objects.

Units are `"key": value` members, keyed by the decoded key. Commas belong to
the text between units (the gap before the next member carries the comma that
separates it from the previous one), so a member added after the last member
brings its own comma and two sides that each append a member merge into valid
JSON. A member's extent therefore ends exactly at the end of its value (it does
not absorb the newline, because a trailing comma may or may not follow).
"""

import json
from json.decoder import scanstring

NAME = "json"
SUFFIXES = (".json",)
WS = " \t\n\r"


class Unsure(Exception):
    pass


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _refuse_constant(name):
    raise ValueError(name)


class _Scanner:
    def __init__(self, text):
        self.t = text
        self.decoder = json.JSONDecoder()

    def skip(self, i):
        t = self.t
        while i < len(t) and t[i] in WS:
            i += 1
        return i

    def members(self, i):
        """t[i] is '{'. Returns ([(key, start, end, children)], index after '}')."""
        t = self.t
        i = self.skip(i + 1)
        out = []
        if t[i] == "}":
            return out, i + 1
        while True:
            i = self.skip(i)
            if t[i] != '"':
                raise Unsure
            start = i
            key, i = scanstring(t, i + 1)
            i = self.skip(i)
            if t[i] != ":":
                raise Unsure
            i = self.skip(i + 1)
            if t[i] == "{":
                kids, i = self.members(i)
            else:
                kids = []
                _, i = self.decoder.raw_decode(t, i)
            out.append((key, start, i, kids))
            i = self.skip(i)
            if t[i] == ",":
                i += 1
            elif t[i] == "}":
                return out, i + 1
            else:
                raise Unsure


def outline(blob, suffix):
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    body = text[1:] if text.startswith("﻿") else text
    try:
        value = json.loads(body, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    scanner = _Scanner(text)
    try:
        first = scanner.skip(1 if text.startswith("﻿") else 0)
        members, _ = scanner.members(first)
    except (Unsure, IndexError, ValueError):
        return None
    if text.isascii():
        def byte(i):
            return i
    else:
        offsets = [0]
        for ch in text:
            offsets.append(offsets[-1] + len(ch.encode("utf-8")))
        def byte(i):
            return offsets[i]

    def line_start(i):
        j = i
        while j > 0 and text[j - 1] in " \t":
            j -= 1
        return j if j == 0 or text[j - 1] in "\n\r" else i

    def convert(nodes):
        return [(key, byte(line_start(s)), byte(e), convert(kids)) for key, s, e, kids in nodes]

    return convert(members)
