//! Assemble independent path edits in one Git fast-import transaction.
use crate::git::Repo;
use crate::json::{self, object, Value};
use crate::snapshot::valid_oid;
use std::collections::{BTreeMap, BTreeSet};
use std::hash::{BuildHasher, RandomState};
use std::io::{self, Read, Write};
use std::path::Path;
use std::thread;

struct Topic<'a> {
    head: &'a str,
    patch_base: Option<&'a str>,
    message: &'a str,
}

#[derive(Clone)]
struct Edit {
    path: Vec<u8>,
    mode: String,
    blob: String,
}
type Deltas = BTreeMap<String, Vec<Edit>>;
type Result<T> = std::result::Result<T, String>;

fn ascii(b: &[u8]) -> Result<&str> {
    std::str::from_utf8(b).map_err(|e| e.to_string())
}

fn forks<'a>(
    repo: &Repo,
    base: &str,
    topics: &[Topic<'a>],
) -> Result<Option<BTreeMap<&'a str, String>>> {
    let mut out = BTreeMap::new();
    for topic in topics {
        if let Some(patch) = topic.patch_base {
            out.insert(topic.head, patch.to_owned());
        }
    }
    if !out.is_empty() {
        return Ok((out.len() == topics.len()).then_some(out));
    }
    let mut query = topics
        .iter()
        .map(|t| format!("{}\n", t.head))
        .collect::<String>();
    query.push_str(&format!("^{base}\n"));
    let graph = repo.text(
        &["rev-list", "--parents", "--stdin"],
        Some(query.as_bytes()),
    )?;
    let mut parents = BTreeMap::new();
    for line in graph.lines() {
        let mut fields = line.split_whitespace();
        let child = fields.next().ok_or("Missing revision")?;
        let Some(parent) = fields.next() else {
            return Ok(None);
        };
        if fields.next().is_some() {
            return Ok(None);
        }
        parents.insert(child, parent);
    }
    let mut visited = BTreeSet::new();
    for topic in topics {
        let mut node = topic.head;
        if !parents.contains_key(node) {
            return Ok(None);
        }
        while let Some(parent) = parents.get(node) {
            if !visited.insert(node) {
                return Ok(None);
            }
            node = parent;
        }
        out.insert(topic.head, node.to_owned());
    }
    Ok(Some(out))
}

fn parents(path: &[u8]) -> impl Iterator<Item = &[u8]> {
    path.iter()
        .enumerate()
        .filter(|(_, b)| **b == b'/')
        .map(|(i, _)| &path[..i])
}

#[derive(Default)]
struct TargetChanges {
    paths: BTreeSet<Vec<u8>>,
    dirs: BTreeSet<Vec<u8>>,
    deletion: bool,
}

impl TargetChanges {
    fn since(repo: &Repo, fork: &str, target: &str) -> Result<Self> {
        let raw = repo.bytes(
            &[
                "diff-tree",
                "--no-commit-id",
                "-r",
                "--no-renames",
                "--ignore-submodules=none",
                "--name-status",
                "-z",
                fork,
                target,
            ],
            None,
        )?;
        let mut changes = Self::default();
        let mut parts = raw.split(|&b| b == 0);
        while let Some(status) = parts.next().filter(|s| !s.is_empty()) {
            let path = parts.next().ok_or("Missing target delta path")?;
            changes.deletion |= status == b"D";
            changes.dirs.extend(parents(path).map(<[u8]>::to_vec));
            changes.paths.insert(path.to_vec());
        }
        Ok(changes)
    }

    fn compatible(&self, path: &[u8], added: bool) -> bool {
        !self.paths.contains(path)
            && (!added
                || !(self.deletion
                    || self.dirs.contains(path)
                    || parents(path).any(|p| self.paths.contains(p))))
    }
}

