import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from concurrent_merge.symbols import IMPORTS, Modules, declarations, executed, unaffected, digests, lcov_hits
from concurrent_merge.unit_store import cached_units

# @babel/parser is not a lit dependency; borrow one from a checkout that has it, or skip.
PARSER = next((p for p in (
    os.environ.get("LIT_BABEL_PARSER", ""),
    str(Path.cwd() / "node_modules/@babel/parser"),
) if p and Path(p, "package.json").is_file()), None)

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


@unittest.skipUnless(PARSER and shutil.which("bun"), "needs bun and @babel/parser")
class SymbolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        modules = self.root / "node_modules" / "@babel"
        modules.mkdir(parents=True)
        (modules / "parser").symlink_to(PARSER)

    def table(self, text, name="lib.ts"):
        (self.root / name).write_text(text)
        return declarations(self.root, [name])[name]

    def test_added_functions_and_comments_change_no_digest(self):
        before = digests(self.table(LIB_V1))
        after = self.table(LIB_V1.replace(
            "  return { inner, alsoInner }",
            "  // a note\n  function added() {\n    return 4\n  }\n  return { inner, alsoInner, added }",
        ) + "\n/** docs */\nexport function another() {\n  return 5\n}\n")
        changed = {
            n for n, d in before.items()
            if n != IMPORTS and (n not in after["symbols"] or after["symbols"][n]["digest"] != d)
        }
        self.assertEqual(changed, set())
        self.assertIn("factory/added", after["symbols"])

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

    @unittest.skipIf(traced(), "needs no outer tracer")
    def test_cached_units_skips_a_reader_of_a_changed_file_by_function(self):
        if not shutil.which("strace"):
            self.skipTest("needs strace")
        work = self.root / "work"
        work.mkdir()
        git = lambda *a: subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *a],
                                        cwd=work, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        (work / "node_modules").symlink_to(self.root / "node_modules")
        (work / ".gitignore").write_text("node_modules\n")
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


if __name__ == "__main__":
    unittest.main()
