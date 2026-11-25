"""JavaScript and TypeScript reader: lit's own tokenizer and declaration scanner.

No Node, no Babel: the file is decoded as UTF-8 and cut into tokens (comments,
strings, template literals with nested ${...}, regex literals, and JSX in .jsx
and .tsx), brackets are matched, and the top level is read statement by
statement. Imports, exports, functions, classes (with their members), variable
declarations, TypeScript interfaces, type aliases, enums and namespaces become
units; so do top-level describe/test/it/suite calls. Expression statements stay
in the text between units.

Whenever the reader is not sure (an unterminated string, comment, template,
regex or JSX element, unbalanced brackets, an index signature, a declaration it
does not recognise after export or declare) it returns None and the file falls
back to Git's line merge.
"""

import re
from itertools import accumulate

NAME = "javascript"
SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")


class Unsure(Exception):
    """The reader cannot vouch for this file."""


WS = re.compile(r"(?:[^\S\n  ]|﻿)+")
IDENT = re.compile(r"#?(?:[^\W\d]|\$)[\w$]*")
NUMBER = re.compile(
    r"(?:0[xXbBoO][0-9a-fA-F_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?)n?"
)
PUNCT = re.compile(
    r">>>=|\.\.\.|===|!==|\*\*=|<<=|>>=|>>>|&&=|\|\|=|\?\?=|=>|==|!=|<=|>=|&&|\|\||\?\?"
    r"|\?\.(?!\d)|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|\*\*|<<|>>|[{}()\[\];,<>+\-*/%&|^!~?:=.@]"
)
SQ = re.compile(r"'(?:[^'\\\n\r]|\\(?:\r\n|[\s\S]))*'")
DQ = re.compile(r'"(?:[^"\\\n\r]|\\(?:\r\n|[\s\S]))*"')
REGEX = re.compile(r"/(?![*/])(?:[^/\\\[\n\r]|\\[^\n\r]|\[(?:[^\]\\\n\r]|\\[^\n\r])*\])+/[A-Za-z]*")
TEMPLATE_CHUNK = re.compile(r"[^`\\$]*")
JSX_NAME = re.compile(r"[\w$][\w$.:\-]*")
JSX_TEXT = re.compile(r"[^<{]*")
TSX_GENERIC = re.compile(r"<\s*[A-Za-z_$][\w$]*\s*(?:,|extends\b)")

# After these keywords a "/" starts a regex, and a "<" may start JSX.
BEFORE_EXPRESSION = frozenset(
    "return typeof case do else in of new delete void throw yield await instanceof default".split()
)
CONTROL = frozenset(("if", "while", "for", "with"))
OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {")": "(", "]": "[", "}": "{"}

# A line ends a statement only if its last token does not need an operand ...
OPEN_PUNCT = frozenset(
    ". ?. , = => ? : ... + - * / % ** & | ^ && || ?? == != === !== <= >= << >> >>> < "
    "+= -= *= /= %= **= &= |= ^= <<= >>= >>>= &&= ||= ??= @".split()
)
OPEN_KEYWORDS = frozenset(
    "typeof keyof extends implements new in instanceof as satisfies await yield delete throw".split()
)
# ... and the next line does not continue it.
CONTINUE_PUNCT = frozenset(
    ". ?. ( [ , = => ? : + - * / % ** & | ^ && || ?? == != === !== < > <= >= << >> >>> "
    "+= -= *= /= %= **= &= |= ^= <<= >>= >>>= &&= ||= ??=".split()
)
CONTINUE_KEYWORDS = frozenset("else catch finally extends implements instanceof in as satisfies".split())
TYPE_BRACE_AFTER = frozenset((":", "|", "&", ",", "=>", "<", "?", "="))

TEST_FUNCTIONS = frozenset(("describe", "test", "it", "suite"))
TEST_MODIFIERS = frozenset("only skip todo concurrent serial failing sequential".split())
MEMBER_MODIFIERS = frozenset(
    "static public private protected readonly override abstract declare async accessor get set".split()
)
DECLARATION_WORDS = frozenset(
    "function async class abstract interface type enum const let var namespace module global".split()
)