fn delta_chunk(
    repo: &Repo,
    topics: &[Topic<'_>],
    forks: &BTreeMap<&str, String>,
    target: &BTreeMap<&str, TargetChanges>,
) -> Result<Option<Deltas>> {
    let query = topics
        .iter()
        .map(|t| format!("{} {}\n", t.head, forks[t.head]))
        .collect::<String>();
    let raw = repo.bytes(
        &[
            "diff-tree",
            "--stdin",
            "-r",
            "--always",
            "--raw",
            "--no-abbrev",
            "--no-renames",
            "--ignore-submodules=none",
            "-z",
        ],
        Some(query.as_bytes()),
    )?;
    let mut deltas = Deltas::new();
    let mut head = "";
    let mut fields = raw.split(|&b| b == 0);
    while let Some(row) = fields.next().filter(|s| !s.is_empty()) {
        let Some(header) = row.strip_prefix(b":") else {
            head = ascii(row)?;
            if !forks.contains_key(head) || deltas.insert(head.to_owned(), Vec::new()).is_some() {
                return Err("Unexpected Git delta head".into());
            }
            continue;
        };
        let meta = ascii(header)?.split_ascii_whitespace().collect::<Vec<_>>();
        let [old, mode, _, blob, status] = meta.as_slice() else {
            return Err("Invalid Git delta".into());
        };
        let path = fields.next().ok_or("Missing Git delta path")?;
        if !matches!(*status, "M" | "A")
            || !matches!(*mode, "100644" | "100755")
            || !matches!(*old, "000000" | "100644" | "100755")
            || path.rsplit(|&b| b == b'/').next() == Some(b".gitattributes")
        {
            return Ok(None);
        }
        let fork = forks.get(head).ok_or("Missing delta head")?;
        if target
            .get(fork.as_str())
            .is_some_and(|t| !t.compatible(path, *status == "A"))
        {
            return Ok(None);
        }
        deltas
            .get_mut(head)
            .ok_or("Unexpected delta head")?
            .push(Edit {
                path: path.to_vec(),
                mode: (*mode).into(),
                blob: (*blob).into(),
            });
    }
    if deltas.len() != topics.len() || !topics.iter().all(|t| deltas.contains_key(t.head)) {
        return Err("Incomplete Git delta stream".into());
    }
    Ok(Some(deltas))
}

fn disjoint(repo: &Repo, base: &str, topics: &[Topic<'_>], jobs: usize) -> Result<Option<Deltas>> {
    if topics.len() < 2
        || topics.iter().map(|t| t.head).collect::<BTreeSet<_>>().len() != topics.len()
    {
        return Ok(None);
    }
    let Some(forks) = forks(repo, base, topics)? else {
        return Ok(None);
    };
    let mut target = BTreeMap::new();
    for fork in forks
        .values()
        .map(String::as_str)
        .filter(|&id| id != base)
        .collect::<BTreeSet<_>>()
    {
        target.insert(fork, TargetChanges::since(repo, fork, base)?);
    }
    let parts = thread::scope(|scope| {
        let chunks = topics.chunks(topics.len().div_ceil(jobs));
        let tasks: Vec<_> = chunks
            .map(|part| scope.spawn(|| delta_chunk(repo, part, &forks, &target)))
            .collect();
        tasks
            .into_iter()
            .map(|t| t.join().map_err(|_| "A delta worker stopped")?)
            .collect::<Result<Vec<_>>>()
    })?;
    let mut deltas = Deltas::new();
    let mut files = BTreeSet::new();
    let mut dirs = BTreeSet::new();
    for part in parts {
        let Some(part) = part else {
            return Ok(None);
        };
        for (head, edits) in part {
            for edit in &edits {
                if dirs.contains(&edit.path) || !files.insert(edit.path.clone()) {
                    return Ok(None);
                }
                for prefix in parents(&edit.path) {
                    if files.contains(prefix) {
                        return Ok(None);
                    }
                    dirs.insert(prefix.to_vec());
                }
            }
            deltas.insert(head, edits);
        }
    }
    Ok(Some(deltas))
}

fn chain(
    repo: &Repo,
    base: &str,
    topics: &[Topic<'_>],
    deltas: &Deltas,
    stamp: i64,
) -> Result<String> {
    let scratch = format!(
        "refs/concurrent-merge/scratch/{:016x}-{}",
        RandomState::new().hash_one(base),
        std::process::id()
    );
    let mut parent = base.to_owned();
    let mut stream = Vec::new();
    for (index, topic) in topics.iter().enumerate() {
        let mark = index + 1;
        write!(stream,"commit {scratch}\nmark :{mark}\ncommitter Concurrent Merge <batch@localhost> {stamp} +0000\ndata {}\n{}\nfrom {parent}\nmerge {}\n",topic.message.len(),topic.message,topic.head).unwrap();
        for edit in &deltas[topic.head] {
            write!(stream, "M {} {} \"", edit.mode, edit.blob).unwrap();
            for byte in &edit.path {
                write!(stream, "\\{byte:03o}").unwrap();
            }
            stream.extend_from_slice(b"\"\n");
        }
        stream.push(b'\n');
        parent = format!(":{mark}");
    }
    write!(stream, "get-mark {parent}\nreset {scratch}\n\ndone\n").unwrap();
    repo.text(
        &["fast-import", "--quiet", "--done", "--date-format=raw"],
        Some(&stream),
    )
}

fn assemble(repo: &Repo, request: &Value) -> Result<Value> {
    let base = request.field("base")?.string()?;
    let stamp = match request.field("timestamp")? {
        Value::Number(n) => n.parse::<i64>().map_err(|e| e.to_string())?,
        _ => return Err("Missing batch timestamp".into()),
    };
    let jobs = usize::try_from(request.field("jobs")?.number()?)
        .ok()
        .filter(|n| *n > 0)
        .ok_or("jobs must be positive")?;
    let mut topics = Vec::new();
    for row in request.field("topics")?.array()? {
        let head = row.field("head")?.string()?;
        let patch_base = row.get("patch_base").map(Value::string).transpose()?;
        if !valid_oid(head) || patch_base.is_some_and(|id| !valid_oid(id)) {
            return Err("Invalid source commit".into());
        }
        topics.push(Topic {
            head,
            patch_base,
            message: row.field("message")?.string()?,
        });
    }
    if !valid_oid(base) {
        return Err("Invalid base commit".into());
    }
    let Some(deltas) = disjoint(repo, base, &topics, jobs)? else {
        return Ok(Value::Null);
    };
    let tip = chain(repo, base, &topics, &deltas, stamp)?;
    let paths = deltas
        .into_iter()
        .map(|(head, edits)| {
            let paths = edits
                .into_iter()
                .map(|e| Value::String(e.path.iter().map(|b| format!("{b:02x}")).collect()))
                .collect();
            (head, Value::Array(paths))
        })
        .collect();
    Ok(object([
        ("tip", Value::String(tip)),
        ("paths", Value::Object(paths)),
    ]))
}

pub fn command(repo: &str) -> i32 {
    let result = (|| {
        let mut data = String::new();
        io::stdin()
            .read_to_string(&mut data)
            .map_err(|e| e.to_string())?;
        assemble(&Repo::new(Path::new(repo))?, &json::parse(&data)?)
    })();
    match result {
        Ok(output) => {
            println!("{}", json::pretty(&output));
            0
        }
        Err(error) => {
            eprintln!("lit: {error}");
            2
        }
    }
}
