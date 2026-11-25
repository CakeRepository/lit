"""PowerShell reader: a tokenizer plus bracket matching, no PowerShell needed.

The tokenizer understands # and <# #> comments, single and double quoted
strings (doubled quotes, backtick escapes, $( ) subexpressions, typographic
quotes), here-strings, backtick line continuation and ${braced} variables.
Units: function/filter/workflow (key "function name"), class (children: methods,
properties, constructors), enum, using lines, a top-level param block, and
Pester Describe/Context/It blocks (key "test name", nested). Everything else
stays in the text between units. Unbalanced or unterminated input is refused.
"""

import codecs
import re

NAME = "powershell"
SUFFIXES = (".ps1", ".psm1", ".psd1")

SQ = "'‘’‚‛"
DQ = '"“”„'
SPACE = " \t\f\v ﻿"
DELIMS = set(" \t\f\v \r\n{}()[];,|&") | set(SQ) | set(DQ)
PAIRS = {"{": "}", "(": ")", "[": "]"}
OPEN = "{(["
FUNCTION_WORDS = ("function", "filter", "workflow")
MODIFIERS = ("static", "hidden")
PESTER = ("describe", "context", "it")


class Unsure(Exception):
    pass


class Lexer:
    def __init__(self, text):
        self.t = text
        self.n = len(text)

    def lex(self, i, sub=False):
        t, n = self.t, self.n
        out = []
        depth = 0
        while i < n:
            c = t[i]
            if c in SPACE:
                i += 1
            elif c == "\r":
                if t.startswith("\n", i + 1):
                    i += 1
                else:
                    raise Unsure
            elif c == "\n":
                out.append(("n", i, i + 1))
                i += 1
            elif c == "`" and t.startswith("\n", i + 1):
                i += 2
            elif c == "`" and t.startswith("\r\n", i + 1):
                i += 3
            elif c == "#":
                j = t.find("\n", i)
                j = n if j < 0 else j
                out.append(("c", i, j))
                i = j
            elif c == "<" and t.startswith("<#", i):
                j = t.find("#>", i + 2)
                if j < 0:
                    raise Unsure
                out.append(("c", i, j + 2))
                i = j + 2
            elif c in SQ:
                j = self.single(i)
                out.append(("s", i, j))
                i = j
            elif c in DQ:
                j = self.double(i)
                out.append(("s", i, j))
                i = j
            elif c == "@" and i + 1 < n and t[i + 1] in "'\"":
                j = self.here(i)
                out.append(("s", i, j))
                i = j
            elif c in "{}[]":
                out.append((c, i, i + 1))
                i += 1
            elif c == "(":
                depth += 1
                out.append((c, i, i + 1))
                i += 1
            elif c == ")":
                if sub and depth == 0:
                    return out, i
                depth -= 1
                out.append((c, i, i + 1))
                i += 1
            elif c == ";":
                out.append((";", i, i + 1))
                i += 1
            elif c in ",|&":
                out.append(("o", i, i + 1))
                i += 1
            else:
                j = self.word(i)
                out.append(("w", i, j))
                i = j
        if sub:
            raise Unsure
        return out, i

    def word(self, i):
        t, n = self.t, self.n
        j = i
        while j < n:
            c = t[j]
            if c == "`":
                j += 2
            elif c == "$" and t.startswith("{", j + 1):
                j = self.braced(j + 1)
            elif c in DELIMS:
                break
            else:
                j += 1
        return min(j, n)

    def braced(self, i):
        t = self.t
        j = i + 1
        while j < self.n:
            if t[j] == "`":
                j += 2
            elif t[j] == "}":
                return j + 1
            else:
                j += 1
        raise Unsure

    def single(self, i):
        t = self.t
        j = i + 1
        while j < self.n:
            if t[j] in SQ:
                if j + 1 < self.n and t[j + 1] in SQ:
                    j += 2
                    continue
                return j + 1
            j += 1
        raise Unsure

    def double(self, i):
        t = self.t
        j = i + 1
        while j < self.n:
            c = t[j]
            if c == "`":
                j += 2
            elif c in DQ:
                if j + 1 < self.n and t[j + 1] in DQ:
                    j += 2
                    continue
                return j + 1
            elif c == "$" and t.startswith("(", j + 1):
                tokens, close = self.lex(j + 2, sub=True)
                matching(tokens)
                j = close + 1
            elif c == "$" and t.startswith("{", j + 1):
                j = self.braced(j + 1)
            else:
                j += 1
        raise Unsure

    def here(self, i):
        t = self.t
        quote = t[i + 1]
        k = i + 2
        while k < self.n and t[k] in " \t":
            k += 1
        if t.startswith("\r\n", k):
            k += 1
        if not t.startswith("\n", k):
            raise Unsure
        terminator = "\n" + quote + "@"
        j = t.find(terminator, k)
        if j < 0:
            raise Unsure
        return j + len(terminator)


