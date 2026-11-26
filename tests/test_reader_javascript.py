import time
import unittest

from concurrent_merge import readers
from concurrent_merge.readers import javascript
from concurrent_merge.structure import outline_merge

from reader_conformance import ReaderConformance

TS = b'''import { Injectable } from '@angular/core';
import type { Repo } from './repo';

export enum Status {
  Active = 'active',
  Closed = 'closed',
}

export interface Ticket {
  id: number;
  title: string;
  status: Status;
}

export type Filter = Partial<Ticket> & {
  limit?: number;
};

export function summarize(t: Ticket): string {
  return `${t.id}: ${t.title} (${t.status})`;
}

export function pending(ts: Ticket[]): Ticket[] {
  return ts.filter((t) => t.status === Status.Active);
}

@Injectable()
export class TicketService {
  private cache = new Map<number, Ticket>();
  static readonly LIMIT = 50;

  constructor(private repo: Repo) {}

  async find(id: number): Promise<Ticket | undefined> {
    return this.cache.get(id) ?? (await this.repo.load(id));
  }

  close(id: number): void {
    const t = this.cache.get(id);
    if (t) t.status = Status.Closed;
  }
}
'''

TS_TYPES = b'''// Ambient and overloaded declarations
declare module "legacy-lib" {
  export function parse(input: string): number;
}

declare global {
  interface Window { app: unknown }
}

namespace Geometry.Shapes {
  export const unit = 1;
  export function area(w: number, h: number): number { return w * h; }
}

export function pick<T>(items: T[], index: number): T;
export function pick<T>(items: T[], index: number, fallback: T): T;
export function pick<T>(items: T[], index: number, fallback?: T): T {
  return items[index] ?? (fallback as T);
}

export abstract class Shape<T extends { id: number } = { id: 1 }> extends Base<T> implements Named, Sized {
  abstract area(): number;
  protected abstract readonly kind: string
  declare phantom: string;
  [Symbol.toStringTag] = 'Shape';
  static #instances = 0;
  get label(): string { return this.kind; }
  set label(v: string) { this.kind = v; }
  public async *stream(): AsyncGenerator<number> { yield 1; }
  describe(a: string): string;
  describe(a: number): string;
  describe(a: unknown): string { return String(a); }
  static { Shape.#instances = 1; }
}

const { host, port = 8080 }: Config = loadConfig(), retries = 3;
export const handler = async (event: Event): Promise<void> => {
  const ratio = event.total / event.count / 2;
  console.log(ratio);
};
export default interface Options { verbose?: boolean }
export { host as hostName, port };
export * from './more';
export * as util from "./util";
import legacy = require("legacy-lib");
const a = 1; const b = 2;
let later
'''

TS_STYLE = b'''/** Header comment: it's "quoted" and has { braces } and `ticks` */
import fs from "fs"
import path from "path"

type Matcher = (value: string) => boolean
type Union =
  | "a"
  | "b"

const URL_RE = /^https?:\\/\\/[^\\s/]+(\\/.*)?$/
const half = (n: number) => n / 2 / 1
const items = [1, 2, 3]
  .map((n) => n * 2)
  .filter(Boolean)

function check(value: string): boolean
{
  return URL_RE.test(value) // "unterminated? no: it's a comment
}

export class Queue<T>
  extends Array<T>
{
  push(...items: T[]): number { return super.push(...items) }
  size = 0
  peek(): T | undefined { return this[0] }
}

test("adds numbers", () => {
  expect(1 + 1).toBe(2)
})
describe.skip("later", () => {})
console.log(path.join("a", "b"))
'''

