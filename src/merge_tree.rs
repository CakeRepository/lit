//! Inspect Git's three-way result, including the stages of ordinary content conflicts.
use crate::git::Repo;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;

pub type Result<T> = std::result::Result<T, String>;
pub type PathBytes = Vec<u8>;
pub type Entry = (String, String); // mode and blob id
pub type Stages = BTreeMap<String, Entry>;

#[cfg(unix)]
pub fn os(bytes: &[u8]) -> Result<OsString> {
    use std::os::unix::ffi::OsStrExt;
    Ok(std::ffi::OsStr::from_bytes(bytes).to_owned())
}
#[cfg(not(unix))]
pub fn os(bytes: &[u8]) -> Result<OsString> {
    Ok(ascii(bytes)?.into())
}

fn ascii(b: &[u8]) -> Result<&str> {
    std::str::from_utf8(b).map_err(|e| e.to_string())
}

fn split(bytes: &[u8], sep: u8) -> Result<(&[u8], &[u8])> {
    let at = bytes
        .iter()
        .position(|&b| b == sep)
        .ok_or("Incomplete Git record")?;
    Ok((&bytes[..at], &bytes[at + 1..]))
}

pub struct Merge {
    pub tree: String,
    pub entries: BTreeMap<PathBytes, Stages>,
    pub kinds: BTreeMap<PathBytes, Vec<String>>,
    pub conflicts: BTreeSet<PathBytes>,
    pub special: BTreeSet<PathBytes>,
    pub clean: BTreeSet<PathBytes>,
    pub conflicted: bool,
}

