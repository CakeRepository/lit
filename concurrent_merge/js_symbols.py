"""Per-function digests of JavaScript and TypeScript files, in pure Python.

This replaces the Babel script lit used to call. It reuses lit's own tokenizer
and bracket matcher (readers/javascript.py), so there is no Bun, Node or
@babel/parser involved.

The digests decide whether a test may be skipped, so every approximation errs
toward "changed": a function is cut out of its parent's code only where its
exact extent is certain, and anything not recognised stays in the parent. The
rules, from the safest end:

- The digest of a symbol is the text of its own code with comments removed.
  Whitespace and newlines are kept (a newline decides where a statement ends),
  except that a comment alone on its line takes the whole line with it. The
  insides of strings, template literals, regular expressions and JSX elements
  are never touched.
- Cut out of the parent: function declarations and expressions, class and
  object methods, and arrow functions, each only when it has a `{}` body whose
  start and end are certain. A named one (a function declaration, a method,
  `const f = <function>`, `export default <function>`) is cut entirely, with
  the code in front of the function (`export const f =`) and the separator
  after it; it becomes the symbol "parent/name". An anonymous one is replaced
  by the marker "ƒ" and becomes "parent/@0", "parent/@1", ... by position.
  Arrow functions whose body is an expression stay in their parent's code.
- Everything else, type-only declarations included, stays in the code of the
  file symbol "", which spans the whole file. Top-level static imports are the
  one exception: they are recorded apart in the `imports` table.
- A file the tokenizer or the import reader is not sure about gives None, and
  lit then falls back to the whole-file rule.
"""

import hashlib
import json
import re
from bisect import bisect_left

from .readers.javascript import (
    CLOSERS, IDENT, OPENERS, TYPE_BRACE_AFTER, Parser, Scanner, Unsure,
)

SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# Files whose text may hold JSX: everything but plain TypeScript, as Babel was told.
NO_JSX = (".ts", ".mts", ".cts")

RESERVED = frozenset(
    "break case catch class const continue debugger default delete do else enum export extends "
    "false finally for function if import in instanceof new null return super switch this throw "
    "true try typeof var void while with yield let static await async".split()
)
# Words that make a run of tokens after `as` more than a type.
NOT_TYPE = frozenset("instanceof in new delete void await yield function class async".split())
TYPE_PUNCT = frozenset((".", "|", "&", ",", "<", ">", ">>", ">>>", "[", "]", "?", ":"))
BEFORE_OBJECT = frozenset((
    "=", "(", ",", ":", "[", "?", "||", "&&", "??", "...", "||=", "&&=", "??=",
))
BEFORE_OBJECT_WORDS = frozenset(("return", "default", "yield", "await", "typeof"))
NEW_INERT = frozenset(("Map", "Set", "WeakMap", "WeakSet"))
MARKER = "ƒ"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


COMMENT = re.compile(r"//[^\n\u2028\u2029]*|/\*[\s\S]*?\*/")


def clean_gap(gap, first, last):
    """The whitespace and comments between two tokens with the comments removed.

    A comment alone on its line takes the line with it (the start of the file
    and its end count as line breaks); a block comment that spans lines still
    counts as a line break.
    """
    if first and gap.startswith("#!"):
        line, newline, rest = gap.partition("\n")
        return line + newline + clean_gap(rest, newline == "\n", last)
    if "/" not in gap:
        return gap
    marked = COMMENT.sub(lambda m: "\x02" if "\n" in m.group() else "\x01", gap)
    lines = marked.split("\n")
    kept = []
    for index, line in enumerate(lines):
        inner = (index > 0 or first) and (index < len(lines) - 1 or last)
        if inner and ("\x01" in line or "\x02" in line):
            if not line.replace("\x01", "").replace("\x02", "").strip():
                continue
        kept.append(line)
    out = "\n".join(kept).replace("\x01", "").replace("\x02", "\n")
    return out if out or not gap or first or last else " "  # a/**/b is not ab


class Cand:
    """A function the digests cut out: tokens lo..hi are its outer extent."""

    __slots__ = ("lo", "hi", "fs", "fe", "name", "kids", "full", "seq")

    def __init__(self, lo, hi, fs, fe, name=None):
        self.lo, self.hi, self.fs, self.fe, self.name = lo, hi, fs, fe, name
        self.kids, self.full, self.seq = [], None, 0