class Scanner:
    """Text to tokens: (kind, start, end, newline_before, value).

    kind is id, num, str, tpl (a whole template literal), re, jsx (a whole JSX
    element) or p (punctuation). value is the text of an id or punctuation.
    """

    def __init__(self, text, jsx, tsx):
        self.text = text
        self.jsx = jsx
        self.tsx = tsx

    def scan(self, i, nested):
        """Tokens from i. Nested scans (inside ${} and JSX {}) stop after the
        unmatched closing brace. Returns (tokens, bracket matches, next index)."""
        text, n = self.text, len(self.text)
        tokens, match, stack = [], {}, []
        newline = False
        operand = False  # the previous token ends an operand, so "/" divides
        last = before = None
        if i == 0 and text.startswith("#!"):
            i = text.find("\n") if "\n" in text else n
        while True:
            if i >= n:
                if nested or stack:
                    raise Unsure("unterminated")
                return tokens, match, i
            c = text[i]
            m = WS.match(text, i)
            if m:
                i = m.end()
                continue
            if c in "\n  ":
                newline = True
                i += 1
                continue
            if c == "/":
                nxt = text[i + 1 : i + 2]
                if nxt == "/":
                    end = i
                    while end < n and text[end] not in "\n  ":
                        end += 1
                    i = end
                    continue
                if nxt == "*":
                    end = text.find("*/", i + 2)
                    if end < 0:
                        raise Unsure("unterminated comment")
                    if "\n" in text[i:end]:
                        newline = True
                    i = end + 2
                    continue
                if not operand:
                    m = REGEX.match(text, i)
                    if not m:
                        raise Unsure("bad regex")
                    tokens.append(("re", i, m.end(), newline, ""))
                    i, newline, operand = m.end(), False, True
                    before, last = last, "/re/"
                    continue
            kind = value = None
            if c in "\"'":
                m = (SQ if c == "'" else DQ).match(text, i)
                if not m:
                    raise Unsure("unterminated string")
                kind, end = "str", m.end()
            elif c == "`":
                kind, end = "tpl", self.template(i)
            elif c.isdigit() or (c == "." and text[i + 1 : i + 2].isdigit()):
                m = NUMBER.match(text, i)
                kind, end = "num", m.end()
            elif c == "<" and self.jsx and not operand and self.jsx_start(i):
                kind, end = "jsx", self.jsx_element(i)
            else:
                m = IDENT.match(text, i)
                if m:
                    kind, end, value = "id", m.end(), m.group()
                else:
                    m = PUNCT.match(text, i)
                    if not m:
                        raise Unsure("unknown character")
                    kind, end, value = "p", m.end(), m.group()
            index = len(tokens)
            if kind == "p":
                if value == "(":
                    control = last in CONTROL or (last == "await" and before == "for")
                    stack.append((index, control))
                    operand = False
                elif value in "[{" and len(value) == 1:
                    stack.append((index, False))
                    operand = False
                elif value in CLOSERS:
                    if not stack:
                        if nested and value == "}":
                            return tokens, match, end
                        raise Unsure("unbalanced")
                    opened, control = stack.pop()
                    if tokens[opened][4] != CLOSERS[value]:
                        raise Unsure("mismatched")
                    match[opened], match[index] = index, opened
                    operand = value == "]" or value == "}" or (value == ")" and not control)
                elif value in ("++", "--", "!"):
                    pass  # postfix keeps the operand state; prefix state is False anyway
                else:
                    operand = False
            elif kind == "id":
                operand = value not in BEFORE_EXPRESSION
            else:
                operand = True
            tokens.append((kind, i, end, newline, value or ""))
            before, last = last, value if kind in ("id", "p") else ""
            newline = False
            i = end

    def template(self, i):
        text, n = self.text, len(self.text)
        i += 1
        while True:
            i = TEMPLATE_CHUNK.match(text, i).end()
            if i >= n:
                raise Unsure("unterminated template")
            c = text[i]
            if c == "`":
                return i + 1
            if c == "\\":
                i += 2
            elif text[i + 1 : i + 2] == "{":
                i = self.scan(i + 2, True)[2]
            else:
                i += 1

    def jsx_start(self, i):
        nxt = self.text[i + 1 : i + 2]
        if not (nxt == ">" or nxt.isalpha() or nxt in ("_", "$")):
            return False
        return not (self.tsx and TSX_GENERIC.match(self.text, i))

    def jsx_space(self, i):
        text = self.text
        while True:
            m = WS.match(text, i)
            if m:
                i = m.end()
            if text[i] in "\n  ":
                i += 1
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                if end < 0:
                    raise Unsure("unterminated comment")
                i = end + 2
            elif not m:
                return i

    def jsx_element(self, i):
        text = self.text
        i = self.jsx_space(i + 1)
        if text[i] == ">":
            name, i = "", i + 1
        else:
            m = JSX_NAME.match(text, i)
            if not m:
                raise Unsure("bad JSX tag")
            name, i = m.group(), m.end()
            while True:
                i = self.jsx_space(i)
                c = text[i]
                if c == "/":
                    i = self.jsx_space(i + 1)
                    if text[i] != ">":
                        raise Unsure("bad JSX tag")
                    return i + 1
                if c == ">":
                    i += 1
                    break
                if c == "{":
                    i = self.scan(i + 1, True)[2]
                    continue
                m = JSX_NAME.match(text, i)
                if not m:
                    raise Unsure("bad JSX attribute")
                i = self.jsx_space(m.end())
                if text[i] == "=":
                    i = self.jsx_space(i + 1)
                    c = text[i]
                    if c in "\"'":
                        end = text.find(c, i + 1)
                        if end < 0:
                            raise Unsure("unterminated JSX string")
                        i = end + 1
                    elif c == "{":
                        i = self.scan(i + 1, True)[2]
                    elif c == "<":
                        i = self.jsx_element(i)
                    else:
                        raise Unsure("bad JSX attribute value")
        while True:
            i = JSX_TEXT.match(text, i).end()
            if i >= len(text):
                raise Unsure("unterminated JSX")
            if text[i] == "{":
                i = self.scan(i + 1, True)[2]
                continue
            j = self.jsx_space(i + 1)
            if text[j] != "/":
                i = self.jsx_element(i)
                continue
            j = self.jsx_space(j + 1)
            m = JSX_NAME.match(text, j)
            closing = ""
            if m:
                closing, j = m.group(), m.end()
            j = self.jsx_space(j)
            if text[j] != ">" or closing != name:
                raise Unsure("mismatched JSX tag")
            return j + 1


