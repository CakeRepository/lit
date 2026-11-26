//! Runs a command that has not moved to Rust yet with the Python lit embedded in
//! the binary.
//!
//! The Python files are copied once into a private folder in the OS temp
//! directory and run from there with "python3 -I -B". Nothing is written into
//! the checkout, the action folder, HOME or RUNNER_TEMP: on a runner those are
//! shared with the host, and a container job running as root that leaves files
//! there (Python's __pycache__, on Nov 25, 2026) stops the runner from cleaning
//! up before the next job.

use crate::sha256::Sha256;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub type Files<'a> = &'a [(&'a str, &'a [u8])];

/// A stable hash of every file's path and bytes, in path order.
pub fn digest(files: Files) -> String {
    let mut sorted: Vec<_> = files.iter().collect();
    sorted.sort_by_key(|(name, _)| *name);
    let mut h = Sha256::new();
    for (name, data) in sorted {
        h.update(format!("{name}\0{}\0", data.len()).as_bytes());
        h.update(data);
    }
    h.hex()
}

fn digest_dir(dir: &Path) -> io::Result<String> {
    fn walk(dir: &Path, rel: &str, out: &mut Vec<(String, Vec<u8>)>) -> io::Result<()> {
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let rel = if rel.is_empty() {
                name
            } else {
                format!("{rel}/{name}")
            };
            let kind = entry.file_type()?;
            if kind.is_dir() {
                walk(&entry.path(), &rel, out)?;
            } else {
                // A symlink or anything else that is not a plain file fails the check.
                if !kind.is_file() {
                    return Err(io::Error::other(format!("{rel} is not a plain file")));
                }
                out.push((rel, fs::read(entry.path())?));
            }
        }
        Ok(())
    }
    let mut found = Vec::new();
    walk(dir, "", &mut found)?;
    let files: Vec<(&str, &[u8])> = found
        .iter()
        .map(|(n, d)| (n.as_str(), d.as_slice()))
        .collect();
    Ok(digest(&files))
}

/// Copies the files into base/lit-py-<digest>-<user> and returns that folder.
/// An existing folder is reused only when this user owns it, nobody else can
/// write to it, and its files still hash to the same digest; otherwise a fresh
/// copy replaces it. The copy is made beside the target and renamed into place,
/// so a concurrent lit never sees half a copy.
pub fn extract(files: Files, base: &Path) -> io::Result<PathBuf> {
    let digest = digest(files);
    let dir = base.join(format!("lit-py-{}-{}", &digest[..16], user_tag()));
    if intact(&dir, &digest).is_ok() {
        return Ok(dir);
    }
    let tmp = private_temp_dir(base)?;
    let result = (|| {
        for (name, data) in files {
            let target = tmp.join(name);
            make_private_dirs(target.parent().unwrap())?;
            write_private(&target, data)?;
        }
        // Replace a folder that failed the checks; another lit may have won the race.
        if fs::symlink_metadata(&dir).is_ok() {
            if intact(&dir, &digest).is_ok() {
                return Ok(());
            }
            remove(&dir).map_err(|e| {
                io::Error::other(format!("lit: cannot replace {}: {e}", dir.display()))
            })?;
        }
        if let Err(e) = fs::rename(&tmp, &dir) {
            if intact(&dir, &digest).is_ok() {
                return Ok(());
            }
            return Err(e);
        }
        intact(&dir, &digest).map_err(|e| {
            io::Error::other(format!(
                "lit: copy at {} failed its check: {e}",
                dir.display()
            ))
        })
    })();
    let _ = fs::remove_dir_all(&tmp);
    result.map(|_| dir)
}

fn remove(path: &Path) -> io::Result<()> {
    if fs::symlink_metadata(path)?.is_dir() {
        fs::remove_dir_all(path)
    } else {
        fs::remove_file(path)
    }
}

fn intact(dir: &Path, digest: &str) -> Result<(), String> {
    let info = fs::symlink_metadata(dir).map_err(|e| e.to_string())?;
    if !info.is_dir() {
        return Err("not a directory".into());
    }
    private(&info)?;
    let got = digest_dir(dir).map_err(|e| e.to_string())?;
    if got != digest {
        return Err("contents changed".into());
    }
    Ok(())
}

static COUNTER: AtomicU32 = AtomicU32::new(0);

