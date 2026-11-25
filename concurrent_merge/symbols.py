"""Per-function footprints for JavaScript and TypeScript sources.

A test that reads a large shared module (a db.ts that hundreds of tests import)
usually runs only a few of its functions. For files recorded with symbols, lit
keeps per file version a digest of each function's own code (js_symbols.py, on
lit's own JavaScript and TypeScript tokenizer: the code of the functions inside
a function is cut out where its extent is certain, as are comments; the file's
top-level code is the symbol ""), and per test the functions that ran (from the
test's own line coverage).

A later version of the file still lets the test be skipped when no symbol that
ran in the test (the file's top-level code always counts as run) changed or
was removed. Added functions never block a skip: an unchanged test can only
reach new code through code that did change. A file that cannot be read with
certainty, or that has no coverage for the test, falls back to the whole-file
rule.

Imports are kept apart from the top-level code. A changed import still lets the
test be skipped when no local name whose binding changed appears in the code of
a symbol that ran, every name newly imported is exported by its module, and
every module the file starts or stops loading either was already loaded by the
test or does nothing when loaded but define names (and loads only such modules
itself). Records made before imports were kept apart compare the top-level code
with its imports, as they always did.
"""

from fnmatch import fnmatchcase
from pathlib import Path

from . import js_symbols

SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
IMPORTS = " imports"  # no symbol name holds a space


def wanted(path, patterns):
    return path.endswith(SUFFIXES) and any(fnmatchcase(path, p) for p in patterns)


def declarations(workdir, paths):
    """{repo path: table or None} for files under workdir, read in this process.

    A file that cannot be read, or that the reader is not sure about, is None.
    """
    out = {}
    for path in sorted(set(paths)):
        try:
            blob = (Path(workdir) / path).read_bytes()
        except OSError:
            out[path] = None
            continue
        out[path] = js_symbols.table(blob, Path(path).suffix.lower())
    return out


def lcov_hits(text, root):
    """{repo path: {line: hits}} from an lcov report whose SF paths are relative to root."""
    files, current = {}, None
    for line in text.splitlines():
        if line.startswith("SF:"):
            source = line[3:].strip()
            path = Path(source)
            if path.is_absolute():
                current = str(path)
            else:
                current = str(Path(root) / path)
            current = _normal(current)
            files.setdefault(current, {})
        elif line.startswith("DA:") and current is not None:
            number, _, hits = line[3:].partition(",")
            try:
                files[current][int(number)] = files[current].get(int(number), 0) + int(hits.split(",")[0])
            except ValueError:
                pass
        elif line == "end_of_record":
            current = None
    return files


def _normal(path):
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts:
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def executed(table, hits):
    """Names of the table's symbols that ran, given {line: hits} for that file.

    A function ran when one of its own lines strictly inside it ran (lines
    inside a function nested in it belong to that one). One with no such
    instrumented line counts as run when any of its lines ran: conservative
    for one-line arrow functions, whose line runs when they are created, and
    for a function whose own code shares its first line with a callback. The
    file's top-level code always counts as run.
    """
    symbols = table["symbols"]
    ran = [""]
    for name, f in symbols.items():
        if name == "":
            continue
        nested = [
            (c["start"], c["end"]) for n, c in symbols.items()
            if n.startswith(name + "/") and "/" not in n[len(name) + 1:]
        ]
        own = [
            hits[n] for n in range(f["start"] + 1, f["end"])
            if n in hits and not any(a <= n <= b for a, b in nested)
        ]
        if any(own) if own else any(hits.get(n, 0) for n in range(f["start"], f["end"] + 1)):
            ran.append(name)
    return sorted(ran)


def digests(table):
    out = {n: f["digest"] for n, f in sorted(table["symbols"].items())}
    if "imports" in table:
        out[IMPORTS] = {src: i["bindings"] for src, i in sorted(table["imports"].items())}
    return out


def _bindings(imports):
    return {local: (src, name) for src, b in imports.items() for local, name in b.items()}