class Unit:
    __slots__ = ("key", "start", "end", "children", "overload", "sig")

    def __init__(self, key, start, end, children=(), overload=False, sig=False):
        self.key, self.start, self.end = key, start, end
        self.children, self.overload, self.sig = list(children), overload, sig


class Parser:
    """Tokens to units. Positions are token indexes; end is exclusive."""

    def __init__(self, text, tokens, match):
        self.text, self.t, self.m = text, tokens, match

    def v(self, k):
        return self.t[k][4] if 0 <= k < len(self.t) else None

    def kind(self, k):
        return self.t[k][0] if 0 <= k < len(self.t) else None

    def norm(self, first, last):
        """Whitespace-normalised text from token first through token last."""
        return " ".join(self.text[self.t[first][1] : self.t[last][2]].split())

    # ----- statement scanning -------------------------------------------------

    def is_open(self, token):
        return (token[0] == "p" and token[4] in OPEN_PUNCT) or (
            token[0] == "id" and token[4] in OPEN_KEYWORDS
        )

    def is_continuation(self, token, brace):
        kind, value = token[0], token[4]
        if kind == "tpl":
            return True
        if kind == "p":
            return value in CONTINUE_PUNCT or (brace and value == "{")
        return kind == "id" and value in CONTINUE_KEYWORDS

    def valid_comma(self, k, hi):
        """A comma at k separates variable declarators (not a generic's comma)."""
        t = self.t
        j = k + 1
        if j >= hi:
            return False
        if t[j][0] == "id":
            after = j + 1
        elif t[j][4] in ("{", "["):
            after = self.m[j] + 1
        else:
            return False
        if after >= len(t) or after >= hi:
            return True
        return t[after][4] in ("=", ":", ",", ";", "!") or t[after][3]

    def skip_angle(self, k, hi):
        """Index after the type-argument list opening at k, or k + 1 if unsure."""
        t, depth, j = self.t, 0, k
        while j < hi:
            value = t[j][4]
            if t[j][0] == "p":
                if value == "<":
                    depth += 1
                elif value == ">":
                    depth -= 1
                elif value == ">>":
                    depth -= 2
                elif value == ">>>":
                    depth -= 3
                elif value in OPENERS:
                    j = self.m[j]
                elif value in (";", "}", ")", "]"):
                    return k + 1
            j += 1
            if depth <= 0:
                return j if depth == 0 else k + 1
        return k + 1

    def scan(self, k, hi, eq=False, comma=False, angle=False, brace=False, strict=False):
        """Walk one statement from token k. Returns (index, why): the statement
        ends at index (exclusive) because of ';' (included), a newline (asi),
        hi (eof); or index is where a body '{' ('{') or a stop token ('=', ',') is."""
        t, m = self.t, self.m
        start = k
        while k < hi:
            kind, value = t[k][0], t[k][4]
            if kind == "p":
                if value in OPENERS:
                    if brace and value == "{" and not (k > start and t[k - 1][4] in TYPE_BRACE_AFTER):
                        return k, "{"
                    k = m[k] + 1
                elif value == ";":
                    return k + 1, ";"
                elif value == "=" and eq:
                    return k, "="
                elif value == "," and comma and self.valid_comma(k, hi):
                    return k, ","
                elif value == "<" and angle:
                    k = self.skip_angle(k, hi)
                else:
                    k += 1
            else:
                k += 1
            if k >= hi:
                return hi, "eof"
            nxt = t[k]
            if nxt[3] and nxt[4] != ";" and not self.is_open(t[k - 1]):
                if not self.is_continuation(nxt, brace):
                    return k, "asi"
                if strict and t[k - 1][4] == "}" and (nxt[0] == "tpl" or nxt[4] in ("(", "[")):
                    raise Unsure("ambiguous line break after a block")
        return hi, "eof"

    def skip_statement(self, k, hi):
        end = self.scan(k, hi)[0]
        return max(end, k + 1)

    # ----- blocks -------------------------------------------------------------

    def block(self, lo, hi, container):
        units, k = [], lo
        while k < hi:
            if self.v(k) == ";":
                k += 1
                continue
            unit = self.unit(k, hi)
            if unit is None:
                k = self.skip_statement(k, hi)
            else:
                units.append(unit)
                k = unit.end
        return self.finish(units)

    def finish(self, units):
        merged = []
        for unit in units:
            if merged and merged[-1].sig and merged[-1].overload and unit.overload and merged[-1].key == unit.key:
                unit.start = merged.pop().start
            merged.append(unit)
        seen = {}
        for unit in merged:
            seen[unit.key] = seen.get(unit.key, 0) + 1
            if seen[unit.key] > 1:
                unit.key = f"{unit.key} #{seen[unit.key]}"
        return merged

    def decorator(self, j, hi):
        j += 1
        if self.kind(j) == "id":
            j += 1
            while self.v(j) == "." and self.kind(j + 1) == "id":
                j += 2
        if self.v(j) != "(":
            raise Unsure("decorator")
        return self.m[j] + 1

    def unit(self, k, hi):
        t, j = self.t, k
        export = default = declare = False
        while True:
            while self.v(j) == "@":
                j = self.decorator(j, hi)
            word = t[j][4] if t[j][0] == "id" else None
            if word == "export" and not export:
                export = True
                j += 1
                nxt = self.v(j)
                if nxt == "default":
                    default = True
                    j += 1
                elif nxt in ("{", "*") or (nxt == "type" and self.v(j + 1) in ("{", "*")):
                    if nxt == "type":
                        j += 1
                    return self.export_list(k, j, hi)
                elif nxt == "=":
                    return Unit("export =", k, self.scan(j, hi)[0])
                elif nxt == "as" and self.v(j + 1) == "namespace":
                    end = self.scan(j, hi)[0]
                    return Unit(f"export as namespace {self.v(j + 2)}", k, end)
            elif word == "declare" and not declare and self.declaration_follows(j + 1):
                declare = True
                j += 1
            elif word == "abstract" and self.v(j + 1) == "class":
                j += 1
            else:
                break
        found = self.declaration(k, j, hi, default, export or declare)
        if found is not None:
            return found
        if default:
            return Unit("export default", k, self.scan(j, hi)[0])
        if export or declare:
            raise Unsure("unknown declaration")
        return self.test_call(k, j, hi)

    def declaration_follows(self, j):
        return self.kind(j) == "id" and self.v(j) in DECLARATION_WORDS and not self.t[j][3]

    def name_follows(self, j):
        return self.kind(j) in ("id", "str") and not self.t[j][3]

    def declaration(self, k, j, hi, default, prefixed):
        t = self.t
        word = t[j][4] if t[j][0] == "id" else None
        if word == "import":
            if self.v(j + 1) in ("(", "."):
                if prefixed:
                    raise Unsure("import expression")
                return None
            return self.import_statement(k, j, hi)
        if word == "function" or (word == "async" and self.v(j + 1) == "function" and not t[j + 1][3]):
            return self.function(k, j, hi, default)
        if word == "class":
            return self.class_declaration(k, j, hi, default)
        if word == "interface" and self.name_follows(j + 1):
            return self.braced(k, j + 1, hi, f"interface {self.v(j + 1)}")
        if word == "enum" or (word == "const" and self.v(j + 1) == "enum"):
            j += word == "const"
            if not self.name_follows(j + 1):
                raise Unsure("enum")
            return self.braced(k, j + 1, hi, f"enum {self.v(j + 1)}")
        if word == "type" and self.name_follows(j + 1) and self.v(j + 2) in ("=", "<"):
            return Unit(f"type {self.v(j + 1)}", k, self.scan(j + 2, hi)[0])
        if word in ("namespace", "module") and self.name_follows(j + 1):
            return self.namespace(k, j, hi)
        if word == "global" and prefixed and self.v(j + 1) == "{":
            close = self.m[j + 1]
            return Unit("global", k, close + 1, self.block(j + 2, close, True))
        if word in ("const", "var") or (word == "let" and (self.kind(j + 1) == "id" or self.v(j + 1) in ("{", "["))):
            return self.variables(k, j, hi)
        return None

    def import_statement(self, k, j, hi):
        end = self.scan(j + 1, hi)[0]
        t, i, source = self.t, j + 1, None
        while i < end:
            if t[i][0] == "p" and t[i][4] in OPENERS:
                if t[i][4] == "(" and self.v(i - 1) == "require" and self.kind(i + 1) == "str":
                    source = t[i + 1]
                    break
                i = self.m[i]
            elif t[i][0] == "str":
                source = t[i]
                break
            i += 1
        if source is not None:
            return Unit(f"import {self.text[source[1] + 1 : source[2] - 1]}", k, end)
        names = [x[4] for x in t[j + 1 : end] if x[0] == "id"]
        if len(names) >= 2 and names[0] == "type":
            names = names[1:]
        if not names:
            raise Unsure("import")
        return Unit(f"import {names[0]}", k, end)

    def export_list(self, k, j, hi):
        end = self.scan(j, hi)[0]
        i, source = j, None
        while i < end:
            if self.t[i][0] == "p" and self.t[i][4] in OPENERS:
                i = self.m[i]
            elif self.t[i][0] == "str":
                source = self.t[i]
                break
            i += 1
        if source is not None:
            return Unit(f"export from {self.text[source[1] + 1 : source[2] - 1]}", k, end)
        return Unit("export " + self.norm(j, end - 1).rstrip(";").strip(), k, end)

    def function(self, k, j, hi, default):
        if self.v(j) == "async":
            j += 1
        j += 1
        if self.v(j) == "*":
            j += 1
        name = None
        if self.kind(j) == "id":
            name = self.v(j)
            j += 1
        if name is None and not default:
            raise Unsure("anonymous function declaration")
        pos, why = self.scan(j, hi, angle=True, brace=True)
        key = f"function {name}" if name else "export default"
        if why == "{":
            return Unit(key, k, self.m[pos] + 1, (), True, False)
        return Unit(key, k, pos, (), True, True)

    def braced(self, k, name_index, hi, key):
        pos, why = self.scan(name_index + 1, hi, angle=True, brace=True)
        if why != "{":
            raise Unsure("declaration without a body")
        return Unit(key, k, self.m[pos] + 1)

    def class_declaration(self, k, j, hi, default):
        j += 1
        name = None
        if self.kind(j) == "id" and self.v(j) not in ("extends", "implements"):
            name = self.v(j)
            j += 1
        if name is None and not default:
            raise Unsure("anonymous class declaration")
        pos, why = self.scan(j, hi, angle=True, brace=True)
        if why != "{":
            raise Unsure("class without a body")
        close = self.m[pos]
        key = f"class {name}" if name else "export default"
        return Unit(key, k, close + 1, self.class_body(pos, close))

    def namespace(self, k, j, hi):
        i = j + 1
        while self.v(i) not in ("{", ";", None) and i < hi:
            if self.kind(i) not in ("id", "str") and self.v(i) != ".":
                raise Unsure("namespace name")
            i += 1
        name = self.norm(j + 1, i - 1)
        if self.v(i) == ";":
            return Unit(f"{self.v(j)} {name}", k, i + 1)
        if self.v(i) != "{":
            raise Unsure("namespace")
        close = self.m[i]
        return Unit(f"{self.v(j)} {name}", k, close + 1, self.block(i + 1, close, True))

    def variables(self, k, j, hi):
        keyword, j, names = self.v(j), j + 1, []
        while True:
            if self.kind(j) == "id":
                names.append(self.v(j))
                j += 1
            elif self.v(j) in ("{", "["):
                names.append(self.norm(j, self.m[j]))
                j = self.m[j] + 1
            else:
                raise Unsure("variable name")
            if self.v(j) == "!":
                j += 1
            pos, why = j, "at"
            if self.v(pos) == ":":
                pos, why = self.scan(pos + 1, hi, eq=True, comma=True, angle=True, strict=True)
            if why in ("at", "=") and pos < hi and self.v(pos) == "=":
                pos, why = self.scan(pos + 1, hi, comma=True, strict=True)
            elif why == "at":
                if pos < hi and self.v(pos) == "," and self.valid_comma(pos, hi):
                    why = ","
                elif pos < hi and self.v(pos) == ";":
                    pos += 1
                elif pos < hi and not self.t[pos][3]:
                    raise Unsure("variable declaration")
            if why == ",":
                j = pos + 1
                continue
            return Unit(f"{keyword} {','.join(names)}", k, pos)

    def test_call(self, k, j, hi):
        t = self.t
        if t[j][0] != "id" or t[j][4] not in TEST_FUNCTIONS:
            return None
        p = j + 1
        while self.v(p) == "." and self.v(p + 1) in TEST_MODIFIERS:
            p += 2
        if self.v(p) != "(" or self.kind(p + 1) not in ("str", "tpl"):
            return None
        literal = t[p + 1]
        body = self.text[literal[1] + 1 : literal[2] - 1]
        if literal[0] == "tpl" and "${" in body:
            return None
        return Unit(f"test {body}", k, self.scan(j, hi)[0])

    # ----- classes ------------------------------------------------------------

    def class_body(self, open_brace, close):
        members, k = [], open_brace + 1
        while k < close:
            if self.v(k) == ";":
                k += 1
                continue
            member = self.member(k, close)
            members.append(member)
            k = member.end
        return self.finish(members)

    def member(self, k, hi):
        t, j = self.t, k
        while self.v(j) == "@":
            j = self.decorator(j, hi)
        static = get = set_ = False
        while t[j][0] == "id" and t[j][4] in MEMBER_MODIFIERS:
            word, nxt = t[j][4], j + 1
            if word == "static" and self.v(nxt) == "{":
                return Unit("static block", k, self.m[nxt] + 1)
            if nxt >= hi or not (
                t[nxt][0] in ("id", "str", "num") or t[nxt][4] in ("[", "*")
            ):
                break
            if word == "async" and t[nxt][3]:
                break
            static, get, set_ = static or word == "static", get or word == "get", set_ or word == "set"
            j += 1
        if self.v(j) == "*":
            j += 1
        if j >= hi:
            raise Unsure("member")
        token = t[j]
        if token[0] in ("id", "str", "num"):
            name, j = self.text[token[1] : token[2]], j + 1
        elif token[4] == "[":
            if self.kind(j + 1) == "id" and self.v(j + 2) == ":":
                raise Unsure("index signature")
            name, j = self.norm(j, self.m[j]), self.m[j] + 1
        else:
            raise Unsure("member")
        if self.v(j) in ("?", "!") and j < hi:
            j += 1
        prefix = "static " if static else ""
        if j < hi and self.v(j) in ("(", "<"):
            pos, why = self.scan(j, hi, angle=True, brace=True)
            end, sig = (self.m[pos] + 1, False) if why == "{" else (pos, True)
            if name == "constructor" and not static:
                key = "constructor"
            else:
                key = prefix + ("get " if get else "set " if set_ else "method ") + name
            return Unit(key, k, end, (), True, sig)
        if j >= hi or (t[j][3] and self.v(j) not in (":", "=", ";")):
            return Unit(f"{prefix}field {name}", k, j)
        if self.v(j) in (":", "=", ";"):
            return Unit(f"{prefix}field {name}", k, self.scan(j, hi, strict=True)[0])
        raise Unsure("member")


def outline(blob, suffix):
    """Top-level units with their members, or None if the file is not understood."""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    tsx = suffix == ".tsx"
    try:
        tokens, match, _ = Scanner(text, suffix in (".jsx", ".tsx"), tsx).scan(0, False)
        parser = Parser(text, tokens, match)
        units = parser.block(0, len(tokens), False)
    except (Unsure, IndexError, RecursionError, KeyError):
        return None
    if text.isascii():
        offsets = None
    else:
        offsets = list(accumulate((len(c.encode("utf-8")) for c in text), initial=0))

    def extent(first, last):
        start, end = tokens[first][1], tokens[last][2]
        line = text.rfind("\n", 0, start) + 1
        if not text[line:start].strip(" \t\r\f\v﻿ "):
            start = line
        newline = text.find("\n", end)
        rest = text[end:] if newline < 0 else text[end:newline]
        if not rest.strip(" \t\r\f\v﻿ "):
            end = len(text) if newline < 0 else newline + 1
        return start, end

    def convert(items):
        nodes = []
        for unit in items:
            start, end = extent(unit.start, unit.end - 1)
            if offsets:
                start, end = offsets[start], offsets[end]
            nodes.append((unit.key, start, end, convert(unit.children)))
        return nodes

    return convert(units)
