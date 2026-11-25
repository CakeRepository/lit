"""C# reader, written with only the standard library (no dotnet, no Roslyn).

It tokenizes C# (comments, regular, verbatim, interpolated and raw string
literals, char literals, preprocessor lines), matches ( ) [ ] { }, and walks
declarations: usings, namespaces (block and file-scoped), types, delegates and
the members of a type. Generic angle brackets are never matched.

Units and their keys:

- "using X", "global using X", "using static X", "using A=B", "extern alias X"
- "attribute [assembly: ...]" for assembly and module attributes
- "namespace A.B" (block namespaces have children; a file-scoped `namespace A;`
  is one unit and the declarations after it stay top-level)
- "class Name", "struct Name", "interface Name", "enum Name", "record Name"
  (record struct and record class too), "delegate Name"; a generic type adds its
  arity, so "class Box`1" and "class Box" differ
- in a type: "method Name(types)" (generic methods "Name`1"), "ctor(types)",
  "static ctor()", "finalizer Name", "property Name", "field a" / "field a,b",
  "event Name", "indexer(types)", "operator +(types)",
  "operator implicit T(types)", nested types, and for enums "member X"

Attributes and modifiers belong to the node, and so does the `;` after a type's
closing brace. Comments (doc comments too), #region lines and other
preprocessor lines before a unit stay in the text between nodes. Top-level
statements (C# 9 programs) are not units either; they stay in that text.

Enum members: the comma between two members is not part of either member; it
stays in the text between nodes (the next member's gap). Two sides that each add
a member at the end of an enum then merge, because the added member carries its
own leading comma. When a side adds a final member to an enum whose last member
had no comma, the old last member and the new one are separate units, so the
merged text can have the comma at the start of a line, which is still valid C#.

The reader returns None (Git's line merge) for #if/#elif/#else/#endif (branches
may disagree on braces), unknown directives, unterminated strings, comments and
raw strings, unbalanced brackets, undecodable input and any declaration-level
construct it does not recognise.
"""

import re
from itertools import accumulate

NAME = "csharp"
SUFFIXES = (".cs",)

MODIFIERS = frozenset(
    "public private protected internal static partial sealed abstract readonly file new "
    "unsafe extern virtual override async volatile required const ref".split()
)
CONTROL = frozenset(
    "if else for foreach while do switch try catch finally using lock unsafe fixed checked unchecked".split()
)
DIRECTIVES = frozenset("region endregion pragma nullable define undef line error warning".split())
TWO = frozenset(("=>", "==", "!=", "<=", ">=", "::", "->"))
OPEN = {"(": ")", "[": "]", "{": "}"}
CLOSE = frozenset(OPEN.values())

WORD = re.compile(r"\w+")
SPACE = re.compile(r"[\s﻿]+")
DIRECTIVE = re.compile(r"#\s*([A-Za-z]+)")
REGULAR = re.compile(r'["\\\n]')
INTERP_REGULAR = re.compile(r'["\\\n{}]')
INTERP_VERBATIM = re.compile(r'["{}]')
RAW_PLAIN = re.compile(r'"')
RAW_INTERP = re.compile(r'["{]')


class Refuse(Exception):
    """The reader is not sure: the whole file falls back to the line merge."""


class NotDeclaration(Exception):
    """A file-level construct that looked like a declaration but is a statement."""


# ---------------------------------------------------------------- tokenizer


def scan_char(s, i):
    if s[i + 1 : i + 2] == "\\":
        j = s.find("'", i + 3)
        if j < 0 or j - i > 12:
            raise Refuse
        return j + 1
    if s[i + 2 : i + 3] == "'" and s[i + 1 : i + 2] not in ("'", "\n", ""):
        return i + 3
    raise Refuse


def scan_hole(s, i):
    """Index of the } that closes an interpolation hole whose text starts at i."""
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in "([{":
            depth += 1
        elif c in ")]":
            depth -= 1
            if depth < 0:
                raise Refuse
        elif c == "}":
            if depth == 0:
                return i
            depth -= 1
        elif c == '"' or c == "$" or (c == "@" and s[i + 1 : i + 2] in ('"', "$")):
            i = scan_string(s, i)
            continue
        elif c == "'":
            i = scan_char(s, i)
            continue
        elif c == ":" and depth == 0:
            if s[i + 1 : i + 2] == ":":
                i += 2
                continue
            j = s.find("}", i)  # format specifier: raw text up to the closing brace
            if j < 0:
                raise Refuse
            return j
        i += 1
    raise Refuse


