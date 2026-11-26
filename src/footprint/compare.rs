use super::{
    glob::Pattern,
    workspace::{self, Workspace},
    Result,
};
use crate::{
    json::{object, Value},
    json_paths as paths, sha256,
};
use std::collections::{BTreeMap, BTreeSet};

pub fn strings(v: &Value) -> Result<Vec<&str>> {
    v.array()?.iter().map(Value::string).collect()
}
fn items(v: &Value) -> Result<Vec<(&String, &Value)>> {
    let mut out = v.object()?.iter().collect::<Vec<_>>();
    out.sort_unstable_by(|a, b| paths::compare(a.0, b.0));
    Ok(out)
}
pub fn reason(kind: &str, key: &str, v: Value) -> Value {
    object([("kind", kind.into()), (key, v)])
}
fn path_reason(kind: &str, name: &str) -> Value {
    reason(kind, "path", name.into())
}
pub fn environment(ignore: &[Pattern]) -> BTreeMap<String, Value> {
    std::env::vars_os()
        .filter_map(|(k, v)| {
            let name = paths::path_key(k.as_encoded_bytes());
            (!ignore.iter().any(|p| p.matches(&name)))
                .then(|| (name, Value::String(sha256::hex(v.as_encoded_bytes()))))
        })
        .collect()
}
pub fn environment_changes(v: &Value) -> Result<Vec<Value>> {
    filtered_environment_changes(v, &[])
}
pub fn filtered_environment_changes(v: &Value, extra: &[Pattern]) -> Result<Vec<Value>> {
    let ignore = strings(v.field("ignored_environment")?)?
        .into_iter()
        .map(Pattern::new)
        .collect::<Vec<_>>();
    let mut now = environment(&ignore);
    now.retain(|k, _| !extra.iter().any(|p| p.matches(k)));
    let prior = v.field("environment")?.object()?;
    let mut names = now.keys().chain(prior.keys()).collect::<Vec<_>>();
    names.sort_unstable_by(|a, b| paths::compare(a, b));
    names.dedup();
    Ok(names
        .into_iter()
        .filter(|s| !extra.iter().any(|p| p.matches(s)) && now.get(*s) != prior.get(*s))
        .map(|s| reason("environment", "name", s.as_str().into()))
        .collect())
}