fn private_temp_dir(base: &Path) -> io::Result<PathBuf> {
    loop {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.subsec_nanos())
            .unwrap_or(0);
        let name = format!(
            "lit-py-tmp-{}-{nanos}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::SeqCst)
        );
        let path = base.join(name);
        match make_private_dir(&path) {
            Ok(()) => return Ok(path),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
}

#[cfg(unix)]
fn make_private_dir(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    fs::DirBuilder::new().mode(0o700).create(path)
}

#[cfg(not(unix))]
fn make_private_dir(path: &Path) -> io::Result<()> {
    fs::create_dir(path)
}

#[cfg(unix)]
fn make_private_dirs(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    fs::DirBuilder::new()
        .mode(0o700)
        .recursive(true)
        .create(path)
}

#[cfg(not(unix))]
fn make_private_dirs(path: &Path) -> io::Result<()> {
    fs::create_dir_all(path)
}

#[cfg(unix)]
fn write_private(path: &Path, data: &[u8]) -> io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    f.write_all(data)
}

#[cfg(not(unix))]
fn write_private(path: &Path, data: &[u8]) -> io::Result<()> {
    fs::write(path, data)
}

#[cfg(unix)]
fn user_tag() -> String {
    crate::sys::uid().to_string()
}

#[cfg(not(unix))]
fn user_tag() -> String {
    std::env::var("USERNAME")
        .unwrap_or_default()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect()
}

#[cfg(unix)]
fn private(info: &fs::Metadata) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    if info.uid() != crate::sys::uid() {
        return Err(format!("owned by uid {}, not this user", info.uid()));
    }
    if info.mode() & 0o022 != 0 {
        return Err("other users can write to it".into());
    }
    Ok(())
}

#[cfg(not(unix))]
fn private(_: &fs::Metadata) -> Result<(), String> {
    // %TEMP% on Windows is already per user.
    Ok(())
}

