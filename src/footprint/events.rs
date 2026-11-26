//! Reconstruct a job's observed paths and the process tree from strace -f -xx -y.
use super::Result;
use crate::json_paths::path_key;
use fancy_regex::Regex;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::BufRead,
    sync::LazyLock,
};

static TOKEN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?:(?:AT_FDCWD|\d+)<((?:\\x[0-9a-f]{2})*)>,\s*)?"((?:\\x[0-9a-f]{2})*)""#)
        .unwrap()
});
fn decode(s: &str) -> Option<String> {
    let bytes = s.as_bytes().chunks_exact(4);
    if !bytes.remainder().is_empty() {
        return None;
    }
    let v = bytes
        .map(|chunk| {
            if &chunk[..2] != b"\\x" {
                return None;
            }
            u8::from_str_radix(std::str::from_utf8(&chunk[2..]).ok()?, 16).ok()
        })
        .collect::<Option<Vec<_>>>()?;
    Some(path_key(&v))
}
fn fd(s: &str) -> Option<String> {
    let (number, rest) = s.split_once('<')?;
    if number.is_empty() || !number.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    decode(rest.split_once('>')?.0)
}
fn join(dir: &str, path: &str) -> String {
    if path.starts_with('/') || dir.is_empty() {
        path.into()
    } else if dir.ends_with('/') {
        format!("{dir}{path}")
    } else {
        format!("{dir}/{path}")
    }
}
fn normalize(s: &str) -> String {
    let absolute = s.starts_with('/');
    let mut parts = Vec::new();
    for p in s.split('/') {
        match p {
            "" | "." => {}
            ".." if parts.last().is_some_and(|v| *v != "..") => {
                parts.pop();
            }
            ".." if absolute => {}
            _ => parts.push(p),
        }
    }
    let prefix = if s.starts_with("//") && !s.starts_with("///") {
        "//"
    } else if absolute {
        "/"
    } else {
        ""
    };
    let path = format!("{prefix}{}", parts.join("/"));
    if path.is_empty() {
        ".".into()
    } else {
        path
    }
}
#[derive(Clone)]
enum Op {
    Touch(String, bool, bool), // written, missing
    List(String),
    Net(String),
    Unresolved(String),
    Uring,
    Exec(Vec<String>, Option<f64>),
    Clone(u64),
}

