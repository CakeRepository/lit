import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from concurrent_merge import js_symbols
from concurrent_merge.symbols import IMPORTS, Modules, declarations, executed, unaffected, digests, lcov_hits
from concurrent_merge.unit_store import cached_units

LIB_V1 = """import { helper } from "./helper"
export function used(a: number) {
  return helper(a) + 1
}
export function unused(a: number) {
  const b = a * 2
  return b
}
export const factory = () => {
  const inner = (x: number) => x * 3
  function alsoInner(x: number) {
    return x - 1
  }
  return { inner, alsoInner }
}
"""


def traced():
    status = Path("/proc/self/status").read_text()
    return any(l.split()[1:] != ["0"] for l in status.splitlines() if l.startswith("TracerPid:"))


class SymbolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def table(self, text, name="lib.ts"):
        (self.root / name).write_text(text)
        return declarations(self.root, [name])[name]

    def test_added_functions_and_comments_change_no_digest(self):
        before = digests(self.table(LIB_V1))
        # lit's reader keeps a property that names a function in its parent's code (Babel moved it
        # into the function's), so the object returned by factory is left alone here: naming `added`
        # in it would change factory's digest, which is the safe direction.
        after = self.table(LIB_V1.replace(
            "  return { inner, alsoInner }",
            "  // a note\n  function added() {\n    return 4\n  }\n  return { inner, alsoInner }",
        ) + "\n/** docs */\nexport function another() {\n  return 5\n}\n")
        changed = {
            n for n, d in before.items()
            if n != IMPORTS and (n not in after["symbols"] or after["symbols"][n]["digest"] != d)
        }
        self.assertEqual(changed, set())
        self.assertIn("factory/added", after["symbols"])
        named = self.table(LIB_V1.replace(
            "  return { inner, alsoInner }",
            "  function added() {\n    return 4\n  }\n  return { inner, alsoInner, added }",
        ))
        self.assertNotEqual(named["symbols"]["factory"]["digest"], before["factory"])

    def test_a_changed_function_blocks_only_the_tests_that_ran_it(self):
        v1 = self.table(LIB_V1)
        v2 = self.table(LIB_V1.replace("return b", "return b + 1"))
        self.assertFalse(unaffected(digests(v1), ["", "unused"], v2))
        self.assertTrue(unaffected(digests(v1), ["", "used"], v2))
        # Top-level code (here an import) counts as run by everyone.
        v3 = self.table(LIB_V1.replace('"./helper"', '"./helper2"'))
        self.assertFalse(unaffected(digests(v1), ["", "used"], v3))
        # A removed property naming a function counts as a change to that function.
        v4 = self.table(LIB_V1.replace("{ inner, alsoInner }", "{ inner }"))
        self.assertFalse(unaffected(digests(v1), ["", "factory", "factory/alsoInner"], v4))
        self.assertTrue(unaffected(digests(v1), ["", "used"], v4))

    def modules(self, loaded=()):
        exists = lambda path: (self.root / path).is_file()
        return Modules(self.root, exists, loaded, {}).ask("lib.ts")

    def test_an_added_import_blocks_only_what_could_see_it(self):
        (self.root / "helper.ts").write_text("export function helper(a: number) {\n  return a\n}\n")
        (self.root / "pure.ts").write_text(
            'import { helper } from "./helper"\nexport const LIMIT = 3\nexport function twice(n: number) {\n'
            '  return helper(n) * 2\n}\n'
        )
        (self.root / "side.ts").write_text('export const seen = []\nglobalThis.flag = true\n')
        v1 = digests(self.table(LIB_V1))
        add = lambda line: self.table(line + "\n" + LIB_V1)
        unused = ["", "unused"]
        # A new import nothing that ran names, from a module that only defines names: reusable.
        pure = add('import { twice } from "./pure"')
        self.assertTrue(unaffected(v1, unused, pure, self.modules()))
        self.assertTrue(unaffected(v1, ["", "used"], pure, self.modules()))
        # Without a way to look at the module, any import change counts.
        self.assertFalse(unaffected(v1, unused, pure))
        # A binding named in code that ran may shadow what it used: rerun.
        self.assertFalse(unaffected(v1, unused, add('import { LIMIT as b } from "./pure"'), self.modules()))
        # A name the module does not export, or a module that does more than define names: rerun.
        self.assertFalse(unaffected(v1, unused, add('import { missing } from "./pure"'), self.modules()))
        side = add('import { seen } from "./side"')
        self.assertFalse(unaffected(v1, unused, side, self.modules()))
        self.assertFalse(unaffected(v1, unused, add('import "./side"'), self.modules()))
        # ...unless the test loaded it anyway.
        self.assertTrue(unaffected(v1, unused, side, self.modules(loaded={"side.ts"})))
        # Packages and unknown files are not looked into.
        self.assertFalse(unaffected(v1, unused, add('import { z } from "zod"'), self.modules()))
        self.assertTrue(unaffected(v1, unused, add('import { join } from "node:path"'), self.modules()))
        # Type-only imports are erased and change nothing.
        self.assertTrue(unaffected(v1, unused, add('import type { Foo } from "./side"')))

    def test_a_removed_import_counts_unless_its_module_only_defines_names(self):
        (self.root / "helper.ts").write_text("export function helper(a: number) {\n  return a\n}\n")
        v1 = digests(self.table(LIB_V1))
        dropped = self.table(LIB_V1.replace('import { helper } from "./helper"', "const helper = (a: number) => a"))
        self.assertFalse(unaffected(v1, ["", "used"], dropped, self.modules()))
        gone = self.table(LIB_V1.replace('import { helper } from "./helper"\n', ""))
        self.assertTrue(unaffected(v1, ["", "unused"], gone, self.modules(loaded={"helper.ts"})))
        (self.root / "helper.ts").write_text("export function helper(a: number) {\n  return a\n}\nconsole.log(1)\n")
        self.assertFalse(unaffected(v1, ["", "unused"], gone, self.modules(loaded={"helper.ts"})))

    def test_records_from_before_imports_were_kept_apart_compare_as_before(self):
        v1 = self.table(LIB_V1)
        legacy = {n: d for n, d in digests(v1).items() if n != IMPORTS}
        legacy[""] = v1["legacy"]
        self.assertTrue(unaffected(legacy, ["", "used"], self.table(LIB_V1)))
        self.assertFalse(unaffected(legacy, ["", "unused"], self.table('import "./x"\n' + LIB_V1), self.modules()))

    def test_coverage_lines_decide_which_functions_ran(self):
        table = self.table(LIB_V1)
        hits = lcov_hits("SF:lib.ts\nDA:2,1\nDA:3,1\nDA:5,0\nDA:6,0\nDA:7,0\nend_of_record\n", "")["lib.ts"]
        self.assertEqual(executed(table, hits), ["", "used"])

    def test_own_code_on_the_first_line_counts_when_its_callback_never_ran(self):
        table = self.table("export function p(xs: number[]) { return xs.map((x) => {\n  return x + 1\n}) }\n")
        self.assertEqual(sorted(table["symbols"]), ["", "p", "p/@0"])
        hits = lcov_hits("SF:lib.ts\nDA:1,1\nDA:2,0\nDA:3,1\nend_of_record\n", "")["lib.ts"]
        self.assertIn("p", executed(table, hits))
        self.assertNotIn("p/@0", executed(table, hits))

    @unittest.skipIf(traced(), "needs no outer tracer")
    def test_cached_units_skips_a_reader_of_a_changed_file_by_function(self):
        if not (shutil.which("strace") and shutil.which("bun")):
            self.skipTest("needs strace and bun, which runs the tests")
        work = self.root / "work"
        work.mkdir()
        git = lambda *a: subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *a],
                                        cwd=work, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (work / "helper.ts").write_text("export const helper = (a: number) => a\n")
        (work / "lib.ts").write_text(LIB_V1)
        (work / "used.test.ts").write_text(
            'import { test, expect } from "bun:test"\nimport { used } from "./lib"\n'
            'test("u", () => { expect(used(1)).toBe(2) })\n')
        (work / "unused.test.ts").write_text(
            'import { test, expect } from "bun:test"\nimport { unused } from "./lib"\n'
            'test("n", () => { expect(unused(2)).toBe(4) })\n')
        git("add", "-A")
        git("commit", "-q", "-m", "one")
        store = self.root / "store"
        run = lambda **kw: cached_units(
            work, "unit", store, ["bun", "test", "{lcov}", "./{}"],
            names=["used.test.ts", "unused.test.ts"], log=os.devnull,
            allow_network=True, symbols=["*.ts"], **kw)
        self.assertEqual(run()["ran"], 2)
        (work / "lib.ts").write_text(LIB_V1.replace("return b", "return b * 2"))
        git("commit", "-qam", "two")
        plan = run(mode="plan")
        self.assertEqual(plan["reusable"], ["used.test.ts"])
        self.assertEqual(plan["reusable_by_function"], 1)
        result = run(mode="enforce")
        self.assertEqual(result["failed"], ["unused.test.ts"])