def unaffected(recorded, ran, current, modules=None):
    """True when a unit that ran only `ran` of the version digested in `recorded` cannot see `current`.

    modules(source, kind, names) answers for a source the file starts loading
    (kind "added", with the names now imported from it) or stops loading
    ("removed"); without it any change to the file's imports counts.
    """
    if recorded is None or not current:
        return False
    after = current["symbols"]
    before = recorded.get(IMPORTS)
    changed = set()
    for n, d in recorded.items():
        if n == IMPORTS:
            continue
        if n == "" and before is None:
            now = current.get("legacy", after.get("", {}).get("digest"))
        else:
            now = after[n]["digest"] if n in after else None
        if now != d:
            changed.add(n)
    ran = set(ran) | {""}
    if changed & ran:
        return False
    if before is None:
        return True
    now = {src: i["bindings"] for src, i in (current.get("imports") or {}).items()}
    if now == before:
        return True
    if modules is None:
        return False
    old, new = _bindings(before), _bindings(now)
    rebound = {local for local in set(old) | set(new) if old.get(local) != new.get(local)}
    used = set().union(*(after[n].get("ids", ()) for n in ran if n in after))
    if rebound & used:
        return False
    for src in sorted(set(before) | set(now)):
        if src not in now:
            if not modules(src, "removed", ()):
                return False
        elif src not in before or now[src] != before[src]:
            if not modules(src, "added", sorted(set(now[src].values()) - set((before.get(src) or {}).values()))):
                return False
    return True


RESOLVE = ("", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs",
           "/index.ts", "/index.tsx", "/index.js", "/index.mjs")
JS_TO_TS = {".js": (".ts", ".tsx"), ".mjs": (".mts",), ".cjs": (".cts",), ".jsx": (".tsx",)}


def resolve(importer, source, exists):
    """Repo path a relative import names, or None when it is not relative or not found."""
    if not source.startswith((".", "/")):
        return None
    base = _normal(str(Path(importer).parent / source)) if source.startswith(".") else _normal(source)
    candidates = [base + ext for ext in RESOLVE]
    for ext, alternatives in JS_TO_TS.items():
        if base.endswith(ext):
            candidates += [base[: -len(ext)] + alt for alt in alternatives]
    for candidate in candidates:
        if candidate.endswith(SUFFIXES + (".json",)) and exists(candidate):
            return candidate
    return None


class Modules:
    """Answers unaffected()'s module questions for one unit, from the checkout as it is now.

    A module counts as harmless to start loading when it was already loaded by
    the unit, or when it only defines names and everything it loads is harmless
    in the same way. One the file stops loading must only define names itself,
    as must everything it loads. Builtins (node:, bun:) count as harmless; packages and path
    aliases do not, unless the unit already loaded them.
    """

    def __init__(self, workdir, exists, loaded, cache):
        self.workdir, self.exists, self.loaded, self.cache = workdir, exists, set(loaded), cache

    def table(self, path):
        if path not in self.cache:
            self.cache.update(declarations(self.workdir, [path]) if path.endswith(SUFFIXES) else {path: None})
        return self.cache.get(path)

    def inert(self, path, seen, strict=False):
        """True when loading path does nothing but define names. Unless strict, a module the unit
        already loaded counts too: starting to load it again changes nothing."""
        if path in seen or (not strict and path in self.loaded):
            return True
        seen.add(path)
        if path.endswith(".json"):
            return True
        table = self.table(path)
        if not table or not table.get("pure"):
            return False
        for dep in table.get("deps", ()):
            if dep.startswith(("node:", "bun:")):
                continue
            target = resolve(path, dep, self.exists)
            if target is None or not self.inert(target, seen, strict):
                return False
        return True

    def ask(self, importer):
        def answer(source, kind, names):
            if source.startswith(("node:", "bun:")):
                return True
            target = resolve(importer, source, self.exists)
            if target is None:
                return False
            if kind == "added" and target.endswith(SUFFIXES):
                table = self.table(target)
                exported = set(table.get("exports", ())) if table else set()
                if not table or (not table.get("star") and set(names) - exported - {"*"}):
                    return False
            # A module the file stops loading may be loaded by nobody else now, so its own effects count.
            return self.inert(target, set(), strict=kind == "removed")
        return answer


def unit_records(workdir, cwd, units, coverage, patterns, tracked):
    """({unit: {path: [symbols that ran]}}, {oid: digests}) for passing units, from each unit's lcov report."""
    workdir = Path(workdir)
    root = _normal(cwd or "")
    needed = sorted({p for u in units.values() for p in u["tracked"] if wanted(p, patterns)})
    tables = declarations(workdir, needed)
    prefix = str(workdir).lstrip("/") + "/"
    out = {}
    for name, unit in units.items():
        report = coverage.get(name)
        if unit["exit_code"] != 0 or not report or not Path(report).is_file():
            continue
        hits = {}
        for path, lines in lcov_hits(Path(report).read_text(encoding="utf8", errors="replace"), root).items():
            hits[path[len(prefix):] if path.startswith(prefix) else path] = lines
        kept = {
            p: executed(tables[p], hits[p]) for p in unit["tracked"]
            if tables.get(p) and p in hits
        }
        if kept:
            out[name] = kept
    versions = {tracked[p][1]: digests(t) for p, t in tables.items() if t and p in tracked}
    return out, versions