def scan_raw(s, j, quotes, braces):
    """End of a raw string literal whose opening quotes start at j."""
    pat = RAW_INTERP if braces else RAW_PLAIN
    pos = j + quotes
    while True:
        m = pat.search(s, pos)
        if not m:
            raise Refuse
        p = m.start()
        run = 1
        if s[p] == '"':
            while s[p + run : p + run + 1] == '"':
                run += 1
            if run >= quotes:
                if run > quotes:
                    raise Refuse
                return p + run
            pos = p + run
        else:
            while s[p + run : p + run + 1] == "{":
                run += 1
            if run < braces:
                pos = p + run
                continue
            if run >= 2 * braces:
                raise Refuse
            end = scan_hole(s, p + run)
            if s[end : end + braces] != "}" * braces:
                raise Refuse
            pos = end + braces


def scan_string(s, i):
    """End (exclusive) of the string literal starting at i ($, @ and " prefixes)."""
    n, j, dollars, ats = len(s), i, 0, 0
    while j < n and s[j] in "$@":
        dollars += s[j] == "$"
        ats += s[j] == "@"
        j += 1
    if ats > 1 or j >= n or s[j] != '"':
        raise Refuse
    verbatim = ats == 1
    quotes = 1
    while s[j + quotes : j + quotes + 1] == '"':
        quotes += 1
    if not verbatim and quotes >= 3:
        return scan_raw(s, j, quotes, dollars)
    if dollars > 1:
        raise Refuse
    pos = j + 1
    if not dollars:
        if verbatim:
            while True:
                p = s.find('"', pos)
                if p < 0:
                    raise Refuse
                if s[p + 1 : p + 2] == '"':
                    pos = p + 2
                else:
                    return p + 1
        while True:
            m = REGULAR.search(s, pos)
            if not m:
                raise Refuse
            if m.group() == '"':
                return m.end()
            if m.group() == "\n":
                raise Refuse
            pos = m.start() + 2
    pat = INTERP_VERBATIM if verbatim else INTERP_REGULAR
    while True:
        m = pat.search(s, pos)
        if not m:
            raise Refuse
        p, ch = m.start(), m.group()
        if ch == '"':
            if verbatim and s[p + 1 : p + 2] == '"':
                pos = p + 2
                continue
            return p + 1
        if ch == "\\":
            pos = p + 2
        elif ch == "\n":
            raise Refuse
        elif ch == "{":
            if s[p + 1 : p + 2] == "{":
                pos = p + 2
            else:
                pos = scan_hole(s, p + 1) + 1
        else:
            if s[p + 1 : p + 2] != "}":
                raise Refuse
            pos = p + 2


def lex(s):
    """Tokens (kind, text, start, end): kind i(dentifier/keyword), n, s, c(har) or p(unct)."""
    toks, n, i = [], len(s), 0
    append = toks.append
    while i < n:
        c = s[i]
        if c.isspace() or c == "﻿":
            i = SPACE.match(s, i).end()
        elif c == "/":
            d = s[i + 1 : i + 2]
            if d == "/":
                j = s.find("\n", i)
                i = n if j < 0 else j
            elif d == "*":
                j = s.find("*/", i + 2)
                if j < 0:
                    raise Refuse
                i = j + 2
            else:
                append(("p", "/", i, i + 1))
                i += 1
        elif c == "#":
            if s[s.rfind("\n", 0, i) + 1 : i].strip():
                raise Refuse
            j = s.find("\n", i)
            j = n if j < 0 else j
            m = DIRECTIVE.match(s, i, j)
            if not m or m.group(1) not in DIRECTIVES:
                raise Refuse  # #if/#elif/#else/#endif and anything unknown
            i = j
        elif c == "$" or c == '"' or (c == "@" and s[i + 1 : i + 2] in ('"', "$")):
            e = scan_string(s, i)
            append(("s", s[i:e], i, e))
            i = e
        elif c == "'":
            e = scan_char(s, i)
            append(("c", s[i:e], i, e))
            i = e
        elif c == "@":
            m = WORD.match(s, i + 1)
            if not m or m.group()[0].isdigit():
                raise Refuse
            append(("i", s[i : m.end()], i, m.end()))
            i = m.end()
        elif c.isalnum() or c == "_":
            m = WORD.match(s, i)
            append(("n" if c.isdigit() else "i", m.group(), i, m.end()))
            i = m.end()
        elif c == "\\" or c == "`":
            raise Refuse
        else:
            two = s[i : i + 2]
            if two in TWO:
                append(("p", two, i, i + 2))
                i += 2
            else:
                append(("p", c, i, i + 1))
                i += 1
    return toks