def table(source, suffix=".ts"):
    return js_symbols.table(source.encode("utf8"), suffix)


def digest_of(source, name="", suffix=".ts"):
    return table(source, suffix)["symbols"][name]["digest"]


class SafetyTests(unittest.TestCase):
    """The digests decide whether a test can be skipped, so anything they cannot tell apart must still differ."""

    def assertDiffer(self, a, b, name="", suffix=".ts"):
        self.assertNotEqual(digest_of(a, name, suffix), digest_of(b, name, suffix))

    def assertSame(self, a, b, name="", suffix=".ts"):
        self.assertEqual(digest_of(a, name, suffix), digest_of(b, name, suffix))

    def test_an_expression_bodied_arrow_stays_in_its_function(self):
        # Babel made `x => x * 3` a symbol of its own; lit keeps it in the parent, so a change to it is the parent's.
        v1 = "export function f(xs) {\n  return xs.map(x => x * 3)\n}\n"
        v2 = v1.replace("* 3", "* 4")
        self.assertDiffer(v1, v2, "f")
        self.assertEqual(sorted(table(v1)["symbols"]), ["", "f"])

    def test_block_bodied_functions_are_cut_out_only_where_certain(self):
        src = (
            "export function outer(list) {\n"
            "  function named(x) {\n    return x\n  }\n"
            "  return list.map(function (x) {\n    return named(x)\n  }).filter((x) => {\n    return x\n  })\n"
            "}\n"
        )
        self.assertEqual(
            sorted(table(src)["symbols"]), ["", "outer", "outer/@0", "outer/@1", "outer/named"])
        # An added named function leaves its parent alone; an added callback does not.
        before = "function p() {\n  run(1)\n}\n"
        named = "function p() {\n  function added() {\n    return 2\n  }\n  run(1)\n}\n"
        callback = "function p() {\n  run(1, () => {\n    return 2\n  })\n}\n"
        self.assertSame(before, named, "p")
        self.assertDiffer(before, callback, "p")

    def test_methods_and_arrow_variables_are_symbols(self):
        src = (
            "class A {\n  run() {\n    return 1\n  }\n  static make() {\n    return 2\n  }\n"
            "  get size() {\n    return 3\n  }\n}\n"
            "const o = {\n  m() {\n    return 4\n  },\n  n: () => {\n    return 5\n  },\n}\n"
            "export const f = async (x: number): Promise<number> => {\n  return x\n}\n"
        )
        self.assertEqual(
            sorted(table(src)["symbols"]),
            ["", "@0", "f", "get size", "m", "run", "static make"],
        )
        changed = src.replace("return 1", "return 10")
        self.assertDiffer(src, changed, "run")
        self.assertSame(src, changed, "")
        self.assertSame(src, changed, "f")

    def test_a_change_inside_a_template_literal_or_regex_changes_the_digest(self):
        v1 = "export function f(x) {\n  return `a ${x} b`\n}\n"
        self.assertDiffer(v1, v1.replace("a $", "c $"), "f")
        self.assertDiffer(v1, v1.replace("${x}", "${x + 1}"), "f")
        r1 = "export function g(s) {\n  return /a+b/.test(s)\n}\n"
        self.assertDiffer(r1, r1.replace("a+b", "a+c"), "g")
        # What looks like a comment inside a string, regex or JSX text is code.
        u1 = 'export const url = "http://example.com/a"\n'
        self.assertDiffer(u1, u1.replace("/a", "/b"))
        x1 = "export const x = /https?:\\/\\/a/\n"
        self.assertDiffer(x1, x1.replace("/a/", "/b/"))
        j1 = "export const el = <p>see // here</p>\n"
        self.assertDiffer(j1, j1.replace("here", "there"), "", ".tsx")
        # JSX attributes are not looked into: a function in one stays in its parent.
        j2 = "export function f() {\n  return <p onClick={() => { go() }}>x</p>\n}\n"
        self.assertDiffer(j2, j2.replace("go()", "stop()"), "f", ".tsx")

    def test_a_line_break_decides_where_a_statement_ends(self):
        self.assertDiffer("function f() {\n  return\n  x\n}\n", "function f() {\n  return x\n}\n", "f")
        self.assertDiffer("let a = 1\n+b\n", "let a = 1 +b\n")
        # Whitespace is part of the digest too (Function.prototype.toString shows it).
        self.assertDiffer("function f() {\n  return 1\n}\n", "function f() {\n    return 1\n}\n", "f")
        # A line break inside a comment still counts as one.
        self.assertDiffer("function f() {\n  return /* a\n b */ x\n}\n", "function f() {\n  return x\n}\n", "f")

    def test_comments_change_nothing(self):
        bare = "function f() {\n  a()\n  b()\n}\n"
        noted = "// top\nfunction f() {\n  a()// then b\n  // a note\n  /* more */\n  b()\n}\n/* end */\n"
        self.assertEqual(table(bare)["symbols"]["f"]["digest"], table(noted)["symbols"]["f"]["digest"])
        self.assertEqual(table(bare)["symbols"][""]["digest"], table(noted)["symbols"][""]["digest"])

    def test_a_file_that_cannot_be_read_with_certainty_gives_none(self):
        for source in (
            "const a = `abc\n",
            'const s = "abc\n',
            "function f( {\n",
            "const r = /abc\n",
            "/* never closed\n",
            "foo(]\n",
            'import x = require("y")\n',
            "import { a b } from './c'\n",
            "import * as from './c'\n",
            "export { a b }\n",
            "export import x = y.z\n",
            "const a = 1   const b = 2\n",
        ):
            self.assertIsNone(table(source), source)
        self.assertIsNone(js_symbols.table(b"const a = '\xff'\n", ".ts"))
        self.assertIsNone(js_symbols.table(b"a\rb\n", ".ts"))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "ok.ts").write_text("export const a = 1\n")
            Path(tmp, "bad.ts").write_bytes(b"const a = '\xff'\n")
            Path(tmp, "open.ts").write_text("const a = `")
            got = declarations(tmp, ["ok.ts", "bad.ts", "open.ts", "missing.ts"])
        self.assertIsNotNone(got["ok.ts"])
        self.assertEqual([got[p] for p in ("bad.ts", "open.ts", "missing.ts")], [None, None, None])

    def test_imports_are_read_exactly(self):
        only_types = table(
            'import type { A } from "./a"\nimport type B from "./b"\nimport type * as C from "./c"\n'
            'export type { D } from "./d"\nexport type X = number\n'
        )
        self.assertEqual((only_types["deps"], only_types["imports"], only_types["exports"]), ([], {}, []))
        self.assertTrue(only_types["pure"])
        # Type specifiers are dropped one by one; the module itself is still loaded.
        mixed = table(
            'import { type A } from "./a"\nimport d, { b as c, type E } from "./m"\n'
            'import * as ns from "./n"\nimport "./p"\n')
        self.assertEqual(mixed["deps"], ["./a", "./m", "./n", "./p"])
        self.assertEqual(mixed["imports"]["./a"]["bindings"], {})
        self.assertEqual(mixed["imports"]["./m"]["bindings"], {"c": "b", "d": "default"})
        self.assertEqual(mixed["imports"]["./n"]["bindings"], {"ns": "*"})
        # An import statement changes the import table and none of the code.
        self.assertSame('import { a } from "./x"\nfoo()\n', 'import { b } from "./y"\nfoo()\n')
        self.assertNotEqual(
            table('import { a } from "./x"\n')["imports"], table('import { a as b } from "./x"\n')["imports"])

    def test_exports_are_never_over_claimed(self):
        shape = table(
            'import { i } from "./i"\nexport * from "./all"\nexport * as ns from "./ns"\nexport { r } from "./r"\n'
            'const local = 1\ninterface Shape { a: number }\nexport { local as renamed, Shape }\n'
            'export default class {}\nexport function fn() {}\nexport const a = 1, b = 2\nexport const { x, y } = o\n'
            'export interface I {}\nexport type T = number\n'
        )
        self.assertEqual(shape["exports"], ["a", "default", "fn", "ns", "renamed"])
        self.assertTrue(shape["star"])
        self.assertEqual(shape["deps"], ["./all", "./i", "./ns", "./r"])
        self.assertTrue(table("export = foo\n")["star"])
        self.assertFalse(table("export const a = 1\n")["star"])

    def test_only_statements_that_define_names_make_a_file_pure(self):
        for source in (
            "export function a() {}\nexport const X = 1\nexport const Y = [1, 'x', null, { k: () => 1, m() {} }]\n",
            "'use strict'\nimport { a } from './a'\nexport { a }\nexport * from './b'\n",
            "export class C extends B {\n  x = foo()\n  static s = 1\n  m() {}\n}\n",
            "export enum E { A = 1, B = 'b', C }\nexport interface I { a: number }\nexport type T = I | null\n",
            "declare const x: number\ndeclare module 'm' { export const a: number }\n",
            "export const f = (x: number): number => x * 2\nexport const m = new Map<string, number>()\n",
            "export const a = 1 as const\nconst b = -1\nlet c\n",
        ):
            self.assertTrue(table(source)["pure"], source)
        for source in (
            "setup()\n",
            "const x = foo()\n",
            "export default foo()\n",
            "export const a = foo\n(bar)\n",
            "const o = { ...p }\n",
            "const o = { [k]: 1 }\n",
            "var a = 1\n",
            "let a = 1\na++\n",
            "export const a = b as any || foo()\n",
            "export enum E { A = 1 << 1 }\n",
            "class A { static y = bar() }\n",
            "class A { [k]() {} }\n",
            "class A extends mixin(B) {}\n",
            "@dec class A {}\n",
            "class A { static { init() } }\n",
            "const m = new Map([[1, 2]])\n",
            "if (a) {}\n",
            "a = 1\n",
            "export = foo\n",
            "namespace N { export const a = 1 }\n",
            "type A = string\n(foo)()\n",
            "function f() {}\nfoo()\n",
            "import('./x')\n",
            "'use strict'\nsetup()\n'late'\n",
        ):
            self.assertFalse(table(source)["pure"], source)

    def test_whole_file_digests_see_every_character_outside_cut_functions(self):
        base = "import { a } from './a'\nexport function f() {\n  return a\n}\nsetup(1)\n"
        for change in ("setup(2)", "setup(1);", "setup( 1)"):
            self.assertNotEqual(
                table(base)["symbols"][""]["digest"], table(base.replace("setup(1)", change))["symbols"][""]["digest"])
        # Records made before imports were kept apart still see them.
        self.assertNotEqual(table(base)["legacy"], table(base.replace("./a", "./b"))["legacy"])
        self.assertEqual(
            table(base)["symbols"][""]["digest"], table(base.replace("./a", "./b"))["symbols"][""]["digest"])


if __name__ == "__main__":
    unittest.main()