def matching(tokens):
    pairs = {}
    stack = []
    for index, token in enumerate(tokens):
        kind = token[0]
        if kind in OPEN:
            stack.append(index)
        elif kind in "}])":
            if not stack or PAIRS[tokens[stack[-1]][0]] != kind:
                raise Unsure
            opener = stack.pop()
            pairs[opener] = index
    if stack:
        raise Unsure
    return pairs


def decode(blob):
    for prefix, encoding in (
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF8, "utf-8"),
    ):
        if blob.startswith(prefix):
            return blob[len(prefix):].decode(encoding), len(prefix), encoding
    return blob.decode("utf-8"), 0, "utf-8"


class Outliner:
    def __init__(self, text, prefix, encoding):
        self.text = text
        self.prefix = prefix
        self.encoding = encoding
        self.tokens, _ = Lexer(text).lex(0)
        self.match = matching(self.tokens)
        if text.isascii() and encoding == "utf-8":
            self.byte = lambda i: prefix + i
        else:
            offsets = [prefix]
            for ch in text:
                offsets.append(offsets[-1] + len(ch.encode(encoding)))
            self.byte = lambda i: offsets[i]

    # token helpers
    def kind(self, i):
        return self.tokens[i][0]

    def word(self, i):
        _, s, e = self.tokens[i]
        return self.text[s:e]

    def is_word(self, i, names):
        return self.kind(i) == "w" and self.word(i).lower() in names

    def skip(self, i, hi, kinds="nc"):
        while i < hi and self.kind(i) in kinds:
            i += 1
        return i

    def node(self, key, first, last, children):
        s, e = self.tokens[first][1], self.tokens[last][2]
        text = self.text
        p = s
        while p > 0 and text[p - 1] in " \t":
            p -= 1
        if p == 0 or text[p - 1] == "\n":
            s = p
        q = e
        while q < len(text) and text[q] in " \t\r":
            q += 1
        if q < len(text) and text[q] == "\n":
            e = q + 1
        return (key, self.byte(s), self.byte(e), children), last

    def statement_starts(self, lo, hi):
        starts = []
        fresh = True
        last = None
        i = lo
        while i < hi:
            kind = self.kind(i)
            if kind == "c":
                i += 1
            elif kind == "n":
                if last != "o":
                    fresh = True
                i += 1
            elif kind == ";":
                fresh = True
                last = ";"
                i += 1
            else:
                if fresh:
                    starts.append(i)
                    fresh = False
                if kind in OPEN:
                    last = "g"
                    i = self.match[i] + 1
                else:
                    last = kind
                    i += 1
        return starts

    def scan(self, lo, hi):
        nodes = []
        done = lo - 1
        for start in self.statement_starts(lo, hi):
            if start <= done:
                continue
            found = self.unit(start, hi)
            if found is not None:
                node, done = found
                nodes.append(node)
        return nodes

    def statement_end(self, i, hi):
        last = i
        while i < hi:
            kind = self.kind(i)
            if kind in "n;":
                break
            if kind == "c":
                i += 1
            elif kind in OPEN:
                last = self.match[i]
                i = last + 1
            else:
                last = i
                i += 1
        return last

    def unit(self, start, hi):
        j = start
        while j < hi and self.kind(j) == "[":
            j = self.skip(self.match[j] + 1, hi)
        if j >= hi or self.kind(j) != "w":
            return None
        keyword = self.word(j).lower()
        if keyword in FUNCTION_WORDS:
            k = self.skip(j + 1, hi, "c")
            if k >= hi or self.kind(k) != "w":
                raise Unsure
            name = self.word(k).lower()
            k = self.skip(k + 1, hi)
            if k < hi and self.kind(k) == "(":
                k = self.skip(self.match[k] + 1, hi)
            if k >= hi or self.kind(k) != "{":
                raise Unsure
            return self.node(f"function {name}", start, self.match[k], [])
        if keyword in ("class", "enum"):
            k = self.skip(j + 1, hi, "c")
            if k >= hi or self.kind(k) != "w":
                raise Unsure
            name = self.word(k).lower()
            k += 1
            while k < hi and self.kind(k) != "{":
                if self.kind(k) in "wonc":
                    k += 1
                elif self.kind(k) == "[":
                    k = self.match[k] + 1
                else:
                    raise Unsure
            if k >= hi:
                raise Unsure
            children = self.members(k + 1, self.match[k], name) if keyword == "class" else []
            return self.node(f"{keyword} {name}", start, self.match[k], children)
        if keyword == "using" and j == start:
            last = self.statement_end(j, hi)
            parts = [self.word(i) if self.kind(i) == "w" else self.text[self.tokens[i][1]:self.tokens[i][2]]
                     for i in range(j, last + 1) if self.kind(i) != "c"]
            return self.node(" ".join(parts).lower(), start, last, [])
        if keyword == "param":
            k = self.skip(j + 1, hi, "c")
            if k >= hi or self.kind(k) != "(":
                raise Unsure
            return self.node("param", start, self.match[k], [])
        if keyword in PESTER and j == start:
            return self.pester(j, hi, keyword)
        return None

    def pester(self, j, hi, keyword):
        k = j + 1
        brace = None
        last = j
        while k < hi:
            kind = self.kind(k)
            if kind in "n;":
                break
            if kind == "c":
                k += 1
            elif kind in OPEN:
                close = self.match[k]
                if kind == "{":
                    brace = (k, close)
                last = close
                k = close + 1
            else:
                last = k
                k += 1
        if brace is None or brace[1] != last:
            return None
        opener, close = brace
        before = opener - 1
        if before > j and self.kind(before) == "w" and self.tokens[before][2] == self.tokens[opener][1] \
                and self.word(before).endswith("@"):
            return None
        name = self.pester_name(j + 1, opener)
        if name is None:
            return None
        children = self.scan(opener + 1, close) if keyword != "it" else []
        return self.node(f"test {name}", j, close, children)

    def pester_name(self, lo, hi):
        i = self.skip(lo, hi, "c")
        if i >= hi:
            return None
        if self.kind(i) == "w" and self.word(i).lower() in ("-name", "-testname"):
            i = self.skip(i + 1, hi, "c")
            if i >= hi:
                return None
        kind = self.kind(i)
        raw = self.word(i) if kind in "ws" else ""
        if kind == "s" and raw[0] in SQ + DQ:
            inner = raw[1:-1]
            if raw[0] in SQ:
                inner = re.sub("[" + SQ + "]{2}", lambda m: m.group()[0], inner)
            return inner.strip() or None
        if kind == "w" and not raw.startswith(("-", "@")):
            return raw
        return None

    def members(self, lo, hi, cls):
        found = []
        arities = {}
        i = lo
        while i < hi:
            if self.kind(i) in "nc;":
                i += 1
                continue
            start = i
            j = i
            while True:
                if j < hi and self.kind(j) == "[":
                    j = self.match[j] + 1
                elif j < hi and self.is_word(j, MODIFIERS):
                    j += 1
                else:
                    break
                j2 = self.skip(j, hi)
                if j2 < hi and (self.kind(j2) == "[" or self.is_word(j2, MODIFIERS)):
                    j = j2
                else:
                    j = j2
                    break
            if j >= hi or self.kind(j) != "w":
                raise Unsure
            token = self.word(j)
            if token.startswith("$"):
                named = re.match(r"\$(?:\{([^}]*)\}|(\w+))", token)
                if not named:
                    raise Unsure
                name = (named.group(1) or named.group(2)).lower()
                last = self.statement_end(j, hi)
                if self.kind(last) == "o" or self.word(last) in ("=", "+", "-"):
                    raise Unsure
                node, i = self.node(f"property ${name}", start, last, [])
            else:
                name = token.lower()
                k = self.skip(j + 1, hi)
                if k >= hi or self.kind(k) != "(":
                    raise Unsure
                parens = k
                k = self.skip(self.match[k] + 1, hi)
                while k < hi and self.kind(k) != "{":
                    if self.kind(k) in "wonc":
                        k += 1
                    elif self.kind(k) == "(":
                        k = self.match[k] + 1
                    else:
                        raise Unsure
                if k >= hi:
                    raise Unsure
                label = "constructor" if name == cls else "method"
                node, i = self.node(f"{label} {name}", start, self.match[k], [])
                arities[len(found)] = self.arity(parens)
            found.append(node)
            i += 1
        # Overloads share a name: tell them apart by parameter count, but only
        # when there is something to tell apart.
        names = [n[0] for n in found]
        return [
            (f"{key}({arities[index]})" if index in arities and names.count(key) > 1 else key, *rest)
            for index, (key, *rest) in enumerate(found)
        ]

    def arity(self, opener):
        count = 0
        i = opener + 1
        while i < self.match[opener]:
            if self.kind(i) in OPEN:
                i = self.match[i]
            elif self.kind(i) == "w" and self.word(i).startswith("$"):
                count += 1
            i += 1
        return count


def outline(blob, suffix):
    try:
        text, prefix, encoding = decode(blob)
    except UnicodeError:
        return None
    if "\x00" in text:
        return None
    try:
        return Outliner(text, prefix, encoding).scan(0, len(Lexer(text).lex(0)[0]))
    except (Unsure, IndexError):
        return None
