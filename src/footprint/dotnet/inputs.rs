use super::*;
use std::io::{BufRead, BufReader};
use std::path::Component;

/// Tracked directory entries are compared even when the checkout gained an untracked file.
pub(super) struct Inputs<'a> {
    root: &'a Path,
    output: &'a Path,
    tracked: BTreeSet<String>,
    dirs: BTreeMap<String, BTreeSet<String>>,
    content: &'a Map,
}

impl<'a> Inputs<'a> {
    pub fn new(repo: &'a Repo, output: &'a Path, content: &'a Map) -> Result<Self> {
        let raw = repo.bytes(&["ls-files", "-z"], None)?;
        let tracked = std::str::from_utf8(&raw)
            .map_err(io)?
            .split('\0')
            .filter(|p| !p.is_empty())
            .map(str::to_owned)
            .collect::<BTreeSet<_>>();
        let mut dirs = BTreeMap::from([(String::new(), BTreeSet::new())]);
        for name in &tracked {
            let mut prefix = String::new();
            for part in name.split('/') {
                dirs.entry(prefix.clone()).or_default().insert(part.into());
                if !prefix.is_empty() {
                    prefix.push('/');
                }
                prefix.push_str(part);
            }
        }
        Ok(Self {
            root: repo.path(),
            output,
            tracked,
            dirs,
            content,
        })
    }
    pub fn digest(&self, key: &str) -> Result<Value> {
        let (kind, rel) = key.split_once(':').ok_or("Invalid .NET input record")?;
        if Path::new(rel)
            .components()
            .any(|c| !matches!(c, Component::Normal(_) | Component::CurDir))
        {
            return Err("Invalid .NET input path".into());
        }
        let p = self.root.join(rel);
        Ok(match kind {
            "out" => self.content.get(rel).cloned().unwrap_or("missing".into()),
            "repo" if p.is_file() => file_hash(&p)?,
            "dir" if p.is_dir() => {
                let entries = self
                    .dirs
                    .get(rel)
                    .into_iter()
                    .flatten()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n");
                Value::String(sha256::hex(entries.as_bytes()))
            }
            "stat" if p.is_dir() => "dir".into(),
            "stat" if p.exists() => "file".into(),
            "absent" if p.exists() => "present".into(),
            "absent" | "repo" | "dir" | "stat" => "missing".into(),
            _ => "unknown".into(),
        })
    }
    pub fn observed(&self, trace: &Path) -> Result<Map> {
        let mut keys = BTreeSet::new();
        let file = fs::File::open(trace).map_err(io)?;
        for line in BufReader::new(file).lines() {
            let line = line.map_err(io)?;
            let Some((call, path, ret)) = syscall(&line) else {
                continue;
            };
            let mut path = if path.is_absolute() {
                path
            } else if self.output.join(&path).exists() {
                self.output.join(path)
            } else {
                self.root.join(path)
            };
            // strace reports the spelling used by the process, including unresolved missing paths.
            let mut clean = PathBuf::new();
            for c in path.components() {
                if c == Component::ParentDir {
                    clean.pop();
                } else if c != Component::CurDir {
                    clean.push(c.as_os_str());
                }
            }
            path = clean;
            let failed = ret.starts_with("-1");
            if path.starts_with(self.output) {
                let rel = relative(&path, self.output)?;
                if !failed && self.content.contains_key(&rel) {
                    keys.insert(format!("out:{rel}"));
                }
                continue;
            }
            if !path.starts_with(self.root) {
                continue;
            }
            let rel = relative(&path, self.root)?;
            if rel == ".git"
                || rel.starts_with(".git/")
                || rel.split('/').any(|part| matches!(part, "bin" | "obj"))
            {
                continue;
            }
            let opened = matches!(call, "open" | "openat" | "openat2" | "creat");
            let kind = if failed {
                if !ret.contains("ENOENT") && !ret.contains("ENOTDIR") {
                    continue;
                }
                "absent"
            } else if opened && self.tracked.contains(&rel) {
                "repo"
            } else if opened && self.dirs.contains_key(&rel) {
                "dir"
            } else if self.tracked.contains(&rel) || self.dirs.contains_key(&rel) {
                "stat"
            } else {
                continue;
            };
            keys.insert(format!("{kind}:{rel}"));
        }
        let mut found = Map::new();
        for k in &keys {
            if let Some(p) = k.strip_prefix("stat:") {
                if keys.contains(&format!("repo:{p}")) || keys.contains(&format!("dir:{p}")) {
                    continue;
                }
            }
            let value = if k.starts_with("absent:") {
                "missing".into()
            } else {
                self.digest(k)?
            };
            found.insert(k.clone(), value);
        }
        Ok(found)
    }
}

// Native recording requests strace -xx, so every byte of the pathname is unambiguous.
fn syscall(line: &str) -> Option<(&str, PathBuf, &str)> {
    let (pid, body) = line.split_once(char::is_whitespace)?;
    pid.parse::<u64>().ok()?;
    let (call, args) = body.trim_start().split_once('(')?;
    if !super::runner::CALLS.split(',').any(|s| s == call) {
        return None;
    }
    let (args, ret) = args.rsplit_once(") ")?;
    let ret = ret.trim_start().strip_prefix('=')?.trim_start();
    let (fd, rest) = args.split_once('"')?;
    let fd = fd.trim_end_matches([' ', ',']);
    if !fd.is_empty() && fd != "AT_FDCWD" && fd.parse::<i32>().is_err() {
        return None;
    }
    let hex = rest.split_once('"')?.0;
    let mut path = Vec::new();
    for part in hex.as_bytes().chunks(4) {
        if part.len() != 4 || &part[..2] != b"\\x" {
            return None;
        }
        path.push(u8::from_str_radix(std::str::from_utf8(&part[2..]).ok()?, 16).ok()?);
    }
    Some((call, PathBuf::from(crate::merge_tree::os(&path).ok()?), ret))
}