class Analysis:
    def __init__(self, text, tokens, match):
        self.text, self.t, self.m, self.n = text, tokens, match, len(tokens)
        self.p = Parser(text, tokens, match)
        self.gaps = {}
        self.newlines = [i for i, c in enumerate(text) if c == "\n"]

    # ----- token helpers --------------------------------------------------

    def v(self, k):
        return self.p.v(k)

    def kind(self, k):
        return self.p.kind(k)

    def plain_id(self, k):
        return self.kind(k) == "id" and self.v(k) not in RESERVED

    def angle(self, k, hi):
        """Index after the type arguments opening at k, or None when unsure."""
        end = self.p.skip_angle(k, hi)
        return None if end == k + 1 else end

    def statement_start(self, i):
        """True when token i begins a statement."""
        if i == 0:
            return True
        if self.v(i - 1) in (";", "{", "}"):
            return True
        return bool(self.t[i][3]) and not self.p.is_open(self.t[i - 1]) and not self.p.is_continuation(self.t[i], False)

    def items(self, lo, hi):
        """(start, end) of the comma separated items between lo and hi, groups skipped."""
        out, start, i = [], lo, lo
        while i < hi:
            if self.kind(i) == "p" and self.v(i) in OPENERS:
                i = self.m[i] + 1
                continue
            if self.v(i) == ",":
                out.append((start, i))
                start = i + 1
            i += 1
        if start < hi:
            out.append((start, hi))
        return out

    def line_of(self, offset):
        return bisect_left(self.newlines, offset) + 1

    def gap(self, i):
        if i not in self.gaps:
            start = self.t[i - 1][2] if i > 0 else 0
            end = self.t[i][1] if i < self.n else len(self.text)
            self.gaps[i] = clean_gap(self.text[start:end], i == 0, i == self.n)
        return self.gaps[i]

    # ----- function extents -----------------------------------------------

    def after_params(self, j, hi):
        """From j (at '<' or '(' after a function's name): ("{", open brace) for a body,
        (";", index after the ;) for a signature without one, None when unsure."""
        if self.v(j) == "<":
            j = self.angle(j, hi)
            if j is None:
                return None
        if self.v(j) != "(":
            return None
        pe = self.m[j] + 1
        pos, why = self.p.scan(pe, hi, angle=True, brace=True)
        if why == "{":
            if pos > pe:
                before = pos - 1
                word, kind = self.v(before), self.kind(before)
                ends_type = kind in ("str", "num", "tpl") or (
                    kind == "id" and word not in ("is", "extends", "keyof", "infer", "asserts", "readonly")
                ) or (kind == "p" and word in (")", "]", "}", ">", ">>", ">>>"))
                if not ends_type:
                    return None
            return "{", pos
        if why == ";":
            return ";", pos
        return None

    def arrow_from(self, i):
        """(index of the =>, indexes that start the same arrow) for an arrow function with a {}
        body that starts at token i, else None."""
        hi, v, claims = self.n, self.v, set()
        j = i
        if v(j) == "async" and i + 1 < hi and not self.t[i + 1][3]:
            nxt = v(j + 1)
            if nxt in ("(", "<") or (self.plain_id(j + 1) and v(j + 2) == "=>"):
                j += 1
                claims.add(j)
        if v(j) == "<":
            j = self.angle(j, hi)
            if j is None or v(j) != "(":
                return None
            claims.add(j)
        if v(j) == "(":
            pe = self.m[j] + 1
            if v(pe) == "=>":
                arrow = pe
            elif v(pe) == ":":
                # A return type makes `a ? (b) : (c) => {}` ambiguous, so only where the "(" starts an
                # operand outright: after = ( , [ ; || && ?? return default yield await, or at the start.
                if i > 0 and not (
                    (self.kind(i - 1) == "p" and v(i - 1) in ("=", "(", ",", "[", ";", "||", "&&", "??"))
                    or (self.kind(i - 1) == "id" and v(i - 1) in ("return", "default", "yield", "await"))
                ):
                    return None
                arrow, k = None, pe + 1
                while k < hi:
                    word = v(k)
                    claims.add(k)  # the return type is not another arrow's start
                    if self.kind(k) == "p":
                        if word in OPENERS:
                            k = self.m[k] + 1
                            continue
                        if word in CLOSERS or word == ";":
                            return None
                        if word == "=>":
                            arrow = k
                            break
                        if word == "<":
                            k = self.p.skip_angle(k, hi)
                            continue
                    k += 1
                if arrow is None:
                    return None
            else:
                return None
        elif self.plain_id(j) and v(j + 1) == "=>" and not (v(j - 1) == ":" and v(j - 2) == ")"):
            # (not the last word of a return type: `(x): T => {` is read from its "(" or not at all)
            arrow = j + 1
        else:
            return None
        if v(arrow + 1) == "{" and (arrow + 1) in self.m:
            return arrow, claims
        return None

    # ----- candidates -----------------------------------------------------

    def candidates(self):
        found, by_lo, claimed = [], {}, set()
        for k in range(self.n):
            kind, val = self.t[k][0], self.t[k][4]
            try:
                if kind == "id" and val in ("function", "class") and self.v(k - 1) not in (".", "?."):
                    if val == "function":
                        cand = self.function_at(k)
                        if cand:
                            found.append(cand)
                            if cand.name is None:
                                by_lo[cand.lo] = cand
                    else:
                        found.extend(self.class_members(k))
                if k not in claimed and (
                    (kind == "id" and (val == "async" or self.v(k + 1) == "=>")) or (kind == "p" and val in ("(", "<"))
                ):
                    arrow = self.arrow_from(k)
                    if arrow is not None:
                        fe = self.m[arrow[0] + 1] + 1
                        cand = Cand(k, fe, k, fe)
                        found.append(cand)
                        by_lo[k] = cand
                        claimed.update(arrow[1])
                if kind == "p" and val == "{":
                    found.extend(self.object_members(k))
            except (Unsure, IndexError, KeyError):
                continue
        self.name_variables(by_lo)
        return found

    def function_at(self, k):
        s = k
        if self.v(k - 1) == "async" and not self.t[k][3] and self.v(k - 2) not in (".", "?."):
            s = k - 1
        j = k + 1
        if self.v(j) == "*":
            j += 1
        name = None
        if self.kind(j) == "id":
            name = self.v(j)
            j += 1
        res = self.after_params(j, self.n)
        if not res or res[0] != "{":
            return None
        fe = self.m[res[1]] + 1
        st, default = s, False
        if self.v(st - 1) == "default" and self.v(st - 2) == "export":
            st, default = st - 2, True
        elif self.v(st - 1) == "export":
            st -= 1
        if (name or default) and self.statement_start(st):
            return Cand(st, fe, s, fe, "default" if default else name)
        return Cand(s, fe, s, fe)

    def class_members(self, k):
        j = k + 1
        if self.kind(j) == "id" and self.v(j) not in ("extends", "implements"):
            j += 1
        elif self.kind(j) == "p" and self.v(j) not in ("{", "<"):
            return []
        if self.v(j) == "<":
            j = self.angle(j, self.n)
            if j is None:
                return []
        pos, why = self.p.scan(j, self.n, angle=True, brace=True)
        if why != "{":
            return []
        out = []
        for u in self.p.class_body(pos, self.m[pos]):
            key = re.sub(r" #\d+$", "", u.key)
            static = key.startswith("static ")
            if static:
                key = key[7:]
            if u.sig or self.v(u.end - 1) != "}":
                continue
            if key == "constructor" and not static:
                name = key
            else:
                word, _, base = key.partition(" ")
                if word not in ("method", "get", "set") or not base or base.startswith("["):
                    continue
                name = (word + " " if word != "method" else "") + ("static " if static else "") + base
            out.append(Cand(u.start, u.end, u.start, u.end, name))
        return out

    def method_at(self, s, e):
        """Name of the object method that is exactly tokens s..e, else None."""
        j, prefix = s, ""
        while self.kind(j) == "id" and self.v(j) in ("async", "get", "set") and j < e - 1:
            nxt = j + 1
            if not (self.kind(nxt) in ("id", "str", "num") or self.v(nxt) == "*"):
                break
            if self.v(j) in ("get", "set"):
                prefix = self.v(j) + " "
            j += 1
        if self.v(j) == "*":
            j += 1
        if self.kind(j) not in ("id", "str", "num") or j >= e:
            return None
        name = self.text[self.t[j][1]:self.t[j][2]]
        j += 1
        if self.v(j) not in ("(", "<"):
            return None
        res = self.after_params(j, e)
        if not res or res[0] != "{" or self.m[res[1]] + 1 != e or self.t[res[1]][3]:
            return None
        return prefix + name

    def object_members(self, b):
        before = self.v(b - 1) if b > 0 else None
        if not (
            (self.kind(b - 1) == "p" and before in BEFORE_OBJECT)
            or (self.kind(b - 1) == "id" and before in BEFORE_OBJECT_WORDS)
        ):
            return []
        close, out = self.m[b], []
        for s, e in self.items(b + 1, close):
            name = self.method_at(s, e)
            if name is not None:
                out.append(Cand(s, e + (1 if e < close and self.v(e) == "," else 0), s, e, name))
        return out

    def name_variables(self, by_lo):
        """Give `const f = <function>` and `export default <function>` the name of the declaration."""
        for k in range(self.n):
            if self.kind(k) != "id" or self.v(k - 1) in (".", "?."):
                continue
            try:
                if self.v(k) == "const" and self.plain_id(k + 1):
                    st = k - 1 if self.v(k - 1) == "export" else k
                    if not self.statement_start(st):
                        continue
                    j = k + 2
                    if self.v(j) == ":":
                        j, why = self.p.scan(j + 1, self.n, eq=True, angle=True)
                        if why != "=":
                            continue
                    if self.v(j) != "=":
                        continue
                    name = self.v(k + 1)
                elif self.v(k) == "default" and self.v(k - 1) == "export":
                    st, j, name = k - 1, k, "default"
                    if not self.statement_start(st):
                        continue
                else:
                    continue
            except (Unsure, IndexError):
                continue
            cand = by_lo.get(j + 1)
            if cand is None or cand.name is not None or cand.lo != cand.fs:
                continue
            end = cand.hi
            if end < self.n:
                if self.v(end) == ";":
                    end += 1
                elif self.v(end) == "}":
                    pass
                elif not (self.t[end][3] and not self.p.is_continuation(self.t[end], False)):
                    continue
            cand.lo, cand.hi, cand.name = st, end, name

    # ----- the tree and the digests ----------------------------------------

    def tree(self, cands):
        for seq, cand in enumerate(cands):
            cand.seq = seq
        cands.sort(key=lambda c: (c.lo, -c.hi, c.seq))
        roots, stack = [], []
        for cand in cands:
            while stack and stack[-1].hi <= cand.lo:
                stack.pop()
            if stack and (cand.hi > stack[-1].hi or (cand.lo, cand.hi) == (stack[-1].lo, stack[-1].hi)):
                continue  # crosses or repeats an outer extent: leave it in the code around it
            (stack[-1].kids if stack else roots).append(cand)
            stack.append(cand)
        return roots

    def render(self, lo, hi, kids, root=False):
        """(code, identifiers) of tokens lo..hi with each kid cut out."""
        text, t = self.text, self.t
        out, ids = [], set()
        state = [False]  # the whitespace at the end of out holds a line break

        def emit(piece):
            if not piece:
                return
            out.append(piece)
            stripped = piece.rstrip(" \t\n\r\f\v ﻿")
            state[0] = ("\n" in piece[len(stripped):]) if stripped else (state[0] or "\n" in piece)

        def settle(gap):
            if "\n" in gap and not state[0]:
                emit("\n")

        i, ki, skip = lo, 0, False
        while i < hi:
            kid = kids[ki] if ki < len(kids) else None
            gap = self.gap(i) if (i > lo or root) else ""
            if skip:
                settle(gap)
                skip = False
            else:
                emit(gap)
            if kid is not None and kid.lo == i:
                ki += 1
                if kid.name is None:
                    emit(MARKER)
                else:
                    skip = True
                i = kid.hi
                continue
            kind, s, e, _, val = t[i]
            emit(text[s:e])
            if kind == "id":
                ids.add(val)
            elif kind in ("tpl", "jsx"):
                ids.update(IDENT.findall(text[s:e]))
            i += 1
        if root and not skip:
            emit(self.gap(self.n))
        return "".join(out), ids

    def symbols(self, roots, cuts):
        symbols = {}

        def add(name, lo, hi, kids, fs, fe, root=False):
            code, ids = self.render(lo, hi, kids, root)
            if root:
                start, end = 1, len(self.newlines) + 1
            else:
                start = self.line_of(self.t[fs][1])
                end = self.line_of(self.t[fe - 1][2] - 1)
            prior = symbols.get(name)
            if prior:
                code = prior["code"] + "\n\0\n" + code
                start, end, ids = min(prior["start"], start), max(prior["end"], end), prior["ids"] | ids
            symbols[name] = {"code": code, "start": start, "end": end, "ids": ids}

        def walk(kids, prefix):
            anonymous = 0
            for cand in kids:
                if cand.name is None:
                    name = f"@{anonymous}"
                    anonymous += 1
                else:
                    name = cand.name
                cand.full = prefix + "/" + name if prefix else name
                add(cand.full, cand.lo, cand.hi, cand.kids, cand.fs, cand.fe)
                walk(cand.kids, cand.full)

        top = sorted(list(roots) + cuts, key=lambda c: c.lo)
        add("", 0, self.n, top, 0, self.n, root=True)
        walk(roots, "")
        # The digest the file symbol had when imports were part of it.
        legacy, _ = self.render(0, self.n, roots, True)
        return symbols, digest(legacy)