TSX = b'''import React, { useState } from 'react';
import type { FC } from 'react';

type Props<T> = { items: T[]; render: (t: T) => JSX.Element };

export const List = <T,>({ items, render }: Props<T>) => {
  return <ul>{items.map((i) => <li key={String(i)}>{render(i)}</li>)}</ul>;
};

export const Pair = <K extends string, V>(props: { k: K; v: V }) => [props.k, props.v] as const;

export function Counter({ start = 0 }: { start?: number }) {
  const [n, setN] = useState<number>(start);
  const half = n / 2 / 1;
  return (
    <div className="counter" data-quote='say "hi"'>
      <p>Don't click "me" {n > 1 ? <b>many</b> : <i>few</i>} it's {/* note */} fine</p>
      <>
        <Child {...props} onClick={() => setN(n + 1)} label={`n=${n}`} />
      </>
      <button disabled>It's {half}</button>
    </div>
  );
}

export const Footer: FC = () => <footer>&copy; Company's 2026</footer>;

export default Counter;
'''

JSX = b'''import { useEffect } from 'react'

export function Title({ text }) {
  useEffect(() => { document.title = text }, [text])
  return <h1 title="it's">{text}</h1>
}

export class Panel extends React.Component {
  state = { open: false }
  toggle = () => this.setState({ open: !this.state.open });
  render() {
    const { children } = this.props
    return <section>
      {this.state.open && <div className='body'>{children}</div>}
      <small>Won't {"you"} stay?</small>
    </section>
  }
}
'''

JS = b''''use strict'
import express from 'express'
import { readFile } from 'node:fs/promises'

const app = express()
const PORT = process.env.PORT || 3000
const ROUTE = /^\\/api\\/(\\d+)\\/?$/i
let total = 10 / 2 / 5

function render(name) {
  return `<h1>${name.toUpperCase()} ${ `n=${name.length}` }</h1>`
}

async function load(path) {
  const text = await readFile(path, 'utf8')
  return text.split(/\\r?\\n/).filter((l) => l /* keep */)
}

class Cache {
  #items = new Map()
  static count = 0
  get size() { return this.#items.size }
  set(key, value) { this.#items.set(key, value); return this }
  has(key) { return this.#items.has(key) }
}

app.get('/', (req, res) => res.send(render('home')))
app.use(express.json())
const average = (a, b) => (a + b) / 2
module.exports = { render, load }
'''

MJS = b'''import assert from 'node:assert/strict'
import { test } from 'node:test'
import { slug } from './slug.mjs'

const big = 1_000_000n
const re = /[/]\\/+/g

test('slugs', () => {
  assert.equal(slug('A B'), 'a-b')
})

test(`template name`, async () => {
  const tpl = `${[1, 2].map((n) => `<${n}>`).join('')}`
  assert.ok(tpl)
})

// don't "quote" {
export const version = "1.0"
if (version) /1/.test(version)
x
++big
'''

TS_BODY = TS_STYLE.replace(b'console.log(path.join("a", "b"))\n', b"")

JS_BODY = JS.replace(b"module.exports = { render, load }\n", b"")


def kinds(source, suffix=".js"):
    jsx = suffix in (".jsx", ".tsx")
    tokens, _, _ = javascript.Scanner(source, jsx, suffix == ".tsx").scan(0, False)
    return [(k, source[s:e]) for k, s, e, _, _ in tokens]


def swap(blob, old, new):
    assert old in blob, old
    return blob.replace(old, new, 1)


def merged(base, *edits):
    for old, new in edits:
        base = swap(base, old, new)
    return base


SUMMARY = (b"return `${t.id}: ${t.title} (${t.status})`;", b"return `#${t.id} ${t.title}`;")
PENDING = (b"ts.filter((t) => t.status === Status.Active)", b"ts.filter((t) => t.status !== Status.Closed)")
FIND = (b"return this.cache.get(id) ?? (await this.repo.load(id));", b"return this.cache.get(id);")
CLOSE = (b"if (t) t.status = Status.Closed;", b"if (t) t.status = Status.Closed, this.cache.delete(id);")


class TypeScriptReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "src/tickets.ts"
    SAMPLES = (TS, TS_TYPES, TS_STYLE, b"export const x = 1;\n",
               "const Ⅺ = .125;\nfunction café() { return /😀+/u; }\n".encode(),
               b"for await (const v of stream) /item/.test(v);\nconst after = 1;\n")
    KEYS = (
        (0, ["import @angular/core", "import ./repo", "enum Status", "interface Ticket", "type Filter",
             "function summarize", "function pending", "class TicketService"]),
        (1, ["module \"legacy-lib\"", "global", "namespace Geometry.Shapes", "function pick", "class Shape",
             "const { host, port = 8080 },retries", "const handler", "interface Options", "export { host as hostName, port }",
             "export from ./more", "export from ./util", "import legacy-lib", "const a", "const b", "let later"]),
        (2, ["import fs", "import path", "type Matcher", "type Union", "const URL_RE", "const half", "const items",
             "function check", "class Queue", "test adds numbers", "test later"]),
    )
    BROKEN = (
        b"export function f() {\n  return 1;\n",
        b"const s = 'abc\nconst t = 1;\n",
        b"/* never closed\nexport const a = 1;\n",
        b"const t = `abc ${ 1 \nexport const a = 1;\n",
        b"export const re = /abc\nconst a = 1;\n",
        b"export class A {\n  [key: string]: any;\n}\n",
        b"export function f() {}\n}\n",
        b"const a = (1 + 2;\n",
        b"export const a = [1, 2);\n",
        b"export function f(a: number) { return a; }\xff\n",
        b"export @ 1;\n",
    )
    MERGES = (
        # Two adjacent functions edited, one on each side.
        (TS, swap(TS, *SUMMARY), swap(TS, *PENDING), merged(TS, SUMMARY, PENDING)),
        # Both sides add a function, an import and a test at the end: ours first.
        (TS, TS + b"\nexport function alpha() {}\n", TS + b"\nexport function beta() {}\n",
         TS + b"\nexport function alpha() {}\n" + b"\nexport function beta() {}\n"),
        (TS, swap(TS, b"import type { Repo } from './repo';\n", b"import type { Repo } from './repo';\nimport { A } from './a';\n"),
         swap(TS, b"import type { Repo } from './repo';\n", b"import type { Repo } from './repo';\nimport { B } from './b';\n"),
         swap(TS, b"import type { Repo } from './repo';\n",
              b"import type { Repo } from './repo';\nimport { A } from './a';\nimport { B } from './b';\n")),
        (TS_BODY, TS_BODY + b"\ntest(\"alpha\", () => {})\n", TS_BODY + b"\ntest(\"beta\", () => {})\n",
         TS_BODY + b"\ntest(\"alpha\", () => {})\n" + b"\ntest(\"beta\", () => {})\n"),
        # The same function changed on both sides.
        (TS, swap(TS, *SUMMARY), swap(TS, SUMMARY[0], b"return 'other';"), None),
        # An edit inside two different class methods.
        (TS, swap(TS, *FIND), swap(TS, *CLOSE), merged(TS, FIND, CLOSE)),
        # The same method on both sides.
        (TS, swap(TS, *FIND), swap(TS, FIND[0], b"return undefined;"), None),
        # One side edits a function, the other adds an enum member next to a class edit.
        (TS, swap(TS, *SUMMARY), swap(TS, *CLOSE), merged(TS, SUMMARY, CLOSE)),
        # A body edit in an overloaded function and in a namespace function.
        (TS_TYPES, swap(TS_TYPES, b"items[index] ?? (fallback as T)", b"items.at(index) ?? (fallback as T)"),
         swap(TS_TYPES, b"return w * h;", b"return w * h * 1;"),
         merged(TS_TYPES, (b"items[index] ?? (fallback as T)", b"items.at(index) ?? (fallback as T)"),
                (b"return w * h;", b"return w * h * 1;"))),
        # Deleting a function one side left alone.
        (TS, swap(TS, b"export function pending(ts: Ticket[]): Ticket[] {\n  return ts.filter((t) => t.status === Status.Active);\n}\n\n", b""),
         TS, swap(TS, b"export function pending(ts: Ticket[]): Ticket[] {\n  return ts.filter((t) => t.status === Status.Active);\n}\n\n", b"")),
    )

    def outline(self, source, path="a.ts"):
        return readers.outline(path, source)[1][3]

    def texts(self, source, path="a.ts"):
        return [(k, source[s:e].decode()) for k, s, e, _ in self.outline(source, path)]

    def test_overloads_form_one_unit(self):
        nodes = {n[0]: n for n in self.outline(TS_TYPES)}
        _, start, end, _ = nodes["function pick"]
        text = TS_TYPES[start:end].decode()
        self.assertTrue(text.startswith("export function pick<T>(items: T[], index: number): T;"))
        self.assertTrue(text.endswith("return items[index] ?? (fallback as T);\n}\n"))
        self.assertEqual(text.count("export function pick"), 3)

    def test_class_members(self):
        nodes = {n[0]: n for n in self.outline(TS_TYPES)}
        keys = [c[0] for c in nodes["class Shape"][3]]
        self.assertEqual(keys, [
            "method area", "field kind", "field phantom", "field [Symbol.toStringTag]", "static field #instances",
            "get label", "set label", "method stream", "method describe", "static block",
        ])
        keys = [c[0] for c in {n[0]: n for n in self.outline(TS)}["class TicketService"][3]]
        self.assertEqual(keys, ["field cache", "static field LIMIT", "constructor", "method find", "method close"])

    def test_decorator_and_export_belong_to_the_node(self):
        (key, text), = [t for t in self.texts(TS) if t[0] == "class TicketService"]
        self.assertTrue(text.startswith("@Injectable()\nexport class TicketService"))

    def test_namespace_children(self):
        nodes = {n[0]: n for n in self.outline(TS_TYPES)}
        self.assertEqual([c[0] for c in nodes["namespace Geometry.Shapes"][3]], ["const unit", "function area"])
        self.assertEqual([c[0] for c in nodes["module \"legacy-lib\""][3]], ["function parse"])

    def test_two_statements_on_one_line_get_exact_spans(self):
        source = b"const a = 1; const b = 2; foo()\n"
        self.assertEqual(self.texts(source), [("const a", "const a = 1;"), ("const b", "const b = 2;")])

    def test_trailing_comment_keeps_exact_span(self):
        source = b"const a = 1; // note\nfunction f() {} // tail\n"
        self.assertEqual(self.texts(source), [("const a", "const a = 1;"), ("function f", "function f() {}")])

    def test_leading_comments_stay_in_the_gap(self):
        source = b"/** doc */\nexport function f() {}\n"
        self.assertEqual(self.texts(source), [("function f", "export function f() {}\n")])

    def test_expression_statements_are_not_units(self):
        source = b"app.use(x)\nif (a) { b() }\nconst k = 1\nfoo(function inner() {})\n"
        self.assertEqual([k for k, _ in self.texts(source)], ["const k"])

    def test_duplicate_keys_are_numbered(self):
        source = b"import a from './x'\nimport type { B } from './x'\ntest('t', () => {})\ntest('t', () => {})\n"
        self.assertEqual([k for k, _ in self.texts(source)], ["import ./x", "import ./x #2", "test t", "test t #2"])

    def test_asi_stops_at_the_newline_and_continues_on_operators(self):
        source = b"const a = 1\nconst b = a +\n  2\nconst c = b\n  .toString()\nconst d = [\n 1,\n]\nlet e = 5\n"
        self.assertEqual([t[1] for t in self.texts(source)],
                         ["const a = 1\n", "const b = a +\n  2\n", "const c = b\n  .toString()\n",
                          "const d = [\n 1,\n]\n", "let e = 5\n"])

    def test_generic_commas_are_not_declarator_separators(self):
        source = b"const f = <T, U>(a: T, b: U) => a, g = 2;\nconst m: Map<string, number> = new Map(), n = 1;\n"
        self.assertEqual([k for k, _ in self.texts(source)], ["const f,g", "const m,n"])

    def test_non_ascii_text_maps_to_byte_offsets(self):
        source = "const café = 'naïve ☃';\n/* 😀 */ export function größe() {}\nconst z = `é${1}`;\n".encode()
        for key, start, end, _ in self.outline(source):
            self.assertTrue(source[start:end].decode().strip())
        self.assertEqual([k for k, _ in self.texts(source)], ["const café", "function größe", "const z"])
        self.assertEqual(self.texts(source)[1][1], "export function größe() {}\n")

    def test_undecodable_bytes_are_refused(self):
        self.assertIsNone(readers.outline("a.ts", b"const a = '\xe9';\n"))

    def test_hashbang_is_skipped(self):
        source = b"#!/usr/bin/env node\nimport a from 'a'\n"
        self.assertEqual([k for k, _ in self.texts(source, "cli.mjs")], ["import a"])

    def test_ts_never_reads_jsx(self):
        source = b"const a = <number>x;\nconst ok = i < n && n > 1;\nfunction f() { return a < b; }\n"
        self.assertEqual([k for k, _ in self.texts(source)], ["const a", "const ok", "function f"])

    def test_speed_on_a_big_file(self):
        parts = []
        for i in range(900):
            parts.append(
                f"export function f{i}(a: number): number {{\n  const x = a / {i} / 2;\n"
                f"  return `v${{x}}` + 'it\\'s'; // c\n}}\n\nconst v{i} = /a{i}/g;\n"
            )
        source = "".join(parts).encode()
        self.assertGreater(source.count(b"\n"), 5000)
        begun = time.time()
        nodes = self.outline(source)
        self.assertLess(time.time() - begun, 1.0)
        self.assertEqual(len(nodes), 1800)


class TsxReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "src/Counter.tsx"
    SAMPLES = (TSX, b"export const A = () => <div>It's</div>;\n")
    KEYS = (
        (0, ["import react", "import react #2", "type Props", "const List", "const Pair", "function Counter",
             "const Footer", "export default"]),
    )
    BROKEN = (
        b"export const A = () => <div>text;\n",
        b"export const A = () => <div></span>;\n",
        b"export const A = () => <div a='x>;\n",
        b"export const A = () => <div>{1 + </div>;\n",
        b"export const A = () => (\n  <div>\n);\n",
        b"export function A() {\n  return <b/>;\n",
    )
    MERGES = (
        (TSX, swap(TSX, b"const half = n / 2 / 1;", b"const half = n / 2;"),
         swap(TSX, b"<footer>&copy; Company's 2026</footer>", b"<footer>&copy; Company's 2027</footer>"),
         merged(TSX, (b"const half = n / 2 / 1;", b"const half = n / 2;"),
                (b"<footer>&copy; Company's 2026</footer>", b"<footer>&copy; Company's 2027</footer>"))),
        (TSX, TSX + b"\nexport const Alpha = () => <a>Don't</a>;\n", TSX + b"\nexport const Beta = () => <b>Won't</b>;\n",
         TSX + b"\nexport const Alpha = () => <a>Don't</a>;\n" + b"\nexport const Beta = () => <b>Won't</b>;\n"),
        (TSX, swap(TSX, b"<i>few</i>", b"<i>one</i>"), swap(TSX, b"<i>few</i>", b"<i>none</i>"), None),
        (TSX, swap(TSX, b"[props.k, props.v] as const", b"[props.v, props.k] as const"),
         swap(TSX, b"render(i)", b"render(i) ?? null"),
         merged(TSX, (b"[props.k, props.v] as const", b"[props.v, props.k] as const"), (b"render(i)", b"render(i) ?? null"))),
    )

    def kinds(self, source):
        return kinds(source, ".tsx")

    def test_apostrophes_in_jsx_text_do_not_start_strings(self):
        tokens = self.kinds("const a = <p>Don't {x} won't</p>;\nconst b = 1;")
        self.assertEqual([k for k, _ in tokens], ["id", "id", "p", "jsx", "p", "id", "id", "p", "num", "p"])
        self.assertEqual(tokens[3][1], "<p>Don't {x} won't</p>")

    def test_fragments_and_self_closing_tags(self):
        tokens = self.kinds("x = <><A {...p} b='1' c={`t${1}`} /><B></B></>;")
        self.assertEqual(tokens[2], ("jsx", "<><A {...p} b='1' c={`t${1}`} /><B></B></>"))

    def test_jsx_attribute_expressions_are_tokenized(self):
        tokens = self.kinds("x = <A f={() => { return '</A>' }} g={<B/>} h=\"}\" />;")
        self.assertEqual(tokens[-2], ("jsx", "<A f={() => { return '</A>' }} g={<B/>} h=\"}\" />"))

    def test_generic_arrow_is_not_jsx(self):
        tokens = self.kinds("const f = <T,>(x: T) => x;")
        self.assertEqual([v for _, v in tokens][:6], ["const", "f", "=", "<", "T", ","])
        tokens = self.kinds("const f = <T extends object>(x: T) => x / 2 / 1;")
        self.assertNotIn("jsx", [k for k, _ in tokens])

    def test_less_than_after_an_operand_is_not_jsx(self):
        tokens = self.kinds("if (a <b && c> d) { x = a < b }")
        self.assertNotIn("jsx", [k for k, _ in tokens])

    def test_jsx_after_return_and_arrow_and_ternary(self):
        for source in ("function f() { return <a/>; }", "const f = () => <a/>;", "const f = x ? <a/> : <b/>;"):
            self.assertIn("jsx", [k for k, _ in self.kinds(source)], source)

    def test_comment_inside_a_tag(self):
        tokens = self.kinds("x = <A /* c */ b='1'>t</A>;")
        self.assertEqual(tokens[2][0], "jsx")


class JsxReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "components/Panel.jsx"
    SAMPLES = (JSX, b"const A = () => <i>x's</i>\n")
    KEYS = ((0, ["import react", "function Title", "class Panel"]),)
    BROKEN = (
        b"export const A = () => <div>\n",
        b"function A() { return <a b=\"c></a>; }\n",
        b"class A { render() { return <a>; } }\n",
    )
    MERGES = (
        (JSX, swap(JSX, b"document.title = text", b"document.title = text.trim()"),
         swap(JSX, b"<small>Won't {\"you\"} stay?</small>", b"<small>Will {\"you\"} stay?</small>"),
         merged(JSX, (b"document.title = text", b"document.title = text.trim()"),
                (b"<small>Won't {\"you\"} stay?</small>", b"<small>Will {\"you\"} stay?</small>"))),
        (JSX, swap(JSX, b"state = { open: false }", b"state = { open: true }"),
         swap(JSX, b"const { children } = this.props", b"const { children = null } = this.props"),
         merged(JSX, (b"state = { open: false }", b"state = { open: true }"),
                (b"const { children } = this.props", b"const { children = null } = this.props"))),
        (JSX, swap(JSX, b"state = { open: false }", b"state = { open: true }"),
         swap(JSX, b"state = { open: false }", b"state = { open: 1 }"), None),
        (JSX, JSX + b"\nexport const A = () => <a>It's</a>\n", JSX + b"\nexport const B = () => <b>Won't</b>\n",
         JSX + b"\nexport const A = () => <a>It's</a>\n" + b"\nexport const B = () => <b>Won't</b>\n"),
    )

    def test_apostrophe_in_text_keeps_the_component_whole(self):
        nodes = readers.outline(self.PATH, JSX)[1][3]
        self.assertEqual([c[0] for c in nodes[2][3]], ["field state", "field toggle", "method render"])