#[derive(Default)]
pub struct Events {
    pub first: BTreeMap<String, bool>,
    pub written: BTreeSet<String>,
    pub missing: BTreeSet<String>,
    pub listed: BTreeSet<String>,
    pub network: Vec<String>,
    pub unresolved: Vec<String>,
    pub uring: bool,
}
impl Events {
    fn apply(&mut self, op: &Op) {
        match op {
            Op::Touch(p, write, missing) => {
                if *write {
                    self.written.insert(p.clone());
                }
                if let std::collections::btree_map::Entry::Vacant(slot) =
                    self.first.entry(p.clone())
                {
                    slot.insert(*write);
                    if !write && *missing {
                        self.missing.insert(p.clone());
                    }
                }
            }
            Op::List(p) => {
                self.listed.insert(p.clone());
            }
            Op::Net(s) => self.network.push(s.clone()),
            Op::Unresolved(s) => self.unresolved.push(s.clone()),
            Op::Uring => self.uring = true,
            _ => {}
        }
    }
}
#[derive(Default)]
pub struct Trace {
    pub all: Events,
    record: bool,
    log: Vec<(u64, Op)>,
    parent: BTreeMap<u64, u64>,
    cwd: BTreeMap<u64, String>,
    exits: BTreeMap<u64, (i32, Option<f64>)>,
}
impl Trace {
    fn note(&mut self, pid: u64, op: Op) {
        self.all.apply(&op);
        if self.record {
            self.log.push((pid, op));
        }
    }
    fn touch(&mut self, pid: u64, p: &str, write: bool, missing: bool) {
        self.note(pid, Op::Touch(normalize(p), write, missing));
    }
    fn feed(
        &mut self,
        pid: u64,
        name: &str,
        args: &str,
        result: &str,
        stamp: Option<f64>,
    ) -> Result<()> {
        let failed = result.starts_with("-1");
        let missing = failed && (result.contains("ENOENT") || result.contains("ENOTDIR"));
        if let Some(cwd) = args
            .split_once("AT_FDCWD<")
            .and_then(|(_, s)| s.split_once('>'))
            .and_then(|(v, _)| decode(v))
        {
            self.cwd.insert(pid, cwd);
        }
        match name {
            "clone" | "clone3" | "fork" | "vfork" if !failed => {
                if let Some(child) = result
                    .split_whitespace()
                    .next()
                    .and_then(|s| s.parse().ok())
                {
                    self.parent.insert(child, pid);
                    self.note(pid, Op::Clone(child));
                    if let Some(cwd) = self.cwd.get(&pid).cloned() {
                        self.cwd.entry(child).or_insert(cwd);
                    }
                }
                return Ok(());
            }
            "getdents" | "getdents64" => {
                if let Some(p) = fd(args) {
                    self.touch(pid, &p, false, false);
                    self.note(pid, Op::List(normalize(&p)));
                }
                return Ok(());
            }
            "connect" => {
                if args.contains("AF_INET") {
                    self.note(pid, Op::Net(args.chars().take(300).collect()));
                }
                return Ok(());
            }
            "io_uring_setup" if !failed => {
                self.note(pid, Op::Uring);
                return Ok(());
            }
            "fchdir" => {
                if !failed {
                    if let Some(p) = fd(args) {
                        self.cwd.insert(pid, p);
                    }
                }
                return Ok(());
            }
            _ => {}
        }
        let moved = matches!(name, "rename" | "renameat" | "renameat2");
        let link = matches!(name, "link" | "linkat");
        let multi = moved || link || matches!(name, "symlink" | "symlinkat");
        let mut paths = Vec::new();
        let mut argv = Vec::new();
        let exec = self.record && name == "execve" && !failed;
        for capture in TOKEN.captures_iter(args) {
            let capture = capture.map_err(|e| e.to_string())?;
            let raw =
                decode(capture.get(2).unwrap().as_str()).ok_or("Invalid strace hex string")?;
            if exec && !paths.is_empty() {
                argv.push(raw);
                continue;
            }
            let resolved = if let Some(base) = capture.get(1) {
                join(
                    &decode(base.as_str()).ok_or("Invalid traced descriptor")?,
                    &raw,
                )
            } else if raw.starts_with('/') {
                raw
            } else if let Some(base) = self.cwd.get(&pid) {
                join(base, &raw)
            } else {
                self.note(pid, Op::Unresolved(raw));
                return Ok(());
            };
            paths.push(resolved);
            if !multi && !exec {
                break;
            }
        }
        let Some(first) = paths.first() else {
            return Ok(());
        };
        if exec {
            self.note(pid, Op::Exec(argv, stamp));
        }
        if name == "chdir" && !failed {
            self.cwd.insert(pid, normalize(first));
        }
        if matches!(name, "open" | "openat" | "openat2") {
            let write = ["O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC"]
                .iter()
                .any(|s| args.contains(s));
            let truncate = write && (args.contains("O_TRUNC") || args.contains("O_EXCL"));
            if !truncate {
                self.touch(pid, first, false, missing);
            }
            if write {
                self.touch(pid, first, true, false);
            }
            if let Some(real) = fd(result).filter(|_| !failed) {
                self.touch(pid, &real, truncate, false);
            }
        } else if matches!(
            name,
            "creat"
                | "mkdir"
                | "mkdirat"
                | "rmdir"
                | "unlink"
                | "unlinkat"
                | "truncate"
                | "chmod"
                | "fchmodat"
                | "fchmodat2"
                | "chown"
                | "lchown"
                | "fchownat"
                | "utime"
                | "utimes"
                | "utimensat"
                | "futimesat"
                | "setxattr"
                | "lsetxattr"
                | "removexattr"
                | "lremovexattr"
                | "mknod"
                | "mknodat"
        ) {
            self.touch(pid, first, true, false);
        } else if multi {
            if moved || link {
                self.touch(pid, first, false, missing);
            }
            if moved {
                self.touch(pid, first, true, false);
            }
            self.touch(pid, paths.last().unwrap(), true, false);
        } else {
            self.touch(pid, first, false, missing);
        }
        Ok(())
    }
    pub fn parse(reader: impl BufRead, record: bool) -> Result<Self> {
        let mut trace = Self {
            record,
            ..Self::default()
        };
        let mut pending = BTreeMap::new();
        for line in reader.split(b'\n') {
            let bytes = line.map_err(|e| e.to_string())?;
            let line = String::from_utf8_lossy(&bytes);
            let Some((pid, text)) = line.split_once(char::is_whitespace) else {
                continue;
            };
            let Ok(pid) = pid.parse::<u64>() else {
                continue;
            };
            let mut text = text.trim_start();
            let mut stamp = None;
            if let Some((time, rest)) = text.split_once(char::is_whitespace) {
                if time.contains('.') && time.bytes().all(|b| b.is_ascii_digit() || b == b'.') {
                    stamp = time.parse().ok();
                    text = rest.trim_start();
                }
            }
            if let Some(code) = text
                .strip_prefix("+++ exited with ")
                .and_then(|s| s.split_whitespace().next())
                .and_then(|s| s.parse().ok())
            {
                trace.exits.insert(pid, (code, stamp));
                continue;
            }
            if text.starts_with("+++ killed by SIG") {
                trace.exits.insert(pid, (-1, stamp));
                continue;
            }
            if let Some(start) = text.strip_suffix("<unfinished ...>") {
                pending.insert(pid, start.to_owned());
                continue;
            }
            let combined;
            if text.starts_with("<... ") {
                let Some(head) = pending.remove(&pid) else {
                    continue;
                };
                let Some((_, tail)) = text.split_once("resumed>") else {
                    continue;
                };
                combined = head + tail;
                text = &combined;
            }
            let Some((name, body)) = text.split_once('(') else {
                continue;
            };
            if !name
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
            {
                continue;
            }
            let Some((args, result)) = body.rsplit_once("= ") else {
                continue;
            };
            if let Some(args) = args.trim_end().strip_suffix(')') {
                trace.feed(pid, name, args, result, stamp)?;
            }
        }
        Ok(trace)
    }
    fn owner(&self, mut pid: u64, unit: &BTreeMap<u64, usize>) -> Option<usize> {
        let mut seen = BTreeSet::new();
        loop {
            if let Some(idx) = unit.get(&pid) {
                return Some(*idx);
            }
            if !seen.insert(pid) {
                return None;
            }
            pid = *self.parent.get(&pid)?;
        }
    }
    pub fn units(&self, pattern: &str) -> Result<Vec<ObservedUnit>> {
        let re = Regex::new(pattern).map_err(|e| e.to_string())?;
        let mut unit = BTreeMap::new();
        let mut roots = Vec::<(u64, Option<f64>)>::new();
        let mut names = BTreeMap::new();
        let mut units = Vec::<ObservedUnit>::new();
        for (pid, op) in &self.log {
            let owner = self.owner(*pid, &unit);
            match op {
                Op::Exec(argv, time) if owner.is_none() => {
                    let cmd = argv.join(" ");
                    if let Some(cap) = re.captures(&cmd).map_err(|e| e.to_string())? {
                        let name = cap
                            .get(usize::from(cap.len() > 1))
                            .ok_or("Unit regex capture did not match")?
                            .as_str();
                        let index = *names.entry(name.to_owned()).or_insert_with(|| {
                            let i = units.len();
                            units.push(ObservedUnit {
                                name: name.into(),
                                ..Default::default()
                            });
                            roots.push((*pid, *time));
                            i
                        });
                        unit.insert(*pid, index);
                        roots[index] = (*pid, *time);
                    }
                }
                Op::Clone(child) => {
                    if let Some(owner) = owner {
                        unit.entry(*child).or_insert(owner);
                    }
                }
                _ => {
                    if let Some(i) = owner {
                        units[i].events.apply(op);
                    }
                }
            }
        }
        for (unit, (pid, start)) in units.iter_mut().zip(roots) {
            if let Some((code, end)) = self.exits.get(&pid) {
                unit.code = Some(*code);
                unit.seconds = end.zip(start).map(|(e, s)| e - s);
            }
        }
        Ok(units)
    }
}
#[derive(Default)]
pub struct ObservedUnit {
    pub name: String,
    pub events: Events,
    pub code: Option<i32>,
    pub seconds: Option<f64>,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unfinished_reads_moves_and_unobservable_inputs() {
        let log = br#"10 openat(AT_FDCWD<\x2f\x77>, "\x61", O_RDONLY <unfinished ...>
11 clone(child_stack=NULL) = 12
10 <... openat resumed>) = 3<\x2f\x77\x2f\x61>
10 chdir("\x73") = 0
10 stat("\x62", 0x1) = -1 ENOENT (No such file or directory)
10 rename("\x63", "\x64") = 0
10 symlinkat("\x74", AT_FDCWD<\x2f\x77\x2f\x73>, "\x6c") = 0
10 openat(AT_FDCWD<\x2f\x77>, "\xff", O_RDWR) = 4<\x2f\x77\x2f\xff>
10 getdents64(4<\x2f\x77>, [], 32) = 0
10 connect(3, {sa_family=AF_INET}, 16) = -1 ECONNREFUSED
12 stat("\x72", 0x3) = -1 ENOENT
10 io_uring_setup(2, {}) = 5
"#;
        let t = Trace::parse(&log[..], false).unwrap();
        assert!(!t.all.first["/w/a"]);
        assert!(t.all.missing.contains("/w/s/b"));
        assert!(!t.all.first["/w/s/c"]);
        assert!(t.all.written.contains("/w/s/c") && t.all.first["/w/s/d"]);
        assert!(t.all.first["/w/s/l"] && !t.all.first.contains_key("/w/s/t"));
        assert!(!t.all.first[&path_key(b"/w/\xff")]);
        assert!(t.all.written.contains(&path_key(b"/w/\xff")));
        assert!(t.all.listed.contains("/w"));
        assert_eq!(t.all.network.len(), 1);
        assert_eq!(t.all.unresolved, ["r"]);
        assert!(t.all.uring);
    }
}