pub fn inspect(repo: &Repo, ours: &str, theirs: &str) -> Result<Merge> {
    let out = repo.output(
        &[
            "merge-tree",
            "--write-tree",
            "--messages",
            "-z",
            ours,
            theirs,
        ],
        None,
    )?;
    if !matches!(out.status.code(), Some(0 | 1)) {
        return Err(format!(
            "git merge-tree: {}",
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    let mut fields = out.stdout.split(|&b| b == 0);
    let tree = ascii(fields.next().ok_or("Missing merge tree")?)?.to_owned();
    let mut result = Merge {
        tree,
        entries: BTreeMap::new(),
        kinds: BTreeMap::new(),
        conflicts: BTreeSet::new(),
        special: BTreeSet::new(),
        clean: BTreeSet::new(),
        conflicted: !out.status.success(),
    };
    for row in fields.by_ref().take_while(|b| !b.is_empty()) {
        let (meta, path) = split(row, b'\t')?;
        let words = ascii(meta)?.split_whitespace().collect::<Vec<_>>();
        let [mode, blob, stage] = words.as_slice() else {
            return Err("Incomplete Git stage".into());
        };
        result
            .entries
            .entry(path.to_vec())
            .or_default()
            .insert((*stage).into(), ((*mode).into(), (*blob).into()));
    }
    while let Some(count) = fields.next().filter(|b| !b.is_empty()) {
        let count = ascii(count)?.parse::<usize>().map_err(|e| e.to_string())?;
        let paths: Vec<_> = fields.by_ref().take(count).collect();
        let kind = ascii(fields.next().ok_or("Missing Git conflict kind")?)?;
        fields.next().ok_or("Missing Git conflict message")?;
        for path in paths {
            result
                .kinds
                .entry(path.to_vec())
                .or_default()
                .push(kind.to_owned());
            if kind == "CONFLICT (contents)" && count == 1 {
                result.conflicts.insert(path.to_vec());
            } else if kind.starts_with("CONFLICT") {
                result.special.insert(path.to_vec());
            } else if kind == "Auto-merging" && count == 1 {
                result.clean.insert(path.to_vec());
            }
        }
    }
    for kinds in result.kinds.values_mut() {
        kinds.sort();
    }
    Ok(result)
}

pub fn entries(repo: &Repo, rev: &str, paths: &[&[u8]]) -> Result<BTreeMap<PathBytes, Entry>> {
    let mut result = BTreeMap::new();
    let mut start = 0;
    while start < paths.len() {
        let mut args: Vec<OsString> = ["--literal-pathspecs", "ls-tree", "-z", rev, "--"]
            .into_iter()
            .map(OsString::from)
            .collect();
        let mut size = 0;
        while start < paths.len() && (size == 0 || size + paths[start].len() + 4 <= 12000) {
            size += paths[start].len() + 4;
            args.push(os(paths[start])?);
            start += 1;
        }
        let raw = repo.path_args(&args)?;
        for row in raw.split(|&b| b == 0).filter(|r| !r.is_empty()) {
            let (meta, path) = split(row, b'\t')?;
            let meta = ascii(meta)?.split_whitespace().collect::<Vec<_>>();
            let [mode, _, oid] = meta.as_slice() else {
                return Err("Invalid ls-tree entry".into());
            };
            result.insert(path.to_vec(), ((*mode).into(), (*oid).into()));
        }
    }
    Ok(result)
}

pub fn blobs(repo: &Repo, oids: &[String]) -> Result<Vec<Vec<u8>>> {
    objects(repo, oids, "blob")
}
pub fn objects(repo: &Repo, oids: &[String], kind: &str) -> Result<Vec<Vec<u8>>> {
    let input = oids.iter().map(|s| format!("{s}\0")).collect::<String>();
    let raw = repo.bytes(&["cat-file", "-Z", "--batch"], Some(input.as_bytes()))?;
    let mut remain = raw.as_slice();
    let mut data = Vec::new();
    for oid in oids {
        let (meta, body) = split(remain, 0)?;
        let fields = ascii(meta)?.split_whitespace().collect::<Vec<_>>();
        let [found, actual, len] = fields.as_slice() else {
            return Err("Unexpected Git object type in batch response".into());
        };
        if *actual != kind {
            return Err("Unexpected Git object type in batch response".into());
        }
        if found != oid {
            return Err("Unexpected blob in Git response".into());
        }
        let len = len.parse().map_err(|_| "Invalid Git object length")?;
        let (blob, rest) = body
            .split_at_checked(len)
            .ok_or("Incomplete Git object stream")?;
        remain = rest
            .strip_prefix(b"\0")
            .ok_or("Incomplete Git object stream")?;
        data.push(blob.to_vec());
    }
    if !remain.is_empty() {
        return Err("Unexpected trailing Git object data".into());
    }
    Ok(data)
}

type Hunk = (usize, usize, Vec<Vec<u8>>);

fn interval(raw: &[u8]) -> Option<(usize, usize)> {
    let s = std::str::from_utf8(raw.get(1..)?).ok()?;
    let (line, count) = s.split_once(',').unwrap_or((s, "1"));
    let count = count.parse().ok()?;
    Some((
        line.parse::<usize>()
            .ok()?
            .checked_sub(usize::from(count != 0))?,
        count,
    ))
}

fn apply(base: &[&[u8]], edits: &[Hunk]) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let mut pos = 0;
    for (start, end, put) in edits {
        for line in base.get(pos..*start)? {
            out.extend_from_slice(line);
        }
        for line in put {
            out.extend_from_slice(line);
        }
        pos = *end;
    }
    for line in base.get(pos..)? {
        out.extend_from_slice(line);
    }
    Some(out)
}

pub fn exact_union(
    repo: &Repo,
    ids: &[String],
    inputs: &[Vec<u8>],
    expected: &[u8],
) -> Result<bool> {
    let base: Vec<_> = inputs[0].split_inclusive(|&b| b == b'\n').collect();
    let mut combined = BTreeSet::new();
    for index in [1, 2] {
        let raw = repo.bytes(
            &[
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--text",
                "-U0",
                &ids[0],
                &ids[index],
            ],
            None,
        )?;
        let revised: Vec<_> = inputs[index].split_inclusive(|&b| b == b'\n').collect();
        let mut edits = Vec::new();
        for row in raw
            .split(|&b| b == b'\n')
            .filter(|line| line.starts_with(b"@@ -"))
        {
            let parts: Vec<_> = row.split(|&b| b == b' ').take(4).collect();
            if parts.len() != 4 || parts[3] != b"@@" {
                continue;
            }
            let (a, n) = interval(parts[1]).ok_or("Invalid Git hunk range")?;
            let (b, m) = interval(parts[2]).ok_or("Invalid Git hunk range")?;
            let Some(put) = revised.get(b..b + m) else {
                return Ok(false);
            };
            edits.push((a, a + n, put.iter().map(|line| line.to_vec()).collect()));
        }
        if apply(&base, &edits).as_deref() != Some(inputs[index].as_slice()) {
            return Ok(false);
        }
        combined.extend(edits);
    }
    let hunks: Vec<_> = combined.into_iter().collect();
    Ok(!hunks.windows(2).any(|w| w[0].0 == w[1].0)
        && apply(&base, &hunks).as_deref() == Some(expected))
}

#[derive(Default)]
struct Directory {
    files: BTreeMap<PathBytes, Entry>,
    dirs: BTreeMap<PathBytes, Directory>,
}
impl Directory {
    fn rebuild(mut self, repo: &Repo, tree: &str) -> Result<String> {
        let raw = repo.bytes(&["ls-tree", "-z", tree], None)?;
        let mut output = Vec::new();
        for row in raw.split(|&b| b == 0).filter(|b| !b.is_empty()) {
            let (meta, name) = split(row, b'\t')?;
            if let Some(sub) = self.dirs.remove(name) {
                let parts = ascii(meta)?.split_whitespace().collect::<Vec<_>>();
                let [mode, "tree", id] = parts.as_slice() else {
                    return Err("Expected a subtree at the resolution path".into());
                };
                output.extend_from_slice(
                    format!("{mode} tree {}", sub.rebuild(repo, id)?).as_bytes(),
                );
            } else if let Some((mode, id)) = self.files.remove(name) {
                output.extend_from_slice(format!("{mode} blob {id}").as_bytes());
            } else {
                output.extend_from_slice(meta);
            }
            output.push(b'\t');
            output.extend_from_slice(name);
            output.push(0);
        }
        if !self.dirs.is_empty() || !self.files.is_empty() {
            return Err("Resolution path missing from Git merge tree".into());
        }
        repo.text(&["mktree", "-z"], Some(&output))
    }
}
pub fn replace(repo: &Repo, tree: &str, edits: BTreeMap<PathBytes, Entry>) -> Result<String> {
    let mut root = Directory::default();
    for (path, entry) in edits {
        let mut dir = &mut root;
        let mut parts = path.split(|&b| b == b'/').peekable();
        while let Some(name) = parts.next() {
            if parts.peek().is_none() {
                dir.files.insert(name.to_vec(), entry);
                break;
            }
            dir = dir.dirs.entry(name.to_vec()).or_default();
        }
    }
    root.rebuild(repo, tree)
}