/// Finds a program on PATH.
pub fn which(name: &str) -> Option<PathBuf> {
    let given = Path::new(name);
    if given.components().count() > 1 {
        return given.is_file().then(|| given.to_path_buf());
    }
    let exts: Vec<String> = if cfg!(windows) {
        std::env::var("PATHEXT")
            .unwrap_or(".EXE;.CMD;.BAT".into())
            .split(';')
            .map(|e| e.to_string())
            .collect()
    } else {
        vec![String::new()]
    };
    for dir in std::env::split_paths(&std::env::var_os("PATH")?) {
        for ext in &exts {
            let candidate = dir.join(format!("{name}{ext}"));
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

/// Finds an interpreter: LIT_PYTHON if set, else python3, else python. It must
/// be Python 3.12 or later.
pub fn python() -> Result<PathBuf, String> {
    let candidates = match std::env::var("LIT_PYTHON") {
        Ok(v) if !v.is_empty() => vec![v],
        _ => vec!["python3".to_string(), "python".to_string()],
    };
    let mut tried = Vec::new();
    for c in candidates {
        let Some(path) = which(&c) else {
            tried.push(format!("{c} (not found)"));
            continue;
        };
        let version = Command::new(&path)
            .arg("--version")
            .output()
            .ok()
            .and_then(|o| {
                let text = String::from_utf8_lossy(&o.stdout).into_owned()
                    + &String::from_utf8_lossy(&o.stderr);
                let rest = text.trim().strip_prefix("Python ")?.to_string();
                let mut parts = rest.split('.');
                let major: u32 = parts.next()?.parse().ok()?;
                let minor: u32 = parts
                    .next()?
                    .chars()
                    .take_while(|c| c.is_ascii_digit())
                    .collect::<String>()
                    .parse()
                    .ok()?;
                Some((major, minor))
            });
        match version {
            Some((major, minor)) if major > 3 || (major == 3 && minor >= 12) => return Ok(path),
            Some((major, minor)) => tried.push(format!("{c} (Python {major}.{minor})")),
            None => tried.push(format!("{c} (no version)")),
        }
    }
    Err(format!(
        "lit: this command still runs on Python 3.12 or later until it moves to Rust; tried {} (set LIT_PYTHON to choose one)",
        tried.join(", ")
    ))
}

const LAUNCHER: &str = r#"import sys
sys.path.insert(0, sys.argv.pop(1))
sys.argv[0] = "lit"
from concurrent_merge import engine, readers
readers.native_program = sys.argv.pop(1)
readers.native_suffixes = sys.argv.pop(1).split(",")
engine.native_program = readers.native_program
epoch = sys.argv.pop(1)
if epoch and sys.argv[1:2] == ["cached-run"]:
    from datetime import datetime, timezone
    from functools import partial
    from concurrent_merge import cache
    cache.cached_run = partial(cache.cached_run, clock=lambda: datetime.fromtimestamp(int(epoch), timezone.utc),
                               rng=lambda: 0.5)
from concurrent_merge.cli import main
sys.exit(main())
"#;

/// Builds the Python process for args. dir is the folder extract made.
pub fn command(python: &Path, dir: &Path, args: &[String], python_only: bool) -> Command {
    let mut cmd = Command::new(python);
    let binary = if python_only {
        None
    } else {
        std::env::current_exe().ok()
    };
    cmd.args(["-I", "-B", "-c", LAUNCHER])
        .arg(dir)
        .arg(binary.unwrap_or_default())
        .arg(crate::readers::suffixes().join(","))
        .arg(
            crate::parity::epoch()
                .map(|n| n.to_string())
                .unwrap_or_default(),
        )
        .args(args);
    // -B already stops bytecode; the variable also covers any Python the command starts.
    cmd.env("PYTHONDONTWRITEBYTECODE", "1");
    cmd
}

fn components() -> Result<&'static (PathBuf, PathBuf), String> {
    use std::sync::OnceLock;
    static PREPARED: OnceLock<Result<(PathBuf, PathBuf), String>> = OnceLock::new();
    PREPARED
        .get_or_init(|| {
            Ok((
                python()?,
                extract(crate::embedded::FILES, &std::env::temp_dir())
                    .map_err(|e| e.to_string())?,
            ))
        })
        .as_ref()
        .map_err(String::clone)
}

pub fn shadow(params: &crate::json::Value) -> Result<(), String> {
    let (python, dir) = components()?;
    let source = "import sys,json; sys.path.insert(0,sys.argv[1]); from pathlib import Path; from concurrent_merge.cli import write_shadow, Repo\n\
        p=json.loads(sys.argv[2]); snap=json.loads(Path(p['snapshot']).read_text(encoding='utf8'))\n\
        write_shadow(Repo(p['repo']),snap,Path(p['store']),p['job'],Path(p['directory']),Path(p['proofs']) if p['proofs'] else None,p['discover'] or None,p['strip'])";
    let status = Command::new(python)
        .args(["-I", "-B", "-c", source])
        .arg(dir)
        .arg(crate::json::canonical(params))
        .status()
        .map_err(|e| e.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("shadow exited {status}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FILES: Files = &[("pkg/a.py", b"print(1)\n"), ("pkg/sub/b.py", b"x = 2\n")];

    fn temp() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "lit-test-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn digest_covers_names_and_bytes_and_ignores_order() {
        let a = digest(FILES);
        assert_eq!(a, digest(&[FILES[1], FILES[0]]));
        assert_ne!(a, digest(&[("pkg/c.py", b"print(1)\n"), FILES[1]]));
        assert_ne!(a, digest(&[("pkg/a.py", b"print(2)\n"), FILES[1]]));
    }

    #[test]
    fn extracts_once_and_reuses_an_intact_copy() {
        let base = temp();
        let dir = extract(FILES, &base).unwrap();
        assert_eq!(fs::read(dir.join("pkg/sub/b.py")).unwrap(), b"x = 2\n");
        let marker = fs::metadata(dir.join("pkg/a.py"))
            .unwrap()
            .modified()
            .unwrap();
        assert_eq!(extract(FILES, &base).unwrap(), dir);
        assert_eq!(
            fs::metadata(dir.join("pkg/a.py"))
                .unwrap()
                .modified()
                .unwrap(),
            marker
        );
        let leftovers: Vec<_> = fs::read_dir(&base).unwrap().collect();
        assert_eq!(leftovers.len(), 1, "temporary copies are cleaned up");
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn replaces_a_tampered_copy_or_one_with_a_stray_file() {
        let base = temp();
        let dir = extract(FILES, &base).unwrap();
        fs::write(dir.join("pkg/a.py"), b"import os; os.system('x')\n").unwrap();
        assert_eq!(extract(FILES, &base).unwrap(), dir);
        assert_eq!(fs::read(dir.join("pkg/a.py")).unwrap(), b"print(1)\n");
        fs::write(dir.join("pkg/evil.py"), b"").unwrap();
        extract(FILES, &base).unwrap();
        assert!(!dir.join("pkg/evil.py").exists());
        fs::remove_dir_all(base).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn refuses_a_copy_others_can_write_to() {
        use std::os::unix::fs::PermissionsExt;
        let base = temp();
        let dir = extract(FILES, &base).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o777)).unwrap();
        assert!(intact(&dir, &digest(FILES)).is_err());
        let again = extract(FILES, &base).unwrap();
        assert_eq!(
            fs::metadata(&again).unwrap().permissions().mode() & 0o777,
            0o700
        );
        fs::remove_dir_all(base).unwrap();
    }
}
