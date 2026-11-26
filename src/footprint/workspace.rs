use super::Result;
use crate::{files, git::Repo, json::Value, json_paths as paths, merge_tree::os, sha256};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

pub struct Entry {
    pub mode: String,
    pub kind: String,
    pub oid: Value,
}
pub struct Workspace {
    pub commit: String,
    pub entries: BTreeMap<String, Entry>,
    pub children: BTreeMap<String, Vec<String>>,
    pub workdir: Option<PathBuf>,
    digests: BTreeMap<String, String>,
}
impl Workspace {
    pub fn new(repo: &Repo, revision: &str, workdir: Option<&Path>) -> Result<Self> {
        let commit = repo.text(
            &[
                "rev-parse",
                "--verify",
                "--end-of-options",
                &format!("{revision}^{{commit}}"),
            ],
            None,
        )?;
        let oid = repo.text(&["rev-parse", &format!("{commit}^{{tree}}")], None)?;
        let mut entries = BTreeMap::from([(
            String::new(),
            Entry {
                mode: "040000".into(),
                kind: "tree".into(),
                oid: Value::String(oid),
            },
        )]);
        let rows = repo.bytes(&["ls-tree", "-r", "-t", "-z", "--full-tree", &commit], None)?;
        for row in rows.split(|b| *b == 0).filter(|r| !r.is_empty()) {
            let mut parts = row.splitn(2, |b| *b == b'\t');
            let meta = std::str::from_utf8(parts.next().unwrap())
                .map_err(|e| e.to_string())?
                .split_whitespace()
                .collect::<Vec<_>>();
            let [mode, kind, oid] = meta.as_slice() else {
                return Err("Invalid ls-tree entry".into());
            };
            let path = paths::path_key(parts.next().ok_or("Missing Git path")?);
            entries.insert(
                path,
                Entry {
                    mode: (*mode).into(),
                    kind: (*kind).into(),
                    oid: (*oid).into(),
                },
            );
        }
        if let Some(path) = workdir {
            let work = Repo::new(path)?;
            if work.text(&["rev-parse", "HEAD"], None)? != commit {
                return Err("The checkout is not at the traced commit".into());
            }
            if !work
                .bytes(
                    &["status", "--porcelain", "-z", "--untracked-files=no"],
                    None,
                )?
                .is_empty()
            {
                return Err("Tracked files differ from the commit; use a clean checkout".into());
            }
            for raw in work
                .bytes(&["ls-files", "--others", "-z"], None)?
                .split(|b| *b == 0)
                .filter(|p| !p.is_empty())
            {
                let name = paths::path_key(raw);
                let mut path = name.as_str();
                let mut kind = "blob";
                while !path.is_empty() && !entries.contains_key(path) {
                    entries.insert(
                        path.into(),
                        Entry {
                            mode: "untracked".into(),
                            kind: kind.into(),
                            oid: Value::Null,
                        },
                    );
                    path = path.rsplit_once('/').map_or("", |(p, _)| p);
                    kind = "tree";
                }
            }
        }
        let mut children: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for p in entries.keys().filter(|s| !s.is_empty()) {
            let (dir, name) = p.rsplit_once('/').unwrap_or(("", p));
            children.entry(dir.into()).or_default().push(name.into());
        }
        for v in children.values_mut() {
            v.sort_by(|a, b| paths::compare(a, b));
        }
        Ok(Self {
            commit,
            entries,
            children,
            digests: BTreeMap::new(),
            workdir: workdir.map(Path::to_owned),
        })
    }
    pub fn listing(&mut self, dir: &str) -> Result<&str> {
        if !self.digests.contains_key(dir) {
            let names = self
                .children
                .get(dir)
                .map(Vec::as_slice)
                .unwrap_or_default();
            self.digests
                .insert(dir.into(), sha256::hex(&paths::bytes(&names.join("\0"))?));
        }
        Ok(&self.digests[dir])
    }
}
pub fn path(s: &str) -> Result<PathBuf> {
    Ok(PathBuf::from(os(&paths::bytes(s)?)?))
}
pub use super::digests::file as digest;
pub fn save(path: &Path, v: &Value) -> Result<()> {
    files::replace(path, format!("{}\n", paths::sorted(v, true)).as_bytes())
        .map_err(|e| files::error(path, e))
}