class Import:
    """A top-level import statement cut out of the file symbol."""

    __slots__ = ("lo", "hi", "name", "kids")

    def __init__(self, lo, hi):
        self.lo, self.hi, self.name, self.kids = lo, hi, "import", []


class Module(Analysis):
    """What loading the file does: imports, exports, dependencies and whether it only defines names."""

    def read(self):
        self.imports, self.cuts, self.known = {}, [], {}
        self.deps, self.exports, self.star = set(), set(), False
        self.declared, self.exported_locals = set(), []
        k = 0
        while k < self.n:
            word = self.v(k) if self.kind(k) == "id" else None
            after_dot = self.v(k - 1) in (".", "?.")
            if word == "import" and not after_dot and self.v(k + 1) not in ("(", "."):
                k = self.read_import(k)
            elif word == "export" and not after_dot:
                k = self.read_export(k)
            else:
                self.note_declaration(k)
                k = self.m[k] + 1 if self.kind(k) == "p" and self.v(k) in OPENERS else k + 1

    def boundary(self, k):
        if not (k == 0 or self.v(k - 1) in (";", "}") or self.t[k][3]):
            raise Unsure("statement boundary")

    def note_declaration(self, k):
        word = self.v(k) if self.kind(k) == "id" else None
        if word not in ("function", "class", "const", "let", "var", "enum"):
            return
        before = self.v(k - 1) if k else None
        if not (k == 0 or before in (";", "}", "export", "default", "async", "abstract") or self.t[k][3]):
            return
        if before == "declare" or (word == "enum" and before == "const"):
            return
        j = k + 1
        if self.v(j) == "*":
            j += 1
        if self.plain_id(j):
            self.declared.add(self.v(j))

    def finish(self, k):
        """End of a statement whose last token before the optional ';' is k - 1."""
        if self.v(k) == ";":
            return k + 1
        if k >= self.n or self.t[k][3]:
            return k
        raise Unsure("statement end")

    def attributes(self, j):
        if self.v(j) in ("with", "assert") and self.v(j + 1) == "{":
            return self.m[j + 1] + 1
        return j

    def specifiers(self, open_brace):
        """[(imported or local name, bound or exported name, type only)] of a {...} list."""
        out = []
        for s, e in self.items(open_brace + 1, self.m[open_brace]):
            if any(self.kind(i) not in ("id", "str") for i in range(s, e)):
                raise Unsure("specifier")
            words = [self.v(i) if self.kind(i) == "id" else self.text[self.t[i][1] + 1:self.t[i][2] - 1] for i in range(s, e)]
            n = e - s
            if self.v(s) == "type" and self.kind(s) == "id" and n > 1:
                if n == 2 and words[1] != "as":
                    out.append((words[1], words[1], True))
                elif n == 4 and words[2] == "as" and words[1] != "as":
                    out.append((words[1], words[3], True))
                else:
                    raise Unsure("type specifier")
            elif n == 1:
                out.append((words[0], words[0], False))
            elif n == 3 and words[1] == "as" and self.kind(s + 1) == "id":
                out.append((words[0], words[2], False))
            else:
                raise Unsure("specifier")
        return out

    def read_import(self, k):
        self.boundary(k)
        j, type_only, bindings = k + 1, False, {}
        if self.kind(j) == "str":
            source = j
            j += 1
        else:
            if self.v(j) == "type" and self.kind(j) == "id":
                after, after_kind = self.v(j + 1), self.kind(j + 1)
                if after in ("{", "*") or (after_kind == "id" and after != "from"):
                    type_only, j = True, j + 1
                elif after == "from":
                    if self.kind(j + 2) != "str":
                        if self.v(j + 2) == "from" and self.kind(j + 3) == "str":
                            type_only, j = True, j + 1
                        else:
                            raise Unsure("import type")
                else:
                    raise Unsure("import type")
            if self.kind(j) == "id" and self.v(j) not in RESERVED and self.v(j + 1) in (",", "from"):
                bindings[self.v(j)] = "default"
                j += 1
                if self.v(j) == ",":
                    j += 1
            if self.v(j) == "*":
                if self.v(j + 1) != "as" or self.kind(j + 2) != "id":
                    raise Unsure("namespace import")
                bindings[self.v(j + 2)] = "*"
                j += 3
            elif self.v(j) == "{":
                for imported, local, erased in self.specifiers(j):
                    if not erased:
                        bindings[local] = imported
                j = self.m[j] + 1
            if self.v(j) != "from" or self.kind(j + 1) != "str":
                raise Unsure("import")
            source = j + 1
            j += 2
        j = self.attributes(j)
        end = self.finish(j)
        self.cuts.append(Import(k, end))
        self.known[k] = end
        if not type_only:
            name = self.text[self.t[source][1] + 1:self.t[source][2] - 1]
            entry = self.imports.setdefault(name, {})
            entry.update(bindings)
            self.deps.add(name)
        return end

    def read_export(self, k):
        self.boundary(k)
        j = k + 1
        word = self.v(j) if self.kind(j) == "id" else None
        if word == "import":
            raise Unsure("export import")
        if word == "default":
            if self.v(j + 1) != "interface":
                self.exports.add("default")
            return k + 1
        if self.v(j) == "=":
            self.star = True
            return k + 1
        type_only = False
        if word == "type" and self.v(j + 1) in ("{", "*"):
            type_only, j = True, j + 1
        if self.v(j) == "*":
            j += 1
            exported = None
            if self.v(j) == "as":
                if self.kind(j + 1) not in ("id", "str"):
                    raise Unsure("export *")
                exported = self.text[self.t[j + 1][1]:self.t[j + 1][2]].strip("\"'")
                j += 2
            if self.v(j) != "from" or self.kind(j + 1) != "str":
                raise Unsure("export *")
            source = self.text[self.t[j + 1][1] + 1:self.t[j + 1][2] - 1]
            end = self.finish(self.attributes(j + 2))
            if not type_only:
                self.deps.add(source)
                if exported is not None:
                    self.exports.add(exported)
                else:
                    self.star = True
            self.known[k] = end
            return end
        if self.v(j) == "{":
            specs = self.specifiers(j)
            j = self.m[j] + 1
            source = None
            if self.v(j) == "from":
                if self.kind(j + 1) != "str":
                    raise Unsure("export from")
                source = self.text[self.t[j + 1][1] + 1:self.t[j + 1][2] - 1]
                j += 2
            end = self.finish(self.attributes(j))
            if not type_only:
                if source is not None:
                    self.deps.add(source)
                else:  # claim only names that are certainly values declared here
                    self.exported_locals.extend((local, name) for local, name, erased in specs if not erased)
            self.known[k] = end
            return end
        # export <declaration>
        if word == "async" and self.v(j + 1) == "function":
            j += 1
            word = "function"
        if word == "abstract" and self.v(j + 1) == "class":
            j += 1
            word = "class"
        if word in ("function", "class", "enum", "const", "let", "var"):
            j += 1
            if word == "function" and self.v(j) == "*":
                j += 1
            if self.plain_id(j):
                self.exports.add(self.v(j))
        return k + 1

    # ----- purity -----------------------------------------------------------

    def pure(self):
        k, prologue = 0, True
        while k < self.n:
            if self.v(k) == ";":
                k += 1
                continue
            if prologue and self.kind(k) == "str":
                e = k + 1
                if self.v(e) == ";":
                    e += 1
                elif e < self.n and (not self.t[e][3] or self.p.is_continuation(self.t[e], False)):
                    return False
                k = e
                continue
            prologue = False
            try:
                end = self.pure_statement(k)
            except (Unsure, IndexError, KeyError):
                return False
            if end is None or end <= k:
                return False
            k = end
        return True

    def pure_statement(self, k):
        if k in self.known:
            return self.known[k]
        j = k
        default = declare = False
        if self.v(j) == "export" and self.kind(j) == "id":
            j += 1
            if self.v(j) == "default":
                default = True
                j += 1
        if self.v(j) == "declare" and self.kind(j) == "id" and self.kind(j + 1) == "id" and not self.t[j + 1][3]:
            declare = True
            j += 1
        word = self.v(j) if self.kind(j) == "id" else None
        nxt = self.v(j + 1)
        if word == "function" or (word == "async" and nxt == "function" and not self.t[j + 1][3]):
            jj = j + (word == "async") + 1
            if self.v(jj) == "*":
                jj += 1
            if self.kind(jj) == "id":
                jj += 1
            res = self.after_params(jj, self.n)
            if res is None:
                return None
            return self.m[res[1]] + 1 if res[0] == "{" else res[1]
        if word == "class" or (word == "abstract" and nxt == "class"):
            return self.class_end(j + (word == "abstract"))
        if default:
            if word == "interface":
                pass
            else:
                end, why = self.p.scan(j, self.n, strict=True)
                stop = end - 1 if why == ";" else end
                return end if self.inert(j, stop) else None
        if word == "interface" and self.kind(j + 1) == "id" and not self.t[j + 1][3]:
            pos, why = self.p.scan(j + 2, self.n, angle=True, brace=True)
            return self.m[pos] + 1 if why == "{" else None
        if word == "type" and self.kind(j + 1) == "id" and not self.t[j + 1][3] and self.v(j + 2) in ("=", "<"):
            end, why = self.p.scan(j + 2, self.n)
            q = j + 3
            while q < end:
                # A line starting with ( [ or a template after a complete type may be code the scan swallowed.
                if self.t[q][3] and (self.v(q) in ("(", "[") or self.kind(q) == "tpl") and not self.p.is_open(self.t[q - 1]):
                    return None
                q = self.m[q] + 1 if self.kind(q) == "p" and self.v(q) in OPENERS else q + 1
            return end
        if word == "enum" or (word == "const" and nxt == "enum"):
            e = j + (word == "const")
            if self.kind(e + 1) != "id" or self.v(e + 2) != "{":
                return None
            for s, stop in self.items(e + 3, self.m[e + 2]):
                if self.kind(s) not in ("id", "str"):
                    return None
                if stop - s > 1 and not (self.v(s + 1) == "=" and self.inert(s + 2, stop)):
                    return None
            return self.m[e + 2] + 1
        if declare and word in ("namespace", "module"):
            i = j + 1
            while i < self.n and self.v(i) not in ("{", ";") and (self.kind(i) in ("id", "str") or self.v(i) == "."):
                i += 1
            if self.v(i) == "{":
                return self.m[i] + 1
            return i + 1 if self.v(i) == ";" else None
        if declare and word == "global" and nxt == "{":
            return self.m[j + 1] + 1
        if word in ("const", "let"):
            return self.variables_end(j)
        return None

    def class_end(self, k):
        """End of the class at k (the word class) when nothing in it runs as the class is defined."""
        j = k + 1
        if self.kind(j) == "id" and self.v(j) not in ("extends", "implements"):
            j += 1
        if self.v(j) == "<":
            j = self.angle(j, self.n)
            if j is None:
                return None
        pos, why = self.p.scan(j, self.n, angle=True, brace=True)
        if why != "{":
            return None
        i = j
        if self.v(i) == "extends":
            if not self.plain_id(i + 1):
                return None
            i += 2
            if self.v(i) == "<":
                i = self.angle(i, pos)
                if i is None:
                    return None
        if self.v(i) == "implements":
            i += 1
            while i < pos:
                if self.v(i) == "<":
                    i = self.angle(i, pos)
                    if i is None:
                        return None
                    continue
                if not (self.kind(i) == "id" or self.v(i) in (".", ",")):
                    return None
                i += 1
        if i != pos:
            return None
        close = self.m[pos]
        for u in self.p.class_body(pos, close):
            key = re.sub(r" #\d+$", "", u.key)
            if self.v(u.start) == "@" or key.startswith("static block"):
                return None
            rest = key[7:] if key.startswith("static ") else key
            if rest != "constructor":
                _, _, base = rest.partition(" ")
                if not base or base.startswith("["):
                    return None
            if key.startswith("static field "):
                end = u.end - 1 if self.v(u.end - 1) == ";" else u.end
                q = u.start
                while q < end:
                    if self.kind(q) == "p" and self.v(q) in OPENERS:
                        q = self.m[q] + 1
                        continue
                    if self.v(q) == "=":
                        if not self.inert(q + 1, end):
                            return None
                        break
                    q += 1
        return close + 1

    def variables_end(self, j):
        k = j + 1
        while True:
            if self.kind(k) != "id":
                return None
            k += 1
            if self.v(k) == "!":
                k += 1
            pos, why = k, "at"
            if self.v(pos) == ":":
                pos, why = self.p.scan(pos + 1, self.n, eq=True, comma=True, angle=True, strict=True)
            if why in ("at", "=") and pos < self.n and self.v(pos) == "=":
                start = pos + 1
                pos, why = self.p.scan(start, self.n, comma=True, strict=True)
                if not self.inert(start, pos - 1 if why == ";" else pos):
                    return None
            elif why == "at":
                if pos < self.n and self.v(pos) == "," and self.p.valid_comma(pos, self.n):
                    why = ","
                elif pos < self.n and self.v(pos) == ";":
                    pos += 1
                elif pos < self.n and not self.t[pos][3]:
                    return None
            if why == ",":
                k = pos + 1
                continue
            return pos

    # ----- expressions that only create a value -----------------------------

    def inert(self, a, b):
        """True when tokens a..b are an expression that only builds a value (a literal, a function, ...)."""
        if a >= b:
            return False
        v = self.v
        if self.arrow_expression(a, b):
            return True
        if v(b - 1) == "!" and b - 1 > a:
            return self.inert(a, b - 1)
        i = a + 1
        while i < b:
            if self.kind(i) == "p" and v(i) in OPENERS:
                i = self.m[i] + 1
                continue
            if self.kind(i) == "id" and v(i) in ("as", "satisfies") and v(i - 1) not in (".", "?."):
                return self.inert(a, i) and self.type_only(i + 1, b)
            i += 1
        kind, word, single = self.kind(a), v(a), b == a + 1
        if kind in ("str", "num", "re"):
            return single
        if kind == "tpl":
            return single and "${" not in self.text[self.t[a][1]:self.t[a][2]]
        if kind == "id":
            if word in ("true", "false", "null"):
                return single
            if word == "function" or (word == "async" and v(a + 1) == "function"):
                j = a + (word == "async") + 1
                if v(j) == "*":
                    j += 1
                if self.kind(j) == "id":
                    j += 1
                res = self.after_params(j, b)
                return bool(res) and res[0] == "{" and self.m[res[1]] + 1 == b
            if word == "class":
                return self.class_end(a) == b
            if word == "new" and self.kind(a + 1) == "id" and v(a + 1) in NEW_INERT:
                j = a + 2
                if v(j) == "<":
                    j = self.angle(j, b)
                    if j is None:
                        return False
                return v(j) == "(" and self.m[j] == j + 1 and j + 2 == b
            return single and word not in RESERVED
        if kind == "p":
            if word == "[" and self.m[a] + 1 == b:
                parts = self.items(a + 1, b - 1)
                return all(s < e and v(s) != "..." and self.inert(s, e) for s, e in parts)
            if word == "{" and self.m[a] + 1 == b:
                return all(self.inert_property(s, e) for s, e in self.items(a + 1, b - 1))
            if word == "-":
                return self.inert(a + 1, b)
        return False

    def inert_property(self, s, e):
        if s >= e or self.v(s) in ("...", "["):
            return False
        if self.kind(s) in ("id", "str", "num") and self.v(s + 1) == ":":
            return self.inert(s + 2, e)
        if e == s + 1 and self.plain_id(s):
            return True
        return self.method_at(s, e) is not None

    def arrow_expression(self, a, b):
        j = a
        if self.v(j) == "async" and self.v(j + 1) != "=>" and not self.t[j + 1][3]:
            j += 1
        if self.v(j) == "<":
            j = self.angle(j, b)
            if j is None:
                return False
        if self.v(j) == "(":
            pe = self.m[j] + 1
            if self.v(pe) == "=>":
                return pe + 1 < b
            if self.v(pe) == ":":
                k = pe + 1
                while k < b:
                    if self.kind(k) == "p" and self.v(k) in OPENERS:
                        k = self.m[k] + 1
                        continue
                    if self.v(k) == "=>":
                        return k + 1 < b
                    k += 1
            return False
        return self.kind(j) == "id" and self.v(j) not in RESERVED and self.v(j + 1) == "=>" and j + 2 < b

    def type_only(self, a, b):
        if a >= b:
            return False
        for i in range(a, b):
            kind, val = self.kind(i), self.v(i)
            if kind == "id":
                if val in NOT_TYPE:
                    return False
            elif kind not in ("str", "num") and not (kind == "p" and val in TYPE_PUNCT):
                return False
        return True


