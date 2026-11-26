//! Explicitly selected source checkout for the developer parity command.
use std::path::{Path, PathBuf};
use std::process::Command;

pub fn driver() -> Result<PathBuf, String> {
    let root = std::env::var_os("LIT_REFERENCE")
        .ok_or("lit parity: set LIT_REFERENCE to the trusted lit source checkout")?;
    if !Path::new(&root).is_absolute() {
        return Err(
            "lit parity: LIT_REFERENCE must be an absolute path to the trusted lit source checkout"
                .into(),
        );
    }
    let path = Path::new(&root).join("scripts/parity-reference.py");
    path.canonicalize()
        .map_err(|e| format!("lit parity: {}: {e}", path.display()))
}

pub fn run(args: &[String]) -> Result<i32, String> {
    let mut child = Command::new(python()?)
        .args(["-I", "-B"])
        .arg(driver()?)
        .arg(
            crate::parity::epoch()
                .map(|n| n.to_string())
                .unwrap_or_default(),
        )
        .args(args)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .spawn()
        .map_err(|e| format!("lit parity: {e}"))?;
    #[cfg(unix)]
    crate::sys::forward_signals_to(child.id());
    child
        .wait()
        .map(|s| crate::parity::exit_code(&s))
        .map_err(|e| e.to_string())
}

/// Finds a program on PATH.
fn which(name: &str) -> Option<PathBuf> {
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
fn python() -> Result<PathBuf, String> {
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
        "lit parity: the development reference requires Python 3.12 or later; tried {} (set LIT_PYTHON to choose one)",
        tried.join(", ")
    ))
}