class JavaScriptReaderTests(ReaderConformance, unittest.TestCase):
    PATH = "server/app.js"
    SAMPLES = (JS, MJS, b"function f() {}\n")
    KEYS = (
        (0, ["import express", "import node:fs/promises", "const app", "const PORT", "const ROUTE", "let total",
             "function render", "function load", "class Cache", "const average"]),
        (1, ["import node:assert/strict", "import node:test", "import ./slug.mjs", "const big", "const re",
             "test slugs", "test template name", "const version"]),
    )
    BROKEN = (
        b"function f() {\n  return `abc ${ x \n}\n",
        b"const s = \"abc\n",
        b"/* open\n",
        b"const a = {\n  b: [1, 2,\n};\n",
        b"const r = /ab[c/;\n",
        b"function f() { return 1; }}\n",
        b"const a = 'x\\\n",
    )
    MERGES = (
        (JS, swap(JS, b"return `<h1>", b"return `<h2>"), swap(JS, b"filter((l) => l /* keep */)", b"filter(Boolean)"),
         merged(JS, (b"return `<h1>", b"return `<h2>"), (b"filter((l) => l /* keep */)", b"filter(Boolean)"))),
        (JS_BODY, JS_BODY + b"\nfunction alpha() {}\n", JS_BODY + b"\nfunction beta() {}\n",
         JS_BODY + b"\nfunction alpha() {}\n" + b"\nfunction beta() {}\n"),
        (JS, swap(JS, b"import { readFile } from 'node:fs/promises'\n", b"import { readFile } from 'node:fs/promises'\nimport os from 'os'\n"),
         swap(JS, b"import { readFile } from 'node:fs/promises'\n", b"import { readFile } from 'node:fs/promises'\nimport vm from 'vm'\n"),
         swap(JS, b"import { readFile } from 'node:fs/promises'\n",
              b"import { readFile } from 'node:fs/promises'\nimport os from 'os'\nimport vm from 'vm'\n")),
        (MJS, MJS.replace(b"if (version)", b"test('two', () => {})\nif (version)"),
         MJS.replace(b"if (version)", b"test('three', () => {})\nif (version)"),
         MJS.replace(b"if (version)", b"test('two', () => {})\ntest('three', () => {})\nif (version)")),
        (JS, swap(JS, b"has(key) { return this.#items.has(key) }", b"has(key) { return this.#items.has(key) === true }"),
         swap(JS, b"get size() { return this.#items.size }", b"get size() { return this.#items.size | 0 }"),
         merged(JS, (b"has(key) { return this.#items.has(key) }", b"has(key) { return this.#items.has(key) === true }"),
                (b"get size() { return this.#items.size }", b"get size() { return this.#items.size | 0 }"))),
        (JS, swap(JS, b"return this", b"return null"), swap(JS, b"return this", b"return undefined"), None),
        (JS, swap(JS, b"(a + b) / 2", b"(a + b) / 4"), swap(JS, b"(a + b) / 2", b"(a + b) / 3"), None),
    )

    def kinds(self, source):
        return kinds(source, ".js")

    def test_division_chains(self):
        self.assertEqual([k for k, _ in self.kinds("a / b / c")], ["id", "p", "id", "p", "id"])
        self.assertEqual([k for k, _ in self.kinds("f(x) / 2 / 1")], ["id", "p", "id", "p", "p", "num", "p", "num"])
        self.assertEqual([k for k, _ in self.kinds("a[0] / b++ / c")], ["id", "p", "num", "p", "p", "id", "p", "p", "id"])

    def test_regex_after_assignment_and_return(self):
        self.assertEqual(self.kinds("x = /re/g;")[2], ("re", "/re/g"))
        self.assertEqual(self.kinds("return /a\\/b[/]/")[1], ("re", "/a\\/b[/]/"))
        for word in ("typeof", "case", "in", "of", "void", "throw", "yield", "await", "delete", "new", "else", "do"):
            self.assertEqual(self.kinds(f"{word} /x/")[1][0], "re", word)

    def test_regex_after_control_parentheses(self):
        self.assertEqual(self.kinds("if (x) /re/.test(y)")[4], ("re", "/re/"))
        self.assertEqual(self.kinds("while (x) /re/.test(y)")[4], ("re", "/re/"))
        self.assertEqual(self.kinds("for (a of b) /re/.test(y)")[6], ("re", "/re/"))
        self.assertEqual(self.kinds("for await (a of b) /re/.exec(y)")[7][0], "re")
        self.assertEqual(self.kinds("(x) / 2 / y")[3], ("p", "/"))

    def test_division_after_closing_call_and_postfix(self):
        self.assertEqual(self.kinds("f()/2/1")[3], ("p", "/"))
        self.assertEqual(self.kinds("i++ / 2 / 1")[1], ("p", "++"))
        self.assertEqual([k for k, _ in self.kinds("i++ / 2 / 1")], ["id", "p", "p", "num", "p", "num"])

    def test_regex_with_slash_in_class_and_flags(self):
        self.assertEqual(self.kinds("x = /[/\\]]+/gi.test(s)")[2], ("re", "/[/\\]]+/gi"))

    def test_templates_nest(self):
        source = "x = `a ${ f(`b ${ '}' } ${ {k: `}`}.k }`) } c`; y"
        tokens = self.kinds(source)
        self.assertEqual(tokens[2], ("tpl", source[4:-3]))
        self.assertEqual(tokens[-1], ("id", "y"))

    def test_template_escapes(self):
        self.assertEqual(self.kinds("x = `a\\`b\\${c}`; y")[2], ("tpl", "`a\\`b\\${c}`"))
        self.assertEqual(self.kinds("x = `$ {`; y")[2], ("tpl", "`$ {`"))

    def test_comments_hide_quotes_and_braces(self):
        tokens = self.kinds("a /* ' \" ` { ( */ b // ' { `\nc /* multi\n } */ d")
        self.assertEqual([v for _, v in tokens], ["a", "b", "c", "d"])

    def test_string_containing_comment_markers(self):
        tokens = self.kinds("a = 'http://x/*y*/'; b = \"// no\"; c = 1 / 2")
        self.assertEqual([k for k, _ in tokens].count("str"), 2)
        self.assertEqual(tokens[-2], ("p", "/"))

    def test_string_escapes_and_line_continuation(self):
        self.assertEqual(self.kinds("a = 'it\\'s'; b = \"say \\\"hi\\\"\"; c = 'x\\\ny'")[-1], ("str", "'x\\\ny'"))

    def test_numbers(self):
        for number in ("1_000", "0xFF_FF", "1.5e-3", ".5", "10n", "0b1010", "1e10", "5."):
            tokens = self.kinds(f"x = {number} / 2")
            self.assertEqual(tokens[2], ("num", number), number)
            self.assertEqual(tokens[3], ("p", "/"), number)

    def test_private_names_and_optional_chaining(self):
        tokens = self.kinds("this.#a?.b ?? c ?.5 : 1")
        self.assertEqual(tokens[2], ("id", "#a"))
        self.assertEqual(tokens[3], ("p", "?."))
        self.assertIn(("p", "?"), tokens)

    def test_jsx_is_plain_operators_in_js(self):
        self.assertEqual([k for k, _ in self.kinds("a = b <c> d")].count("jsx"), 0)

    def test_regex_after_closing_brace_is_division(self):
        self.assertEqual(self.kinds("x = {}\n/ 2 / 1")[4], ("p", "/"))

    def test_asi_hazards(self):
        source = b"const a = 1\nconst b = 2\n;[a, b].forEach(f)\nconst c = a\nconst d = c\n"
        self.assertEqual([k for k, _, _, _ in readers.outline("a.js", source)[1][3]],
                         ["const a", "const b", "const c", "const d"])
        self.assertIsNone(readers.outline("a.js", b"class A {\n  f = () => {}\n  [k]() {}\n}\n"))

    def test_test_helpers_become_units(self):
        source = (b"describe('math', () => {\n  it('adds', () => {})\n})\n"
                  b"it.only(`plain`, () => {})\ntest.each([1])('table %s', () => {})\nbeforeEach(() => {})\n")
        keys = [k for k, _, _, _ in readers.outline("a.test.js", source)[1][3]]
        self.assertEqual(keys, ["test math", "test plain"])

    def test_reader_registration(self):
        for suffix in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"):
            self.assertIs(readers.reader_for("x" + suffix), javascript)
        self.assertEqual(javascript.NAME, "javascript")


if __name__ == "__main__":
    unittest.main()
