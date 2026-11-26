//! Verify three-way merges and preserve the reference conflict IDs and per-author votes.
use crate::changes::Change;
use crate::git::Repo;
use crate::json::{self, object, Value};
use crate::merge_tree::{self as trees, Entry, Merge, PathBytes, Result, Stages};
use crate::{files, readers, sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::path::Path;
use std::thread;

pub type Owners = BTreeMap<PathBytes, BTreeSet<String>>;
pub struct Merged {
    pub head: Option<String>,
    pub fixes: Vec<Value>,
    pub conflicts: Vec<Value>,
}

pub fn path_changes(repo: &Repo, from: &str, to: &str) -> Result<Vec<PathBytes>> {
    Ok(repo
        .bytes(
            &[
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--name-only",
                "-z",
                from,
                to,
            ],
            None,
        )?
        .split(|&b| b == 0)
        .filter(|p| !p.is_empty())
        .map(<[u8]>::to_vec)
        .collect())
}

fn suffix(path: &[u8]) -> String {
    let file = path.rsplit(|&b| b == b'/').next().unwrap_or(path);
    let dot = file.iter().rposition(|&b| b == b'.');
    dot.filter(|&i| i > 0 && i + 1 < file.len())
        .map_or_else(String::new, |i| {
            String::from_utf8_lossy(&file[i..]).to_lowercase()
        })
}

fn clean_stages(repo: &Repo, merged: &mut Merge, ours: &str, incoming: &str) -> Result<()> {
    let paths: Vec<_> = merged
        .clean
        .iter()
        .filter(|p| {
            !merged.entries.contains_key(*p)
                && !merged.special.contains(*p)
                && readers::language(&suffix(p)).is_some()
        })
        .map(Vec::as_slice)
        .collect();
    if paths.is_empty() {
        return Ok(());
    }
    let bases = repo.text(&["merge-base", "--all", ours, incoming], None)?;
    if bases.lines().count() != 1 {
        return Ok(());
    }
    let snapshots = [
        trees::entries(repo, &bases, &paths)?,
        trees::entries(repo, ours, &paths)?,
        trees::entries(repo, incoming, &paths)?,
    ];
    for path in paths {
        let mut stages = Stages::new();
        for (i, tree) in snapshots.iter().enumerate() {
            if let Some(entry) = tree
                .get(path)
                .filter(|(m, _)| matches!(m.as_str(), "100644" | "100755"))
            {
                stages.insert((i + 1).to_string(), entry.clone());
            }
        }
        if stages.len() == 3
            && stages
                .values()
                .map(|(_, b)| b)
                .collect::<BTreeSet<_>>()
                .len()
                == 3
        {
            merged.entries.insert(path.to_vec(), stages);
        }
    }
    Ok(())
}

fn mode(stages: &Stages) -> Option<String> {
    let [a, b, c] = ["1", "2", "3"].map(|s| stages.get(s).map(|v| v.0.as_str()));
    let (a, b, c) = (a?, b?, c?);
    if ![a, b, c].iter().all(|m| matches!(*m, "100644" | "100755")) {
        return None;
    }
    if b == c || a == c {
        Some(b.into())
    } else if a == b {
        Some(c.into())
    } else {
        None
    }
}

pub fn write_json(path: &Path, value: &Value) -> Result<()> {
    files::replace(path, format!("{}\n", json::pretty(value)).as_bytes()).map_err(|e| e.to_string())
}

fn approved(
    repo: &Repo,
    state: &Path,
    key: &str,
    authors: &BTreeSet<String>,
) -> Result<Option<String>> {
    let mut voted = None;
    for author in authors {
        let path = state
            .join("votes")
            .join(key)
            .join(format!("{}.json", sha256::hex(author.as_bytes())));
        let data = match std::fs::read_to_string(&path) {
            Ok(data) => data,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.to_string()),
        };
        let row = json::parse(&data)?;
        let id = row.field("blob")?.string()?;
        if !crate::snapshot::valid_oid(id) {
            return Err("Approved object must be a Git blob".into());
        }
        if voted.as_deref().is_some_and(|v| v != id) {
            return Ok(None);
        }
        voted = Some(id.to_owned());
    }
    if let Some(id) = &voted {
        if repo.text(&["cat-file", "-t", id], None)? != "blob" {
            return Err("Approved object must be a Git blob".into());
        }
    }
    Ok(voted)
}