pub fn stem(name: &str) -> &str {
    name.split('.')
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or(name)
}
pub fn resolution_key(name: &str) -> Option<&str> {
    for suffix in [
        ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".json", ".node", ".d.ts",
    ] {
        if let Some(s) = name.strip_suffix(suffix).filter(|s| !s.is_empty()) {
            return Some(s);
        }
    }
    if [
        ".tsbuildinfo",
        ".log",
        ".map",
        ".lock",
        ".orig",
        ".rej",
        ".swp",
        ".tmp",
        ".md",
    ]
    .iter()
    .any(|s| name.ends_with(s))
    {
        None
    } else {
        Some(stem(name))
    }
}
pub fn under(p: &str, dir: &str) -> bool {
    p == dir
        || p.strip_prefix(dir.trim_end_matches('/'))
            .is_some_and(|s| s.starts_with('/'))
}
pub const MACHINE_LOCAL: [&str; 4] = [
    "/etc/hosts",
    "/etc/hostname",
    "/etc/resolv.conf",
    "/var/cache/fontconfig",
];
fn listing_matters(
    v: &Value,
    bundle: &Value,
    dir: &str,
    kids: Option<&Vec<String>>,
) -> Result<bool> {
    let nested = |key: &str| v.get(key).and_then(|m| m.get(dir));
    let Some(before) = bundle.get("listing_children").and_then(|v| v.get(dir)) else {
        return Ok(true);
    };
    let keys = nested("listing_keys");
    let Some(lookups) = keys.or_else(|| nested("listing_lookups")) else {
        return Ok(true);
    };
    if let Some(enumerated) = v.get("enumerated") {
        if strings(enumerated)?.contains(&dir) {
            return Ok(true);
        }
    }
    let before = strings(before)?.into_iter().collect::<BTreeSet<_>>();
    let after = kids
        .into_iter()
        .flatten()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    let lookups = strings(lookups)?.into_iter().collect::<BTreeSet<_>>();
    for &name in before.symmetric_difference(&after) {
        if matches!(
            name,
            "package.json"
                | "tsconfig.json"
                | "jsconfig.json"
                | "node_modules"
                | "__init__.py"
                | "conftest.py"
        ) {
            return Ok(true);
        }
        let k = resolution_key(name);
        if keys.is_some() {
            if lookups.contains(name) || k.is_some_and(|s| lookups.contains(s)) {
                return Ok(true);
            }
        } else if k.is_some() && lookups.contains(stem(name)) {
            return Ok(true);
        }
    }
    Ok(false)
}
pub struct Options {
    pub env: bool,
    pub host: bool,
    pub allow_network: bool,
    pub names: bool,
}
pub fn compare(v: &Value, work: &mut Workspace, opt: &Options) -> Result<Vec<Value>> {
    compare_unit(v, v, work, opt)
}
pub fn compare_unit(
    v: &Value,
    bundle: &Value,
    work: &mut Workspace,
    opt: &Options,
) -> Result<Vec<Value>> {
    let mut out = Vec::new();
    for d in v.field("incomplete")?.array()? {
        out.push(reason("incomplete", "detail", d.clone()));
    }
    if v.field("network")?.truthy() && !opt.allow_network {
        out.push(reason(
            "network",
            "detail",
            "the job opened network connections".into(),
        ));
    }
    let different = v.field("commit")?.string()? != work.commit;
    if v.field("reads_git_metadata")?.truthy() && different {
        let meta = v
            .get("git_metadata")
            .filter(|x| x.truthy())
            .map(Value::array)
            .transpose()?
            .unwrap_or_default();
        out.push(object([
            ("kind", "revision".into()),
            (
                "detail",
                "the job read Git metadata of its own checkout".into(),
            ),
            (
                "paths",
                Value::Array(meta.iter().take(5).cloned().collect()),
            ),
        ]));
    }
    if let Some(fields) = v.get("git_static").filter(|x| x.truthy()) {
        for (name, d) in items(fields)? {
            if let Some(root) = &work.workdir {
                if workspace::digest(&root.join(workspace::path(name)?)) != d.string()? {
                    out.push(path_reason("changed", name));
                }
            } else if different {
                out.push(object([
                    ("kind", "revision".into()),
                    (
                        "detail",
                        "the job looked at the checkout's Git directory".into(),
                    ),
                    ("paths", Value::Array(vec![name.as_str().into()])),
                ]));
                break;
            }
        }
    }
    for (name, expected) in items(v.field("tracked")?)? {
        let [mode, oid] = expected.array()? else {
            return Err("Expected tracked mode and object ID".into());
        };
        if !work
            .entries
            .get(name)
            .is_some_and(|e| mode.string() == Ok(e.mode.as_str()) && e.oid == *oid)
        {
            out.push(path_reason("changed", name));
        }
    }
    for name in strings(v.field("directories")?)? {
        if !work.entries.get(name).is_some_and(|e| e.kind == "tree") {
            out.push(path_reason("changed", name));
        }
    }
    for (name, d) in items(v.field("listings")?)? {
        if work.entries.contains_key(name)
            && work.listing(name)? != d.string()?
            && (!opt.names || listing_matters(v, bundle, name, work.children.get(name))?)
        {
            out.push(path_reason("listing", name));
        }
    }
    for name in strings(v.field("absent")?)? {
        if work.entries.contains_key(name) {
            out.push(path_reason("appeared", name));
        }
    }
    for (name, d) in items(v.field("untracked")?)? {
        if let Some(root) = &work.workdir {
            if workspace::digest(&root.join(workspace::path(name)?)) != d.string()? {
                out.push(path_reason("changed", name));
            }
        } else {
            out.push(path_reason("untracked", name));
        }
    }
    if opt.env {
        out.extend(environment_changes(v)?);
    }
    if opt.host {
        for (name, d) in items(v.field("external")?)? {
            if !MACHINE_LOCAL.iter().any(|p| under(name, p))
                && workspace::digest(&workspace::path(name)?) != d.string()?
            {
                out.push(path_reason("host", name));
            }
        }
    }
    Ok(out)
}
