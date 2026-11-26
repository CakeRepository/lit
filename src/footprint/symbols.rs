//! Apply a unit's LCOV evidence only when the code and imports it used are unchanged.
use super::{
    compare::strings,
    glob::Pattern,
    workspace::{self, Workspace},
    Result,
};
use crate::{
    json::{self, Value},
    json_paths as paths, readers,
};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};
type Map = BTreeMap<String, Value>;
const IMPORTS: &str = " imports";
const EXT: &[&str] = &[".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"];
fn textset(v: &Value) -> Result<BTreeSet<String>> {
    Ok(strings(v)?.into_iter().map(str::to_owned).collect())
}
fn arr(v: BTreeSet<String>) -> Value {
    Value::Array(v.into_iter().map(Value::String).collect())
}
fn normal(path: &str) -> String {
    let mut out = Vec::new();
    for p in path.split('/') {
        match p {
            "" | "." => (),
            ".." if !out.is_empty() => {
                out.pop();
            }
            _ => out.push(p),
        }
    }
    out.join("/")
}
fn javascript(p: &str) -> bool {
    EXT.iter().any(|s| p.ends_with(s))
}
fn resolve(work: &Workspace, importer: &str, source: &str) -> Option<String> {
    if !source.starts_with(['.', '/']) {
        return None;
    }
    let parent = importer.rsplit_once('/').map_or("", |(folder, _)| folder);
    let base = if source.starts_with('/') {
        normal(source)
    } else {
        normal(&format!("{parent}/{source}"))
    };
    let mut candidates = [
        "",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        "/index.ts",
        "/index.tsx",
        "/index.js",
        "/index.mjs",
    ]
    .into_iter()
    .map(|end| format!("{base}{end}"))
    .collect::<Vec<_>>();
    for (ext, alts) in [
        (".js", &[".ts", ".tsx"][..]),
        (".jsx", &[".tsx"]),
        (".mjs", &[".mts"]),
        (".cjs", &[".cts"]),
    ] {
        if let Some(stem) = base.strip_suffix(ext) {
            candidates.extend(alts.iter().map(|s| format!("{stem}{s}")));
        }
    }
    candidates.into_iter().find(|s| {
        (javascript(s) || s.ends_with(".json"))
            && work.entries.get(s).is_some_and(|e| e.kind == "blob")
    })
}
#[derive(Default)]
pub struct Tables {
    data: BTreeMap<String, Option<Value>>,
}
impl Tables {
    fn table(&mut self, root: &Path, path: &str) -> Option<&Value> {
        self.data
            .entry(path.into())
            .or_insert_with(|| {
                if !javascript(path) {
                    return None;
                }
                let bytes = fs::read(root.join(workspace::path(path).ok()?)).ok()?;
                let suffix = &path[path.rfind('.')?..];
                let v = readers::symbols::table(&bytes, suffix)?;
                paths::parse(json::pretty(&v).as_bytes()).ok()
            })
            .as_ref()
    }
    fn inert(
        &mut self,
        root: &Path,
        work: &Workspace,
        path: &str,
        loaded: &BTreeSet<String>,
        strict: bool,
    ) -> Result<bool> {
        let mut stack = vec![path.to_owned()];
        let mut seen = BTreeSet::new();
        while let Some(p) = stack.pop() {
            if !seen.insert(p.clone()) || (!strict && loaded.contains(&p)) || p.ends_with(".json") {
                continue;
            }
            let Some(table) = self.table(root, &p) else {
                return Ok(false);
            };
            if !table.field("pure")?.truthy() {
                return Ok(false);
            }
            for dep in strings(table.field("deps")?)? {
                if dep.starts_with("node:") || dep.starts_with("bun:") {
                    continue;
                }
                let Some(target) = resolve(work, &p, dep) else {
                    return Ok(false);
                };
                stack.push(target);
            }
        }
        Ok(true)
    }
    fn harmless(
        &mut self,
        ctx: &Context,
        source: &str,
        added: Option<&BTreeSet<String>>,
    ) -> Result<bool> {
        if source.starts_with("node:") || source.starts_with("bun:") {
            return Ok(true);
        }
        let Some(target) = resolve(ctx.work, ctx.path, source) else {
            return Ok(false);
        };
        if let Some(names) = added.filter(|_| javascript(&target)) {
            let Some(t) = self.table(ctx.root, &target) else {
                return Ok(false);
            };
            if !t.field("star")?.truthy() {
                let exports = textset(t.field("exports")?)?;
                if names.iter().any(|n| n != "*" && !exports.contains(n)) {
                    return Ok(false);
                }
            }
        }
        self.inert(ctx.root, ctx.work, &target, ctx.loaded, added.is_none())
    }
    pub fn unaffected(&mut self, ctx: &Context, recorded: &Value, ran: &Value) -> Result<bool> {
        let Some(current) = self.table(ctx.root, ctx.path) else {
            return Ok(false);
        };
        let symbols = current.field("symbols")?.object()?;
        let before = recorded.get(IMPORTS);
        let mut ran = textset(ran)?;
        ran.insert(String::new());
        for n in &ran {
            let Some(old) = recorded.get(n) else { continue };
            let now = if n.is_empty() && before.is_none() {
                current
                    .get("legacy")
                    .or_else(|| symbols.get("").and_then(|s| s.get("digest")))
            } else {
                symbols.get(n).and_then(|s| s.get("digest"))
            };
            if Some(old) != now {
                return Ok(false);
            }
        }
        let Some(old) = before else { return Ok(true) };
        let after = bindings(current)?;
        let old = old.object()?;
        if *old == after {
            return Ok(true);
        }
        let locals = |m: &Map| -> Result<BTreeMap<String, (String, Value)>> {
            let mut out = BTreeMap::new();
            for (src, b) in m {
                for (local, name) in b.object()? {
                    out.insert(local.clone(), (src.clone(), name.clone()));
                }
            }
            Ok(out)
        };
        let (prev, next) = (locals(old)?, locals(&after)?);
        for name in &ran {
            if let Some(ids) = symbols.get(name).and_then(|s| s.get("ids")) {
                if strings(ids)?
                    .iter()
                    .any(|&local| prev.get(local) != next.get(local))
                {
                    return Ok(false);
                }
            }
        }
        for source in old.keys().chain(after.keys()).collect::<BTreeSet<_>>() {
            if let Some(new) = after.get(source) {
                if old.get(source) == Some(new) {
                    continue;
                }
                let values = |entry: Option<&Value>| -> Result<BTreeSet<String>> {
                    entry
                        .map(|v| {
                            v.object()?
                                .values()
                                .map(|s| s.string().map(str::to_owned))
                                .collect()
                        })
                        .unwrap_or_else(|| Ok(BTreeSet::new()))
                };
                let added = values(Some(new))?
                    .difference(&values(old.get(source))?)
                    .cloned()
                    .collect();
                if !self.harmless(ctx, source, Some(&added))? {
                    return Ok(false);
                }
            } else if !self.harmless(ctx, source, None)? {
                return Ok(false);
            }
        }
        Ok(true)
    }
}
pub struct Context<'a> {
    pub work: &'a Workspace,
    pub root: &'a Path,
    pub path: &'a str,
    pub loaded: &'a BTreeSet<String>,
}
fn bindings(t: &Value) -> Result<Map> {
    t.field("imports")?
        .object()?
        .iter()
        .map(|(k, v)| Ok((k.clone(), v.field("bindings")?.clone())))
        .collect()
}
pub(super) fn digests(t: &Value) -> Result<Value> {
    let mut out: Map = t
        .field("symbols")?
        .object()?
        .iter()
        .map(|(n, s)| Ok((n.clone(), s.field("digest")?.clone())))
        .collect::<Result<_>>()?;
    if t.get("imports").is_some() {
        out.insert(IMPORTS.into(), Value::Object(bindings(t)?));
    }
    Ok(Value::Object(out))
}
type Hits = BTreeMap<i64, i64>;
fn lcov(text: &str, cwd: &str) -> BTreeMap<String, Hits> {
    let mut files = BTreeMap::new();
    let mut current = None;
    for line in text.lines() {
        if let Some(src) = line.strip_prefix("SF:") {
            let src = src.trim();
            let p = if src.starts_with('/') {
                normal(src)
            } else {
                normal(&format!("{cwd}/{src}"))
            };
            current = Some(
                files
                    .entry(paths::path_key(p.as_bytes()))
                    .or_insert_with(Hits::new),
            );
        } else if line == "end_of_record" {
            current = None;
        } else if let (Some(file), Some(data)) = (current.as_mut(), line.strip_prefix("DA:")) {
            let mut parts = data.split(',');
            if let (Some(n), Some(hits)) = (
                parts.next().and_then(|s| s.trim().parse().ok()),
                parts.next().and_then(|s| s.trim().parse::<i64>().ok()),
            ) {
                let sum = file.entry(n).or_default();
                *sum = sum.saturating_add(hits);
            }
        }
    }
    files
}
fn executed(t: &Value, hits: &Hits) -> Result<Value> {
    let symbols = t.field("symbols")?.object()?;
    let range = |s: &Value| -> Result<(i64, i64)> {
        Ok((
            s.field("start")?.number()? as i64,
            s.field("end")?.number()? as i64,
        ))
    };
    let mut ran = BTreeSet::from([String::new()]);
    for (name, s) in symbols.iter().filter(|(n, _)| !n.is_empty()) {
        let (start, end) = range(s)?;
        let prefix = format!("{name}/");
        let nested = symbols
            .iter()
            .filter(|(n, _)| n.strip_prefix(&prefix).is_some_and(|s| !s.contains('/')))
            .map(|(_, s)| range(s))
            .collect::<Result<Vec<_>>>()?;
        let mut interior = false;
        let mut observed = false;
        let mut any = false;
        for (&line, &hits) in hits.range(start..=end) {
            any |= hits != 0;
            if line > start && line < end && !nested.iter().any(|(a, b)| (*a..=*b).contains(&line))
            {
                interior = true;
                observed |= hits != 0;
            }
        }
        if if interior { observed } else { any } {
            ran.insert(name.clone());
        }
    }
    Ok(arr(ran))
}
pub fn records(
    root: &Path,
    cwd: &str,
    bundle: &mut Value,
    coverage: &BTreeMap<String, std::path::PathBuf>,
    patterns: &[Pattern],
) -> Result<Map> {
    let units = bundle.field("units")?.object()?;
    let mut needed = BTreeSet::new();
    for u in units.values() {
        for path in strings(u.field("tracked")?)? {
            if javascript(path) && patterns.iter().any(|p| p.matches(path)) {
                needed.insert(path.to_owned());
            }
        }
    }
    let prefix = format!(
        "{}/",
        paths::path_key(root.as_os_str().as_encoded_bytes()).trim_start_matches('/')
    );
    let mut tables = Tables::default();
    let mut kept = BTreeMap::new();
    for (name, unit) in units {
        if unit.field("exit_code")? != &0u64.into() {
            continue;
        }
        let Some(report) = coverage.get(name) else {
            continue;
        };
        if !report.is_file() {
            continue;
        }
        let data = std::fs::read(report).map_err(|e| e.to_string())?;
        let hits = lcov(&String::from_utf8_lossy(&data), cwd)
            .into_iter()
            .map(|(path, hits)| (path.strip_prefix(&prefix).unwrap_or(&path).to_owned(), hits))
            .collect::<BTreeMap<_, _>>();
        let mut symbols = Map::new();
        for path in strings(unit.field("tracked")?)? {
            if needed.contains(path) {
                if let (Some(lines), Some(t)) = (hits.get(path), tables.table(root, path)) {
                    symbols.insert(path.into(), executed(t, lines)?);
                }
            }
        }
        if !symbols.is_empty() {
            kept.insert(name.clone(), Value::Object(symbols));
        }
    }
    let tracked = bundle.field("tracked")?.object()?;
    let mut versions = Map::new();
    for p in needed {
        if let (Some(t), Some(entry)) = (tables.table(root, &p), tracked.get(&p)) {
            let oid = entry
                .array()?
                .get(1)
                .ok_or("Missing blob identity")?
                .string()?;
            versions.insert(oid.into(), digests(t)?);
        }
    }
    let Value::Object(b) = bundle else {
        return Err("Expected unit footprint bundle".into());
    };
    let Some(Value::Object(units)) = b.get_mut("units") else {
        return Err("Expected footprint units".into());
    };
    for (n, s) in kept {
        if let Some(Value::Object(u)) = units.get_mut(&n) {
            u.insert("symbols".into(), s);
        }
    }
    Ok(versions)
}
