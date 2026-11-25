// Function-level digests of JavaScript and TypeScript files, for lit's per-function test selection.
// Usage: bun ts_decls.mjs FILE... (paths relative to the current directory; @babel/parser must resolve from
// there, or from a directory listed in LIT_PARSER_PATHS, separated like PATH). Prints {file: {symbols: {name: {digest, start, end}}} or null when the file cannot be parsed}.
//
// Every function in the file is a symbol, named by its path ("createDatabase/getAgent", anonymous ones by
// position, "createDatabase/@3"). A symbol's digest covers its own code only: the code of the functions
// inside it is cut out (a named one entirely, so adding a named function changes no other digest; an anonymous
// one is replaced by a marker, so adding a callback does). The file itself is the symbol "", whose digest
// covers all top-level code except functions, type-only declarations and imports. Comments are cut out everywhere.
// Lines are 1-based.
//
// Imports are recorded apart, per source: {source: {digest, bindings: {local: imported name}}}, so lit can tell an import that
// only adds a binding nobody that ran uses from a change to the file's top-level code. "legacy" is the digest the
// top-level code had when imports were part of it, for records made before. Each symbol also lists the
// identifiers its own code names ("ids"), and the file says what it exports, which sources it loads ("deps"),
// and whether loading it can do anything but define names ("pure").
import { createHash } from "node:crypto"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import path from "node:path"

function loadParser(files) {
  const extra = (process.env.LIT_PARSER_PATHS ?? "").split(path.delimiter).filter(Boolean).map((d) => path.resolve(d))
  const bases = [process.cwd(), ...files.map((f) => path.dirname(path.resolve(f))), ...extra]
  for (const base of bases) {
    try {
      return createRequire(path.join(base, "noop.js"))("@babel/parser")
    } catch {}
  }
  return null
}

const FUNCTIONS = new Set([
  "FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression",
  "ObjectMethod", "ClassMethod", "ClassPrivateMethod",
])
const TYPE_ONLY = new Set(["TSInterfaceDeclaration", "TSTypeAliasDeclaration", "TSDeclareFunction", "TSDeclareMethod"])
const SKIP = new Set(["loc", "start", "end", "extra", "leadingComments", "trailingComments", "innerComments", "range"])
const digest = (text) => createHash("sha256").update(text).digest("hex").slice(0, 32)

function keyName(node) {
  const key = node.key
  if (!key) return null
  const base = key.type === "Identifier" || key.type === "PrivateName" && key.id ? (key.name ?? "#" + key.id.name)
    : key.type === "StringLiteral" || key.type === "NumericLiteral" ? String(key.value) : null
  if (base === null) return null
  return (node.kind === "get" || node.kind === "set" ? node.kind + " " : "") + (node.static ? "static " : "") + base
}

// A named, order-independent binding whose whole text can be cut out of its parent: [name, function node].
function namedUnit(node) {
  let inner = node
  if (node.type === "ExportNamedDeclaration" && node.declaration) inner = node.declaration
  if (node.type === "ExportDefaultDeclaration") {
    const d = node.declaration
    if (FUNCTIONS.has(d.type)) return ["default", d]
    return null
  }
  if (inner.type === "FunctionDeclaration" && inner.id) return [inner.id.name, inner]
  if (inner.type === "VariableDeclaration" && inner.kind === "const" && inner.declarations.length === 1) {
    const d = inner.declarations[0]
    if (d.id.type === "Identifier" && d.init && FUNCTIONS.has(d.init.type)) return [d.id.name, d.init]
  }
  if (inner.type === "ObjectMethod" || inner.type === "ClassMethod" || inner.type === "ClassPrivateMethod") {
    const name = keyName(inner)
    if (name !== null && !inner.computed) return [name, inner]
  }
  if ((inner.type === "ObjectProperty" || inner.type === "ClassProperty" || inner.type === "ClassPrivateProperty")
      && !inner.computed && inner.value && FUNCTIONS.has(inner.value.type)) {
    const name = keyName(inner)
    if (name !== null) return [name, inner.value]
  }
  return null
}

