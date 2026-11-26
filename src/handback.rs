//! Gives files that lit created as root back to the user who owns the folder
//! they were written into.
//!
//! A GitHub container job runs as root, but its workspace and temp folders
//! belong to the runner's user on the host. Anything root leaves there cannot be
//! removed by the runner, and the next job on that runner fails before it
//! starts. lit never writes into the action folder; this covers the paths an
//! operator names on the command line (--state, --repo, --out, --receipt, ...).

#[cfg(unix)]
use std::path::Path;
use std::path::PathBuf;
use std::time::{Duration, SystemTime};

/// Marks when a command started, so only entries changed during the command
/// are handed back.
pub struct Session {
    #[cfg_attr(not(unix), allow(dead_code))]
    start: SystemTime,
}

pub fn begin() -> Session {
    Session {
        start: SystemTime::now() - Duration::from_secs(2),
    }
}

/// Options whose value is a folder lit works in rather than one it writes its
/// own files into: the job's checkout, and the folder inside it.
const SKIP_VALUE: &[&str] = &["--workdir", "--cwd"];
/// Options after which every argument belongs to a command lit runs.
const STOP: &[&str] = &["--test"];

/// Picks the arguments that may name files lit wrote: plain arguments and
/// option values, up to --test.
pub fn paths(args: &[String]) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut skip = false;
    for a in args {
        if STOP.contains(&a.as_str()) {
            break;
        }
        if skip {
            skip = false;
            continue;
        }
        if a.starts_with('-') {
            let (name, value) = match a.split_once('=') {
                Some((n, v)) => (n, Some(v)),
                None => (a.as_str(), None),
            };
            if SKIP_VALUE.contains(&name) {
                skip = value.is_none();
                continue;
            }
            if let Some(v) = value.filter(|v| !v.is_empty()) {
                out.push(v.to_string());
            }
            continue;
        }
        out.push(a.clone());
    }
    out.into_iter()
        .filter_map(|p| std::path::absolute(&p).ok())
        // Never walk a filesystem root.
        .filter(|p| p.parent().is_some())
        .collect()
}

#[cfg(unix)]
impl Session {
    /// Hands back, under each path, every entry that root created or changed
    /// since begin. Each goes to the owner of the nearest folder above the path
    /// that root does not own; a path with no such folder (root's own area) is
    /// left as it is. Does nothing unless lit runs as root.
    pub fn finish(&self, paths: &[PathBuf]) -> Result<(), String> {
        if crate::sys::euid() != 0 {
            return Ok(());
        }
        let mut errors = Vec::new();
        for p in paths {
            if std::fs::symlink_metadata(p).is_err() {
                continue;
            }
            let Some(parent) = p.parent() else { continue };
            let Some((uid, gid, top)) = owner(parent) else {
                continue;
            };
            // Folders lit made on the way to p, such as a/b for --out a/b/c.json.
            let mut dir = parent;
            while dir != top {
                if self.created(dir) {
                    if let Err(e) = std::os::unix::fs::lchown(dir, Some(uid), Some(gid)) {
                        errors.push(format!("{}: {e}", dir.display()));
                    }
                }
                match dir.parent() {
                    Some(d) => dir = d,
                    None => break,
                }
            }
            self.walk(p, uid, gid, &mut errors);
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; "))
        }
    }

    fn walk(&self, path: &Path, uid: u32, gid: u32, errors: &mut Vec<String>) {
        // Vanished or unreadable: nothing of ours to give back.
        let Ok(info) = std::fs::symlink_metadata(path) else {
            return;
        };
        if self.created(path) {
            if let Err(e) = std::os::unix::fs::lchown(path, Some(uid), Some(gid)) {
                errors.push(format!("{}: {e}", path.display()));
            }
        }
        if info.is_dir() {
            if let Ok(entries) = std::fs::read_dir(path) {
                for entry in entries.flatten() {
                    self.walk(&entry.path(), uid, gid, errors);
                }
            }
        }
    }

    /// Whether name is root's and changed since begin.
    fn created(&self, name: &Path) -> bool {
        use std::os::unix::fs::MetadataExt;
        match std::fs::symlink_metadata(name) {
            Ok(info) => {
                info.uid() == 0 && info.modified().map(|m| m >= self.start).unwrap_or(false)
            }
            Err(_) => false,
        }
    }
}

/// The nearest existing folder at or above dir that root does not own, and
/// its owner.
#[cfg(unix)]
fn owner(dir: &Path) -> Option<(u32, u32, &Path)> {
    use std::os::unix::fs::MetadataExt;
    let mut dir = dir;
    loop {
        if let Ok(info) = std::fs::symlink_metadata(dir) {
            if info.uid() != 0 {
                return Some((info.uid(), info.gid(), dir));
            }
        }
        dir = dir.parent()?;
    }
}

#[cfg(not(unix))]
impl Session {
    pub fn finish(&self, _: &[PathBuf]) -> Result<(), String> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(args: &[&str]) -> Vec<String> {
        args.iter().map(|a| a.to_string()).collect()
    }

    #[test]
    fn picks_paths_up_to_the_test_command() {
        let cwd = std::env::current_dir().unwrap();
        let got = paths(&s(&[
            "run",
            "queue.json",
            "--repo",
            "batch.git",
            "--state=st",
            "--workdir",
            "checkout",
            "--cwd=inner",
            "--jobs",
            "4",
            "--test",
            "make",
            "out",
        ]));
        let want: Vec<PathBuf> = ["run", "queue.json", "batch.git", "st", "4"]
            .iter()
            .map(|p| cwd.join(p))
            .collect();
        assert_eq!(got, want);
    }

    #[test]
    fn never_names_a_filesystem_root() {
        assert!(paths(&s(&["/"])).is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn gives_back_what_root_created_and_nothing_else() {
        use std::os::unix::fs::MetadataExt;
        if crate::sys::euid() != 0 {
            eprintln!("skipped: needs root");
            return;
        }
        let base = std::env::temp_dir().join(format!("lit-handback-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let runner = base.join("runner");
        std::fs::create_dir_all(&runner).unwrap();
        std::os::unix::fs::chown(&runner, Some(4321), Some(4321)).unwrap();
        // Root's old file in the runner's folder is not ours to give away.
        let old = runner.join("old.txt");
        std::fs::write(&old, "old").unwrap();
        let earlier = SystemTime::now() - Duration::from_secs(3600);
        std::fs::File::options()
            .write(true)
            .open(&old)
            .unwrap()
            .set_modified(earlier)
            .unwrap();

        let session = begin();
        std::fs::create_dir_all(runner.join("a/b")).unwrap();
        std::fs::write(runner.join("a/b/c.json"), "{}").unwrap();
        std::fs::create_dir_all(base.join("mine")).unwrap();
        std::fs::write(base.join("mine/x"), "x").unwrap();
        session
            .finish(&[runner.join("a/b/c.json"), old.clone(), base.join("mine/x")])
            .unwrap();

        for p in ["a", "a/b", "a/b/c.json"] {
            assert_eq!(
                std::fs::metadata(runner.join(p)).unwrap().uid(),
                4321,
                "{p}"
            );
        }
        assert_eq!(
            std::fs::metadata(&old).unwrap().uid(),
            0,
            "an old root file stays root's"
        );
        // base is root's own area, so mine/x stays with root.
        assert_eq!(std::fs::metadata(base.join("mine/x")).unwrap().uid(), 0);
        std::fs::remove_dir_all(base).unwrap();
    }
}