enum Decision {
    Keep,
    Replace(Entry, Option<Value>),
    Pending(String, Value),
}

struct Compare<'a> {
    repo: &'a Repo,
    state: &'a Path,
    ours: &'a str,
    change: &'a Change,
    owners: &'a Owners,
    merged: &'a Merge,
}

impl Compare<'_> {
    fn file(&self, path: &[u8]) -> Result<Decision> {
        let empty = Stages::new();
        let stages = self.merged.entries.get(path).unwrap_or(&empty);
        let line = self.merged.conflicts.contains(path);
        let ordinary = line || self.merged.clean.contains(path);
        let mode = if ordinary && !self.merged.special.contains(path) {
            mode(stages)
        } else {
            None
        };
        let ext = suffix(path);
        let mut replacement = None;
        if mode.is_some() {
            let mut ids: Vec<_> = ["1", "2", "3"]
                .iter()
                .map(|k| stages[*k].1.clone())
                .collect();
            if !line {
                let entries = trees::entries(self.repo, &self.merged.tree, &[path])?;
                ids.push(
                    entries
                        .get(path)
                        .ok_or("Missing clean merge blob")?
                        .1
                        .clone(),
                );
            }
            let data = trees::blobs(self.repo, &ids)?;
            if !line && trees::exact_union(self.repo, &ids, &data, &data[3])? {
                return Ok(Decision::Keep);
            }
            let result = readers::resolve(&ext, [&data[0], &data[1], &data[2]])?;
            if line {
                if let Some(bytes) = result {
                    let id = self
                        .repo
                        .text(&["hash-object", "-w", "--stdin"], Some(&bytes))?;
                    replacement = Some((
                        id,
                        format!(
                            "{}-outline",
                            readers::language(&ext).ok_or("Missing source reader")?
                        ),
                    ));
                }
            } else if result.as_deref() == Some(data[3].as_slice()) {
                return Ok(Decision::Keep);
            }
        }
        let mut authors = self.owners.get(path).cloned().unwrap_or_default();
        if authors.is_empty() {
            let mut args: Vec<OsString> = ["log", "-1", "--format=%H %an", self.ours, "--"]
                .into_iter()
                .map(Into::into)
                .collect();
            args.push(trees::os(path)?);
            let log = String::from_utf8(self.repo.path_args(&args)?).map_err(|e| e.to_string())?;
            let name = log.trim();
            authors.insert(format!(
                "base:{}",
                if name.is_empty() { self.ours } else { name }
            ));
        }
        authors.insert(self.change.participant());
        let path_value = Value::PathBytes(path.to_vec());
        let incoming = object([
            ("number", self.change.number.into()),
            ("head", self.change.head.as_str().into()),
            ("author", self.change.author.as_str().into()),
        ]);
        let stages_value = stages
            .iter()
            .map(|(k, (m, id))| {
                (
                    k.clone(),
                    Value::Array(vec![m.as_str().into(), id.as_str().into()]),
                )
            })
            .collect();
        let conflict = object([
            ("path", path_value.clone()),
            ("incoming", incoming),
            ("stages", Value::Object(stages_value)),
            (
                "authors",
                Value::Array(authors.iter().cloned().map(Value::String).collect()),
            ),
            ("mode", mode.as_deref().map_or(Value::Null, Value::from)),
            (
                "kinds",
                Value::Array(
                    self.merged
                        .kinds
                        .get(path)
                        .into_iter()
                        .flatten()
                        .cloned()
                        .map(Value::String)
                        .collect(),
                ),
            ),
        ]);
        let key = sha256::hex(json::canonical(&conflict).as_bytes());
        if let Some(mode) = mode {
            if replacement.is_none() {
                replacement = approved(self.repo, self.state, &key, &authors)?
                    .map(|id| (id, "agent-agreement".into()));
            }
            if let Some((id, why)) = replacement {
                let fix = (line || why == "agent-agreement").then(|| {
                    object([
                        ("id", key.as_str().into()),
                        ("path", path_value),
                        ("method", Value::String(why)),
                    ])
                });
                return Ok(Decision::Replace((mode, id), fix));
            }
        }
        let Value::Object(mut row) = conflict else {
            unreachable!()
        };
        row.insert("id".into(), key.as_str().into());
        row.insert("ours".into(), self.ours.into());
        if row["mode"] != Value::Null
            && readers::language(&ext).is_none()
            && !matches!(ext.as_str(), "" | ".txt" | ".text" | ".log")
        {
            let mut hint =
                format!("lit has no reader for {ext} files yet, so it could only compare ")
                    .into_bytes();
            hint.extend_from_slice(path);
            hint.extend_from_slice(b" line by line. Agents: you can teach lit this language. Open an issue at https://github.com/CakeRepository/lit/issues and send a PR that adds a reader under concurrent_merge/readers/ (see CONTRIBUTING.md).");
            row.insert("hint".into(), Value::PathBytes(hint));
        }
        Ok(Decision::Pending(key, Value::Object(row)))
    }
}