def match_brackets(toks):
    match, stack = [-1] * len(toks), []
    for index, (kind, text, _, _) in enumerate(toks):
        if kind != "p":
            continue
        if text in OPEN:
            stack.append(index)
        elif text in CLOSE:
            if not stack or OPEN[toks[stack[-1]][1]] != text:
                raise Refuse
            opener = stack.pop()
            match[opener] = index
            match[index] = opener
    if stack:
        raise Refuse
    return match


def join(texts):
    """Token texts with a space only where two words would otherwise merge."""
    out, prev = [], ""
    for text in texts:
        if prev and (prev[-1].isalnum() or prev[-1] in "_@") and (text[0].isalnum() or text[0] in "_@"):
            out.append(" ")
        out.append(text)
        prev = text
    return "".join(out)


# ------------------------------------------------------------------- parser


class Parser:
    def __init__(self, text, base):
        self.s = text
        self.toks = lex(text)
        self.match = match_brackets(self.toks)
        pad = [("e", "", len(text), len(text))] * 6
        self.count = len(self.toks)
        self.toks += pad
        self.match += [-1] * 6
        self.t = [tok[1] for tok in self.toks]
        self.k = [tok[0] for tok in self.toks]
        self.base = base
        self.offsets = None if text.isascii() else [0, *accumulate(len(c.encode("utf-8")) for c in text)]
        self.file_scoped = False

    # -- helpers

    def byte(self, pos):
        return self.base + (pos if self.offsets is None else self.offsets[pos])

    def node(self, key, a, b, children):
        """Node over tokens a..b (inclusive) with the contract's line extents."""
        s = self.s
        start, end = self.toks[a][2], self.toks[b][3]
        line = s.rfind("\n", 0, start) + 1
        if not s[line:start].replace("﻿", "").strip():
            start = line
        newline = s.find("\n", end)
        if newline >= 0 and not s[end:newline].replace("﻿", "").strip():
            end = newline + 1
        return (key, self.byte(start), self.byte(end), children)

    def opener(self, i):
        return self.k[i] == "p" and self.t[i] in OPEN

    def skip_attrs(self, i):
        while self.t[i] == "[" and self.k[i] == "p":
            i = self.match[i] + 1
        return i

    def skip_mods(self, i):
        while self.k[i] == "i" and self.t[i] in MODIFIERS:
            i += 1
        return i

    def head(self, i, limit, terms):
        """Depth-0 token indexes from i up to a terminator; a group counts as its opener."""
        entries = []
        while i < limit:
            if self.k[i] == "p":
                text = self.t[i]
                if text in terms:
                    return entries, i
                if text in OPEN:
                    entries.append(i)
                    i = self.match[i] + 1
                    continue
            entries.append(i)
            i += 1
        raise Refuse

    def find_semicolon(self, i, limit):
        while i < limit:
            if self.opener(i):
                i = self.match[i] + 1
            elif self.k[i] == "p" and self.t[i] == ";":
                return i
            else:
                i += 1
        raise Refuse

    def is_type_start(self, k):
        t, kind = self.t, self.k
        if kind[k] != "i":
            return False
        word = t[k]
        if word in ("class", "struct", "interface", "enum"):
            return kind[k + 1] == "i"
        if word == "record":
            n = k + 1 + (t[k + 1] in ("class", "struct") and kind[k + 1] == "i")
            return kind[n] == "i"
        if word == "delegate":
            return t[k + 1] != "{" and t[k + 1] != ""
        return False

    def types_of(self, group):
        """Normalized parameter types of a ( ) or [ ] group."""
        t, k, end = self.t, self.k, self.match[group]
        parts, angle, j = [[]], 0, group + 1
        while j < end:
            text = t[j]
            if k[j] == "p":
                if text in OPEN:
                    parts[-1].append(j)
                    j = self.match[j] + 1
                    continue
                if text == "<":
                    angle += 1
                elif text == ">":
                    angle = max(0, angle - 1)
                elif text == "," and angle == 0:
                    parts.append([])
                    j += 1
                    continue
                elif text == "=" and angle == 0:
                    j += 1
                    while j < end and t[j] != ",":
                        j = self.match[j] + 1 if self.opener(j) else j + 1
                    continue
            parts[-1].append(j)
            j += 1
        types = []
        for items in parts:
            if not items:
                continue
            while items and t[items[0]] == "[":
                items = items[1:]
            while items and t[items[0]] in ("this", "params", "scoped"):
                items = items[1:]
            if len(items) > 1 and k[items[-1]] == "i":
                items = items[:-1]
            texts = []
            for x in items:
                if self.opener(x):
                    texts.extend(t[x : self.match[x] + 1])
                else:
                    texts.append(t[x])
            types.append(join(texts))
        return ",".join(types)

    def name_at(self, core, e):
        """(name, generic arity, entry position of the name's start) for the name ending at core[e]."""
        t, k = self.t, self.k

        def back(q):  # position of the < matching the > at core[q]
            depth, commas = 0, 0
            while q >= 0:
                text = t[core[q]]
                if text == ">":
                    depth += 1
                elif text == "<":
                    depth -= 1
                    if depth == 0:
                        return q, commas
                elif text == "," and depth == 1:
                    commas += 1
                q -= 1
            raise Refuse

        arity, pos = 0, e
        if t[core[e]] == ">":
            q, commas = back(e)
            arity, pos = commas + 1, q - 1
        if pos < 0 or k[core[pos]] != "i":
            raise Refuse
        r = pos
        while r >= 2 and t[core[r - 1]] == ".":
            p = r - 2
            if t[core[p]] == ">":
                p = back(p)[0] - 1
            if p < 0 or k[core[p]] != "i":
                raise Refuse
            r = p
        return join(t[core[r] : core[pos] + 1]), arity, r

    def declarators(self, a, b):
        """Declared names of a field or event over tokens a..b."""
        t, k = self.t, self.k
        names, angle, j = [], 0, a
        while j <= b:
            if self.opener(j):
                j = self.match[j] + 1
                continue
            text = t[j]
            if text == "<":
                angle += 1
            elif text == ">":
                angle = max(0, angle - 1)
            elif (
                k[j] == "i"
                and angle == 0
                and t[j + 1] in ("=", ",", ";")
                and (not names or t[j - 1] == ",")
            ):
                names.append(text)
                angle = 0
            j += 1
        if not names:
            raise Refuse
        return names

    # -- declarations

    def run(self):
        nodes = self.scope(0, self.count, "file", None)
        return nodes

    def scope(self, i, limit, kind, tname):
        nodes = []
        while i < limit:
            node, i = self.item(i, limit, kind, tname)
            if node is not None:
                nodes.append(node)
        return nodes

    def directive(self, i, limit):
        """Index of the ; ending a using directive or extern alias at i, or None."""
        t, k = self.t, self.k
        if t[i] == "extern" and t[i + 1] == "alias":
            return self.find_semicolon(i, limit)
        j = i + (t[i] == "global" and t[i + 1] == "using")
        if t[j] != "using" or k[j] != "i":
            return None
        j += 1
        if t[j] == "static" and k[j + 1] == "i":
            j += 1
        if k[j] != "i":
            return None
        if t[j + 1] == "=":
            return self.find_semicolon(j, limit)
        while t[j + 1] in (".", "::") and k[j + 2] == "i":
            j += 2
        return j + 1 if t[j + 1] == ";" else None

    def item(self, i, limit, kind, tname):
        t, k = self.t, self.k
        if kind in ("file", "ns"):
            end = self.directive(i, limit)
            if end is not None:
                return self.node(join(t[i:end]), i, end, []), end + 1
            if kind == "file" and t[i] == "[" and t[i + 1] in ("assembly", "module") and t[i + 2] == ":":
                end = self.match[i]
                return self.node("attribute " + join(t[i : end + 1]), i, end, []), end + 1
        j = self.skip_attrs(i)
        m = self.skip_mods(j)
        if kind in ("file", "ns") and t[m] == "namespace" and k[m + 1] == "i":
            return self.namespace(i, m, limit, kind)
        if self.is_type_start(m):
            try:
                return self.type_decl(i, m, limit, kind)
            except NotDeclaration:
                pass
        elif kind == "type":
            return self.member(i, limit, tname)
        elif kind == "ns":
            raise Refuse
        if self.file_scoped:
            raise Refuse
        return None, self.statement(i, limit)

    def statement(self, i, limit):
        """Index after a top-level statement starting at i (it stays in the gap text)."""
        t, k = self.t, self.k
        control = t[i] in CONTROL and k[i] == "i"
        assigned, j = False, i
        while j < limit:
            text = t[j]
            if k[j] == "p":
                if text == "{":
                    end, before = self.match[j], t[j - 1]
                    if j == i:
                        return end + 1
                    nxt = t[end + 1]
                    j = end + 1
                    if nxt in (";", ".", ",", ")", "]", "}", "?", ":"):
                        continue
                    if control:
                        if nxt in ("else", "catch", "finally", "while"):
                            continue
                        return j
                    if before == ")" and not assigned:
                        return j  # a local function body
                    continue
                if text in ("(", "["):
                    j = self.match[j] + 1
                    continue
                if text == ";":
                    return j + 1
                if text in ("=", "=>"):
                    assigned = True
            j += 1
        raise Refuse

    def namespace(self, i, m, limit, kind):
        t, k = self.t, self.k
        entries, term = self.head(m + 1, limit, ("{", ";"))
        if not entries or not all(k[x] == "i" or t[x] == "." for x in entries) or k[entries[0]] != "i":
            raise Refuse
        key = "namespace " + join(t[entries[0] : entries[-1] + 1])
        if t[term] == ";":
            if kind != "file":
                raise Refuse
            self.file_scoped = True
            return self.node(key, i, term, []), term + 1
        end = self.match[term]
        children = self.scope(term + 1, end, "ns", None)
        if t[end + 1] == ";" and end + 1 < limit:
            end += 1
        return self.node(key, i, end, children), end + 1

    def type_decl(self, i, k0, limit, kind):
        t, k = self.t, self.k
        word, j = t[k0], k0 + 1
        if word == "record" and t[j] in ("class", "struct"):
            j += 1
        if word == "delegate":
            return self.delegate(i, k0, limit, kind)
        label = "record" if word == "record" else word
        name = t[j]
        j += 1
        arity = 0
        if t[j] == "<":
            depth, commas = 1, 0
            j += 1
            while depth:
                if j >= limit or k[j] != "p" and k[j] != "i":
                    raise Refuse
                if t[j] == "<":
                    depth += 1
                elif t[j] == ">":
                    depth -= 1
                elif t[j] == "," and depth == 1:
                    commas += 1
                j += 1
            arity = commas + 1
        _, term = self.head(j, limit, ("{", ";"))
        key = f"{label} {name}" + (f"`{arity}" if arity else "")
        if t[term] == ";":
            if word != "record":
                raise Refuse
            return self.node(key, i, term, []), term + 1
        end = self.match[term]
        if word == "enum":
            children = self.enum_members(term + 1, end)
        else:
            children = self.scope(term + 1, end, "type", name)
        if t[end + 1] == ";" and end + 1 < limit:
            end += 1
        return self.node(key, i, end, children), end + 1

    def delegate(self, i, k0, limit, kind):
        t = self.t
        entries, term = self.head(k0 + 1, limit, ("{", ";"))
        if t[term] == "{":
            if kind == "file":
                raise NotDeclaration
            raise Refuse
        core = self.cut(entries)
        if len(core) < 3 or t[core[-1]] != "(":
            raise Refuse
        name, arity, r = self.name_at(core, len(core) - 2)
        if r < 1:
            raise Refuse
        key = f"delegate {name}" + (f"`{arity}" if arity else "")
        return self.node(key, i, term, []), term + 1

    def cut(self, entries):
        for n, x in enumerate(entries):
            if self.t[x] in ("where", ":") and self.k[x] in ("i", "p"):
                return entries[:n]
        return entries

    def enum_members(self, a, b):
        t, k = self.t, self.k
        nodes, j = [], a
        while j < b:
            first = j
            while j < b and not (t[j] == "," and k[j] == "p"):
                j = self.match[j] + 1 if self.opener(j) else j + 1
            last = j - 1
            if last < first:
                raise Refuse
            at = self.skip_attrs(first)
            if at > last or k[at] != "i" or (at < last and t[at + 1] != "="):
                raise Refuse
            nodes.append(self.node("member " + t[at], first, last, []))
            j += 1
        return nodes

    def member(self, i, limit, tname):
        t, k = self.t, self.k
        j = self.skip_attrs(i)
        m = self.skip_mods(j)
        if self.is_type_start(m):
            return self.type_decl(i, m, limit, "type")
        entries, term = self.head(j, limit, ("{", ";", "=>", "="))
        pos = 0
        while pos < len(entries) and k[entries[pos]] == "i" and t[entries[pos]] in MODIFIERS:
            pos += 1
        rest = entries[pos:]
        if not rest:
            raise Refuse
        words = [t[x] if k[x] == "i" else "" for x in rest]
        kind = "method"
        if t[rest[0]] == "~" and k[rest[0]] == "p":
            kind = "finalizer"
        elif "operator" in words:
            kind = "operator"
        elif "event" in words:
            kind = "event"
        core = entries if kind in ("operator", "event") else self.cut(entries)
        if not core:
            raise Refuse
        last = core[-1]
        if kind == "method" and t[last] == "[" and k[last] == "p" and len(core) >= 2 and t[core[-2]] == "this":
            kind = "indexer"
        elif kind == "method" and not (t[last] == "(" and k[last] == "p"):
            kind = "property" if t[term] in ("{", "=>") else "field"

        if kind == "finalizer":
            if len(rest) != 3 or t[rest[2]] != "(" or k[rest[1]] != "i":
                raise Refuse
            key = f"finalizer {t[rest[1]]}"
        elif kind == "operator":
            op = words.index("operator")
            first_sym, last_sym = pos + op + 1, len(core) - 2
            if t[last] != "(" or last_sym < first_sym:
                raise Refuse
            symbol = join(t[core[first_sym] : core[last_sym] + 1])
            conv = "implicit " if "implicit" in words[:op] else "explicit " if "explicit" in words[:op] else ""
            key = f"operator {conv}{symbol}({self.types_of(last)})"
        elif kind == "indexer":
            qualifier = ""
            if len(core) >= 4 and t[core[-3]] == ".":
                r = self.name_at(core, len(core) - 4)[2]
                qualifier = join(t[core[r] : core[-3]]) + "."
            key = "indexer" + (" " + qualifier if qualifier else "") + f"({self.types_of(last)})"
        elif kind == "event":
            if t[term] == "{":
                key = "event " + self.name_at(core, len(core) - 1)[0]
            else:
                end = self.find_semicolon(term, limit) if t[term] != ";" else term
                key = "event " + ",".join(self.declarators(j, end))
        elif kind == "method":
            name, arity, r = self.name_at(core, len(core) - 2)
            before = [x for x in core[:r] if not (k[x] == "i" and t[x] in MODIFIERS)]
            if not before:
                if name != tname or arity:
                    raise Refuse
                static = any(t[x] == "static" for x in core[:r])
                key = ("static " if static else "") + f"ctor({self.types_of(last)})"
            else:
                key = f"method {name}" + (f"`{arity}" if arity else "") + f"({self.types_of(last)})"
        elif kind == "property":
            name, _, r = self.name_at(core, len(core) - 1)
            if not [x for x in core[:r] if not (k[x] == "i" and t[x] in MODIFIERS)]:
                raise Refuse
            key = f"property {name}"
        else:  # field
            end = term if t[term] == ";" else self.find_semicolon(term + 1, limit)
            names = self.declarators(j, end)
            if not [x for x in core if not (k[x] == "i" and t[x] in MODIFIERS)][1:]:
                raise Refuse
            key = "field " + ",".join(names)

        if t[term] == ";":
            end = term
        elif t[term] == "{":
            end = self.match[term]
            if kind == "property" and t[end + 1] == "=":
                end = self.find_semicolon(end + 2, limit)
        else:
            end = self.find_semicolon(term + 1, limit)
        return self.node(key, i, end, []), end + 1


def outline(blob, suffix):
    """Top-level units of a .cs file as (key, start, end, children), or None."""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    base = 0
    if text.startswith("﻿"):
        text, base = text[1:], 3
    if "\x00" in text:
        return None
    try:
        return Parser(text, base).run()
    except (Refuse, NotDeclaration, IndexError):
        return None