function symbolsOf(text, ast) {
  const comments = (ast.comments ?? []).map((c) => [c.start, c.end])
  const symbols = Object.create(null)
  const add = (name, code, fn, ids) => {
    const prior = symbols[name]
    const start = fn ? fn.loc.start.line : 1, end = fn ? fn.loc.end.line : text.split("\n").length
    symbols[name] = prior
      ? { code: prior.code + "\n\u0000\n" + code, start: Math.min(prior.start, start), end: Math.max(prior.end, end),
          ids: new Set([...prior.ids, ...ids]) }
      : { code, start, end, ids }
  }
  let legacy = null
  const imports = Object.create(null)
  // Type-only imports are erased. Every other one loads its source; its value bindings are kept per source.
  function recordImport(node) {
    if (node.importKind === "type") return
    const source = node.source.value
    const entry = imports[source] ??= { bindings: Object.create(null) }
    for (const spec of node.specifiers) {
      if (spec.importKind === "type") continue
      const imported = spec.type === "ImportDefaultSpecifier" ? "default"
        : spec.type === "ImportNamespaceSpecifier" ? "*"
        : spec.imported.type === "StringLiteral" ? spec.imported.value : spec.imported.name
      entry.bindings[spec.local.name] = imported
    }
  }
  // The code of `container` (a function node, or the program) with its functions and comments cut out.
  // Each function found is visited as a symbol of its own.
  function visit(container, name) {
    const cuts = [], refs = [], named = new Set(), ids = new Set(), importCuts = []
    let anonymous = 0
    const walk = (node, top) => {
      if (!node || typeof node.type !== "string") return
      if (top && TYPE_ONLY.has(node.type)) { cuts.push([node.start, node.end, ""]); return }
      if (top && node.type === "ImportDeclaration") {
        recordImport(node)
        importCuts.push([node.start, trail(node.end), ""])
        return
      }
      if (node.type === "Identifier" || node.type === "JSXIdentifier") ids.add(node.name)
      if (top && node.type === "ExportNamedDeclaration" && node.declaration && TYPE_ONLY.has(node.declaration.type)) {
        cuts.push([node.start, node.end, ""]); return
      }
      const unit = namedUnit(node)
      if (unit) {
        cuts.push([node.start, trail(node.end), ""])
        named.add(unit[0])
        visit(unit[1], name ? name + "/" + unit[0] : unit[0])
        return
      }
      // `{ getAgent }` or `{ getAgent: getAgent }` naming a function of this container: it belongs with that
      // function (decided below, once every function of the container is known).
      if (node.type === "ObjectProperty" && !node.computed && node.value?.type === "Identifier" && keyName(node) !== null) {
        refs.push([node, node.value.name])
        return
      }
      if (FUNCTIONS.has(node.type)) {
        const child = (name ? name + "/" : "") + "@" + anonymous++
        cuts.push([node.start, node.end, "ƒ"])
        visit(node, child)
        return
      }
      for (const key of Object.keys(node)) {
        if (SKIP.has(key)) continue
        const value = node[key]
        if (Array.isArray(value)) for (const v of value) walk(v, false)
        else if (value && typeof value === "object") walk(value, false)
      }
    }
    if (container.type === "Program") {
      for (const statement of container.body) walk(statement, true)
    } else {
      // The function's own parameters and body: walk its children, not the function itself.
      for (const key of ["params", "body", "returnType", "typeParameters", "key", "decorators"]) {
        const value = container[key]
        if (Array.isArray(value)) for (const v of value) walk(v, false)
        else if (value && typeof value === "object") walk(value, false)
      }
    }
    for (const [node, ref] of refs) {
      if (named.has(ref)) {
        cuts.push([node.start, trail(node.end), ""])
        extra.push([name ? name + "/" + ref : ref, text.slice(node.start, node.end)])
      }
    }
    for (const [s, e] of comments) if (s >= container.start && e <= container.end) cuts.push([s, trail(e), ""])
    const apply = (list) => {
      list = [...list].sort((a, b) => a[0] - b[0] || b[1] - a[1])
      let code = "", at = container.start
      for (const [s, e, mark] of list) {
        if (s < at) continue
        code += text.slice(at, s) + mark
        at = e
      }
      return code + text.slice(at, container.end)
    }
    if (container.type === "Program") legacy = digest(apply(cuts))
    add(name, apply([...cuts, ...importCuts]), container.type === "Program" ? null : container, ids)
  }
  // A cut also takes the separator and blank space after it, so adding or removing a cut-out function
  // leaves its parent's code exactly as it was.
  const trail = (end) => {
    const m = /^\s*,?\s*/.exec(text.slice(end, end + 200))
    return end + (m ? m[0].length : 0)
  }
  const extra = []
  visit(ast.program, "")
  for (const [n, code] of extra) if (symbols[n]) symbols[n].code += "\n\u0000ref " + code
  const out = Object.create(null)
  for (const [n, s] of Object.entries(symbols)) {
    out[n] = { digest: digest(s.code), start: s.start, end: s.end, ids: [...s.ids].sort() }
  }
  const importTable = Object.create(null)
  for (const [source, e] of Object.entries(imports)) {
    const bindings = Object.fromEntries(Object.entries(e.bindings).sort())
    importTable[source] = { digest: digest(JSON.stringify(bindings)), bindings }
  }
  return { symbols: out, imports: importTable, legacy, ...moduleShape(ast.program) }
}

