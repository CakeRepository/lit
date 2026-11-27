use super::{
    compare::{resolution_key, stem, under, MACHINE_LOCAL},
    events::Events,
    workspace::{self, Workspace},
    Result,
};
use crate::{
    json::{object, Value},
    json_paths as paths,
};
use std::collections::{BTreeMap, BTreeSet};

pub const EPHEMERAL: [&str; 6] = ["/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp"];
pub fn array<'a>(text: impl IntoIterator<Item = &'a str>) -> Value {
    Value::Array(text.into_iter().map(Into::into).collect())
}
fn sorted(set: BTreeSet<String>, limit: usize) -> Value {
    let mut names = set.into_iter().collect::<Vec<_>>();
    names.sort_by(|a, b| paths::compare(a, b));
    names.truncate(limit);
    Value::Array(names.into_iter().map(Value::String).collect())
}
fn relative<'a>(path: &'a str, root: &str) -> Option<&'a str> {
    if path == root {
        Some("")
    } else {
        path.strip_prefix(root.trim_end_matches('/'))?
            .strip_prefix('/')
    }
}
pub fn summarize(
    e: &Events,
    work: &mut Workspace,
    root: &str,
    produced: &BTreeSet<String>,
    scratch: &[String],
) -> Result<Value> {
    let (mut tracked, mut untracked, mut git_static, mut listings, mut external) = (
        BTreeMap::new(),
        BTreeMap::new(),
        BTreeMap::new(),
        BTreeMap::new(),
        BTreeMap::new(),
    );
    let (mut directories, mut absent, mut metadata, mut unknown, mut shared) = (
        BTreeSet::new(),
        BTreeSet::new(),
        BTreeSet::new(),
        BTreeSet::new(),
        BTreeSet::new(),
    );
    let mut changed = BTreeSet::new();
    let mut touched: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (path, write) in &e.first {
        if let Some(rel) = relative(path, root) {
            let entry = work.entries.get(rel);
            if scratch.iter().any(|s| under(path, s))
                && !entry.is_some_and(|v| v.mode != "untracked" && v.kind == "blob")
            {
                continue;
            }
            let (dir, name) = rel
                .rsplit_once('/')
                .unwrap_or(("", if rel.is_empty() { "." } else { rel }));
            touched.entry(dir.into()).or_default().insert(name.into());
            if *write {
                continue;
            }
            if matches!(rel, ".git" | ".git/info" | ".git/info/exclude") {
                let d = if e.missing.contains(path) {
                    "absent".into()
                } else {
                    workspace::digest(&workspace::path(path)?)
                };
                git_static.insert(rel.into(), Value::String(d));
            } else if rel.starts_with(".git/") {
                metadata.insert(rel.into());
            } else if let Some(entry) = entry {
                if entry.kind == "tree" {
                    directories.insert(rel.into());
                    if e.listed.contains(path) {
                        listings.insert(rel.to_owned(), work.listing(rel)?.into());
                    }
                } else if entry.mode == "untracked" {
                    if e.written.contains(path) {
                        changed.insert(rel.to_owned());
                    }
                    untracked.insert(
                        rel.into(),
                        Value::String(workspace::digest(&workspace::path(path)?)),
                    );
                } else {
                    tracked.insert(
                        rel.into(),
                        Value::Array(vec![entry.mode.as_str().into(), entry.oid.clone()]),
                    );
                }
            } else if e.missing.contains(path) {
                absent.insert(rel.into());
            } else if produced.contains(path) {
                shared.insert(rel.into());
            } else if e.created.contains(path) {
                // this run's own O_CREAT made it, so it must not exist beforehand
                absent.insert(rel.into());
            } else {
                unknown.insert(rel.into());
            }
        } else if !*write {
            if produced.contains(path) {
                shared.insert(path.clone());
            } else if !e.written.contains(path)
                && !EPHEMERAL
                    .iter()
                    .chain(&MACHINE_LOCAL)
                    .any(|p| under(path, p))
            {
                external.insert(
                    path.clone(),
                    Value::String(workspace::digest(&workspace::path(path)?)),
                );
            }
        }
    }
    let (mut lookups, mut keys, mut enumerated) =
        (BTreeMap::new(), BTreeMap::new(), BTreeSet::new());
    for dir in listings.keys() {
        let names = touched.get(dir).cloned().unwrap_or_default();
        let count = names.len();
        let stems = names.iter().map(|n| stem(n).into()).collect();
        let mut keys_here = names.clone();
        keys_here.extend(
            names
                .iter()
                .filter_map(|n| resolution_key(n).map(str::to_owned)),
        );
        lookups.insert(dir.clone(), sorted(stems, usize::MAX));
        keys.insert(dir.clone(), sorted(keys_here, usize::MAX));
        if count > 5 && count * 2 >= work.children.get(dir).map_or(0, Vec::len) {
            enumerated.insert(dir.clone());
        }
    }
    let incomplete = [
        (
            !unknown.is_empty(),
            "read checkout paths that existed before neither the run nor the commit",
        ),
        (
            !changed.is_empty(),
            "modified untracked files after reading them",
        ),
        (!e.unresolved.is_empty(), "could not resolve relative paths"),
        (e.uring, "used io_uring, whose file reads strace cannot see"),
    ];
    Ok(object([
        ("tracked", Value::Object(tracked)),
        ("directories", sorted(directories, usize::MAX)),
        ("listings", Value::Object(listings)),
        ("listing_lookups", Value::Object(lookups)),
        ("listing_keys", Value::Object(keys)),
        ("enumerated", sorted(enumerated, usize::MAX)),
        ("absent", sorted(absent, usize::MAX)),
        ("untracked", Value::Object(untracked)),
        ("unknown", sorted(unknown, 50)),
        ("rewritten", sorted(changed, 50)),
        ("shared_state", sorted(shared, 50)),
        ("reads_git_metadata", Value::Bool(!metadata.is_empty())),
        ("git_metadata", sorted(metadata, 50)),
        ("git_static", Value::Object(git_static)),
        ("external", Value::Object(external)),
        (
            "network",
            array(e.network.iter().take(50).map(String::as_str)),
        ),
        (
            "unresolved",
            array(e.unresolved.iter().take(50).map(String::as_str)),
        ),
        (
            "incomplete",
            array(incomplete.iter().filter(|(b, _)| *b).map(|(_, s)| *s)),
        ),
    ]))
}
