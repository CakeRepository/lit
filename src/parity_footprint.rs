//! Put explicitly named fixture paths in each parity copy, including stores outside cwd.
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};
type Result<T> = std::result::Result<T, String>;
const PATH_FLAGS: &[&str] = &[
    "--store",
    "--repo",
    "--workdir",
    "--log",
    "--out",
    "--receipt",
    "--list",
    "--units-file",
    "--pin-file",
    "--audit-book",
    "--proof-store",
];
fn resolved(path: &Path) -> Result<PathBuf> {
    if path.exists() {
        return path.canonicalize().map_err(|e| e.to_string());
    }
    let parent = path.parent().ok_or("Invalid footprint parity path")?;
    Ok(resolved(parent)?.join(
        path.file_name()
            .ok_or("Invalid footprint parity filename")?,
    ))
}
pub struct Inputs {
    args: Vec<String>,
    edits: BTreeMap<usize, (Option<String>, PathBuf)>,
    external: Vec<PathBuf>,
    root: PathBuf,
}
impl Inputs {
    pub fn prepare(source: &Path, argv: &[String]) -> Result<Option<Self>> {
        let help = match argv.first().map(String::as_str) {
            Some("cached-run") => include_str!("help/cached-run.txt"),
            Some("cached-units") => include_str!("help/cached-units.txt"),
            Some("trace") => include_str!("help/trace.txt"),
            Some("trace-units") => include_str!("help/trace-units.txt"),
            Some("reuse") => include_str!("help/reuse.txt"),
            Some("select") => include_str!("help/select.txt"),
            _ => return Ok(None),
        };
        let flags: Vec<_> = help
            .lines()
            .filter_map(|line| line.split_whitespace().next())
            .filter(|s| s.starts_with("--"))
            .collect();
        let root = source.canonicalize().map_err(|e| e.to_string())?;
        let mut edits = BTreeMap::new();
        let mut outside = Vec::new();
        let mut i = 1;
        while i < argv.len() {
            let arg = &argv[i];
            if matches!(arg.as_str(), "--test" | "--") {
                break;
            }
            let (flag, inline) = arg
                .split_once('=')
                .map_or((arg.as_str(), None), |(f, v)| (f, Some(v)));
            let mut matching = flags
                .iter()
                .copied()
                .filter(|p| flag.starts_with("--") && p.starts_with(flag));
            let first = matching.next();
            let key = if flags.contains(&flag) {
                Some(flag)
            } else {
                first.filter(|_| matching.next().is_none())
            };
            if key == Some("--test") {
                break;
            }
            if key.is_some_and(|k| PATH_FLAGS.contains(&k)) {
                let val = if let Some(raw) = inline {
                    raw
                } else if let Some(v) = argv.get(i + 1).filter(|v| !v.starts_with('-')) {
                    i += 1;
                    v
                } else {
                    i += 1;
                    continue;
                };
                if val != "/dev/null" && !(cfg!(windows) && val.eq_ignore_ascii_case("NUL")) {
                    let real = resolved(&root.join(val))?;
                    if !real.starts_with(&root) {
                        outside.push(real.clone());
                    }
                    edits.insert(i, (inline.map(|_| flag.into()), real));
                }
            }
            i += 1;
        }
        outside.sort_by_key(|p| p.components().count());
        let mut external: Vec<PathBuf> = Vec::new();
        for p in outside {
            if !external.iter().any(|base| p.starts_with(base)) {
                external.push(p);
            }
        }
        Ok(Some(Self {
            args: argv.to_vec(),
            edits,
            external,
            root,
        }))
    }
    pub fn stage(&self, external: &Path) -> Result<Vec<String>> {
        if !self.external.is_empty() {
            fs::create_dir_all(external).map_err(|e| e.to_string())?;
        }
        for (i, src) in self.external.iter().enumerate() {
            if src.exists() {
                if external.starts_with(src) {
                    return Err(
                        "A footprint parity input contains the parity temporary directory".into(),
                    );
                }
                crate::parity::copy_tree(src, &external.join(i.to_string()))
                    .map_err(|e| e.to_string())?;
            }
        }
        let mut args = self.args.clone();
        for (i, (flag, path)) in &self.edits {
            let p = if let Ok(rel) = path.strip_prefix(&self.root) {
                if rel.as_os_str().is_empty() {
                    PathBuf::from(".")
                } else {
                    rel.into()
                }
            } else {
                let (n, rel) = self
                    .external
                    .iter()
                    .enumerate()
                    .find_map(|(i, base)| path.strip_prefix(base).ok().map(|p| (i, p)))
                    .ok_or("Missing parity input")?;
                let base = external.join(n.to_string());
                if rel.as_os_str().is_empty() {
                    base
                } else {
                    base.join(rel)
                }
            };
            args[*i] = if let Some(f) = flag {
                format!("{f}={}", p.display())
            } else {
                p.to_string_lossy().into_owned()
            };
        }
        Ok(args)
    }
}