pub struct Engine<'a> {
    pub repo: &'a Repo,
    pub state: &'a Path,
    pub jobs: usize,
    pub hinted: BTreeSet<String>,
}

fn sort_key(path: &[u8]) -> Vec<u32> {
    let mut key = Vec::new();
    for chunk in path.utf8_chunks() {
        key.extend(chunk.valid().chars().map(u32::from));
        key.extend(chunk.invalid().iter().map(|&b| 0xdc00 + u32::from(b)));
    }
    key
}

impl Engine<'_> {
    pub fn merge(
        &mut self,
        ours: &str,
        change: &Change,
        owners: &Owners,
        stamp: i64,
    ) -> Result<Merged> {
        let mut merged = trees::inspect(self.repo, ours, &change.head)?;
        clean_stages(self.repo, &mut merged, ours, &change.head)?;
        let mut paths: Vec<_> = merged
            .entries
            .keys()
            .chain(&merged.special)
            .cloned()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        paths.sort_by_cached_key(|p| sort_key(p));
        let compare = Compare {
            repo: self.repo,
            state: self.state,
            ours,
            change,
            owners,
            merged: &merged,
        };
        let decisions = thread::scope(|scope| {
            let groups = paths.chunks(paths.len().div_ceil(self.jobs).max(1));
            let run = |part: &[PathBytes]| {
                part.iter()
                    .map(|p| compare.file(p))
                    .collect::<Result<Vec<_>>>()
            };
            let tasks: Vec<_> = groups.map(|part| scope.spawn(move || run(part))).collect();
            let mut all = Vec::new();
            for task in tasks {
                all.extend(task.join().map_err(|_| "A merge worker stopped")??);
            }
            Ok::<_, String>(all)
        })?;
        let mut fixes = Vec::new();
        let mut conflicts = Vec::new();
        let mut edits = BTreeMap::new();
        for (path, decision) in paths.into_iter().zip(decisions) {
            match decision {
                Decision::Keep => (),
                Decision::Replace(entry, fix) => {
                    edits.insert(path, entry);
                    fixes.extend(fix);
                }
                Decision::Pending(key, record) => {
                    let refs=format!("update refs/concurrent-merge/conflicts/{key}/ours {ours}\nupdate refs/concurrent-merge/conflicts/{key}/incoming {}\n",change.head);
                    self.repo
                        .text(&["update-ref", "--stdin"], Some(refs.as_bytes()))?;
                    if let Some(Value::PathBytes(hint)) = record.get("hint") {
                        if self.hinted.insert(suffix(&path)) {
                            eprintln!("lit: {}", String::from_utf8_lossy(hint));
                        }
                    }
                    write_json(
                        &self.state.join("conflicts").join(format!("{key}.json")),
                        &record,
                    )?;
                    conflicts.push(Value::String(key));
                }
            }
        }
        let head = if !conflicts.is_empty() {
            None
        } else {
            let tree = if !edits.is_empty() {
                trees::replace(self.repo, &merged.tree, edits)?
            } else if merged.conflicted {
                return Err("Unaccounted Git conflict".into());
            } else {
                merged.tree
            };
            Some(
                self.repo
                    .commit(&tree, ours, &change.head, &change.message(), stamp)?,
            )
        };
        Ok(Merged {
            head,
            fixes,
            conflicts,
        })
    }
}