def table(blob, suffix):
    """The table for one file's bytes (see symbols.py), or None when it cannot be vouched for."""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\u2028" in text or "\u2029" in text or re.search(r"\r(?!\n)", text):
        return None  # line breaks the tokenizer and the line numbers do not agree on
    try:
        return _table(text, suffix)
    except (Unsure, IndexError, KeyError, RecursionError, ValueError):
        return None


def _table(text, suffix):
    jsx = suffix not in NO_JSX
    tokens, match, _ = Scanner(text, jsx, suffix == ".tsx").scan(0, False)
    module = Module(text, tokens, match)
    module.read()
    cands = module.candidates()
    roots = module.tree(cands)
    # Nothing may start inside a statement the import reader cut out.
    cuts = [c for c in module.cuts if not any(r.lo < c.hi and c.lo < r.hi for r in roots)]
    if len(cuts) != len(module.cuts):
        return None
    symbols, legacy = module.symbols(roots, cuts)
    out = {
        name: {"digest": digest(s["code"]), "start": s["start"], "end": s["end"], "ids": sorted(s["ids"])}
        for name, s in symbols.items()
    }
    imports = {}
    for source, bindings in sorted(module.imports.items()):
        ordered = dict(sorted(bindings.items()))
        imports[source] = {"digest": digest(json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)), "bindings": ordered}
    exports = set(module.exports)
    for local, name in getattr(module, "exported_locals", ()):
        if local in module.declared:
            exports.add(name)
    return {
        "symbols": out,
        "imports": imports,
        "legacy": legacy,
        "pure": module.pure(),
        "deps": sorted(module.deps),
        "exports": sorted(exports),
        "star": module.star,
    }