// What loading the file does beyond defining names. pure: every top-level statement only declares (imports,
// functions, classes without static code, type declarations, and const or let bound to a function, a class, a
// literal, or an object or array of those). deps: every source it loads. exports: the names it exports, with
// star true when it re-exports a whole module or uses `export =`.
const LITERALS = new Set(["StringLiteral", "NumericLiteral", "BooleanLiteral", "NullLiteral", "BigIntLiteral", "RegExpLiteral"])
function inert(node) {
  if (!node) return true
  if (LITERALS.has(node.type) || FUNCTIONS.has(node.type) || node.type === "Identifier") return true
  if (node.type === "TemplateLiteral") return node.expressions.length === 0
  if (node.type === "UnaryExpression" && node.operator === "-") return inert(node.argument)
  if (node.type === "TSAsExpression" || node.type === "TSSatisfiesExpression" || node.type === "TSNonNullExpression"
      || node.type === "TSTypeAssertion") return inert(node.expression)
  if (node.type === "ArrayExpression") return node.elements.every((e) => e && e.type !== "SpreadElement" && inert(e))
  if (node.type === "ObjectExpression") {
    return node.properties.every((p) => (p.type === "ObjectMethod" && !p.computed)
      || (p.type === "ObjectProperty" && !p.computed && inert(p.value)))
  }
  if (node.type === "ClassExpression") return inertClass(node)
  if (node.type === "NewExpression" && node.callee.type === "Identifier" && node.arguments.length === 0) {
    return ["Map", "Set", "WeakMap", "WeakSet"].includes(node.callee.name)
  }
  return false
}
function inertClass(node) {
  if (node.decorators?.length || (node.superClass && node.superClass.type !== "Identifier")) return false
  return node.body.body.every((m) => {
    if (m.type === "StaticBlock") return false
    if (m.computed || m.decorators?.length) return false
    if ((m.type === "ClassProperty" || m.type === "ClassPrivateProperty") && m.static) return inert(m.value)
    return true
  })
}
function inertStatement(node) {
  if (node.type === "ExportNamedDeclaration") return !node.declaration || inertStatement(node.declaration)
  if (node.type === "ExportDefaultDeclaration") {
    const d = node.declaration
    return d.type === "ClassDeclaration" ? inertClass(d) : FUNCTIONS.has(d.type) || TYPE_ONLY.has(d.type) || inert(d)
  }
  if (node.type === "ImportDeclaration" || node.type === "ExportAllDeclaration" || node.type === "EmptyStatement") return true
  if (TYPE_ONLY.has(node.type) || node.type === "FunctionDeclaration") return true
  if (node.type === "TSModuleDeclaration") return Boolean(node.declare)
  if (node.type === "TSEnumDeclaration") return node.members.every((m) => inert(m.initializer))
  if (node.type === "ClassDeclaration") return inertClass(node)
  if (node.type === "VariableDeclaration") {
    return node.kind !== "var" && node.declarations.every((d) => d.id.type === "Identifier" && inert(d.init))
  }
  return false
}
function moduleShape(program) {
  const deps = new Set(), exports = new Set()
  let star = false
  for (const node of program.body) {
    if (node.type === "ImportDeclaration" && node.importKind !== "type") deps.add(node.source.value)
    if ((node.type === "ExportNamedDeclaration" || node.type === "ExportAllDeclaration") && node.source
        && node.exportKind !== "type") deps.add(node.source.value)
    if (node.type === "ExportAllDeclaration") {
      if (node.exported) exports.add(node.exported.name ?? node.exported.value)
      else star = true
    }
    if (node.type === "TSExportAssignment") star = true
    if (node.type === "ExportDefaultDeclaration") exports.add("default")
    if (node.type === "ExportNamedDeclaration") {
      for (const spec of node.specifiers ?? []) {
        const e = spec.exported
        exports.add(e.type === "StringLiteral" ? e.value : e.name)
      }
      const d = node.declaration
      if (d?.id?.name) exports.add(d.id.name)
      if (d?.type === "VariableDeclaration") {
        for (const v of d.declarations) if (v.id.type === "Identifier") exports.add(v.id.name)
          else star = true
      }
    }
  }
  return {
    pure: program.body.every(inertStatement),
    deps: [...deps].sort(),
    exports: [...exports].sort(),
    star,
  }
}

const files = process.argv.slice(2)
const babel = loadParser(files)
const out = {}
for (const file of files) {
  try {
    const text = readFileSync(file, "utf8")
    const plugins = ["typescript", "decorators-legacy", "importAttributes", "explicitResourceManagement"]
    if (/\.(tsx|jsx|js|mjs|cjs)$/.test(file)) plugins.push("jsx")
    const ast = babel.parse(text, { sourceType: "module", plugins, errorRecovery: false })
    out[file] = symbolsOf(text, ast)
  } catch {
    out[file] = null
  }
}
process.stdout.write(JSON.stringify(out))
