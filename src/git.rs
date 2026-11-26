//! Run Git in the repository explicitly selected by the caller.
use std::ffi::{OsStr, OsString};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

pub struct Repo {
    path: PathBuf,
}

impl Repo {
    pub fn path(&self) -> &Path {
        &self.path
    }

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
        self.output_env(args, input, &[])
    }

    pub(crate) fn output_env<S: AsRef<OsStr>>(
        &self,
        args: &[S],
        input: Option<&[u8]>,
        env: &[(&str, &str)],
    ) -> Result<Output, String> {
        let mut command = Command::new("git");
        command.envs(env.iter().copied());
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
        Self::checked(args, self.output(args, input)?)
    }

    pub fn bytes(&self, args: &[&str], input: Option<&[u8]>) -> Result<Vec<u8>, String> {
        let output = self.output(args, input)?;
        if output.status.success() {
            Ok(output.stdout)
        } else {
            Err(format!(
                "git {}: {}",
                args[0],
                String::from_utf8_lossy(&output.stderr).trim()
            ))
        }
    }

    pub fn path_args(&self, args: &[OsString]) -> Result<Vec<u8>, String> {
        let out = self.output_env(args, None, &[])?;
        if out.status.success() {
            Ok(out.stdout)
        } else {
            Err(format!(
                "git: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            ))
        }
    }

    fn checked(args: &[&str], output: Output) -> Result<String, String> {
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

    pub fn commit(
        &self,
        tree: &str,
        parent: &str,
        head: &str,
        message: &str,
        timestamp: i64,
    ) -> Result<String, String> {
        let mut args = vec!["commit-tree", tree, "-p", parent];
        if head != parent {
            args.extend(["-p", head]);
        }
        let date = format!("@{timestamp} +0000");
        let env = [
            ("GIT_AUTHOR_NAME", "Concurrent Merge"),
            ("GIT_COMMITTER_NAME", "Concurrent Merge"),
            ("GIT_AUTHOR_EMAIL", "batch@localhost"),
            ("GIT_COMMITTER_EMAIL", "batch@localhost"),
            ("GIT_AUTHOR_DATE", date.as_str()),
            ("GIT_COMMITTER_DATE", date.as_str()),
        ];
        Self::checked(
            &args,
            self.output_env(&args, Some(message.as_bytes()), &env)?,
        )
    }

    pub fn ancestor(&self, before: &str, after: &str) -> Result<bool, String> {
        match self
            .output(&["merge-base", "--is-ancestor", before, after], None)?
            .status
            .code()
        {
            Some(0) => Ok(true),
            Some(1) => Ok(false),
            _ => Err("Missing commit in archive".into()),
        }
    }
}
