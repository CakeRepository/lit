//! Run Git in the repository explicitly selected by the caller.
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

pub struct Repo {
    path: PathBuf,
}

impl Repo {
    pub fn new(path: &Path) -> Result<Self, String> {
        // Git for Windows expects drive paths, not the verbatim paths returned by canonicalize.
        let path = std::fs::canonicalize(path).map_err(|e| format!("{}: {e}", path.display()))?;
        #[cfg(windows)]
        let path = {
            let plain = path.to_string_lossy();
            if let Some(rest) = plain.strip_prefix(r"\\?\UNC\") {
                PathBuf::from(format!(r"\\{rest}"))
            } else {
                PathBuf::from(plain.strip_prefix(r"\\?\").unwrap_or(&plain))
            }
        };
        Ok(Self { path })
    }

    pub fn output(&self, args: &[&str], input: Option<&[u8]>) -> Result<Output, String> {
        let mut command = Command::new("git");
        for dir in [&self.path, &self.path.join(".git")] {
            command
                .arg("-c")
                .arg(format!("safe.directory={}", dir.display()));
        }
        command
            .args([
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-C",
            ])
            .arg(&self.path)
            .args(args);
        if let Some(bytes) = input {
            command
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            let mut child = command.spawn().map_err(|e| e.to_string())?;
            let mut stdin = child.stdin.take().expect("piped stdin");
            std::thread::scope(|scope| {
                let writer = scope.spawn(move || stdin.write_all(bytes));
                let result = child.wait_with_output().map_err(|e| e.to_string())?;
                if let Err(e) = writer.join().expect("Git input writer") {
                    if e.kind() != io::ErrorKind::BrokenPipe || result.status.success() {
                        return Err(e.to_string());
                    }
                }
                Ok(result)
            })
        } else {
            command.output().map_err(|e| e.to_string())
        }
    }

    pub fn text(&self, args: &[&str], input: Option<&[u8]>) -> Result<String, String> {
        let output = self.output(args, input)?;
        if !output.status.success() {
            return Err(format!(
                "git {}: {}",
                args[0],
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(String::from_utf8(output.stdout)
            .map_err(|e| e.to_string())?
            .trim()
            .into())
    }
}
