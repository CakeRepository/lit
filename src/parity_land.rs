//! Give each implementation its own copy of the destination branch for a landing comparison.
use crate::{files, json, land};
use std::path::{Path, PathBuf};
use std::process::Command;

pub struct Landing {
    seed: PathBuf,
    url: String,
    receipt: PathBuf,
    repo: PathBuf,
}

fn inside(root: &Path, path: &Path) -> Result<PathBuf, String> {
    let canonical = root
        .join(path)
        .canonicalize()
        .map_err(|e| format!("{}: {e}", path.display()))?;
    canonical
        .strip_prefix(root)
        .map(Path::to_owned)
        .map_err(|_| {
            "land parity needs its receipt and Git repository inside the input directory".into()
        })
}

impl Landing {
    pub fn prepare(source: &Path, work: &Path, args: &[String]) -> Result<Self, String> {
        let (receipt, repo) = land::inputs(args)?;
        let root = source.canonicalize().map_err(|e| e.to_string())?;
        let receipt = inside(&root, receipt)?;
        let repo = inside(&root, repo)?;
        let raw = files::read(&root.join(&receipt))?;
        let row = json::parse(std::str::from_utf8(&raw).map_err(|e| e.to_string())?)?;
        let snap = row.field("snapshot")?;
        let repository = snap.field("repository")?.string()?;
        crate::collect::repository_parts(repository)?;
        let branch = snap.field("branch")?.string()?;
        let url = format!("https://github.com/{repository}.git");
        let seed = work.join("landing-base.git");
        let out = Command::new("git")
            .args([
                "clone",
                "--bare",
                "--no-local",
                "--no-tags",
                "--single-branch",
                "--branch",
                branch,
                "--",
                &url,
            ])
            .arg(&seed)
            .current_dir(root.join(&repo))
            .output()
            .map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(format!(
                "read landing target: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            ));
        }
        Ok(Self {
            seed,
            url,
            receipt,
            repo,
        })
    }

    pub fn configure(&self, child: &mut Command, dir: &Path, target: &Path) -> Result<(), String> {
        crate::parity::copy_tree(&self.seed, target).map_err(|e| e.to_string())?;
        let repo_dir = dir.join(&self.repo);
        let root = dir.canonicalize().map_err(|e| e.to_string())?;
        inside(&root, &repo_dir)?;
        inside(&root, &dir.join(&self.receipt))?;
        let dest = target
            .to_str()
            .ok_or("landing parity needs a UTF-8 temp path")?;
        // Configure a named remote in the command's environment. Probe the same URL Git
        // will receive and decline to run either land if a more specific rewrite won.
        let count: usize = std::env::var("GIT_CONFIG_COUNT")
            .unwrap_or_else(|_| "0".into())
            .parse()
            .map_err(|_| "invalid GIT_CONFIG_COUNT")?;
        let mut probe = Command::new("git");
        for cmd in [&mut probe, &mut *child] {
            cmd.env("GIT_CONFIG_COUNT", (count + 1).to_string())
                .env(
                    format!("GIT_CONFIG_KEY_{count}"),
                    format!("url.{dest}.pushInsteadOf"),
                )
                .env(format!("GIT_CONFIG_VALUE_{count}"), &self.url)
                .env("GIT_ALLOW_PROTOCOL", "file");
        }
        let out = probe
            .arg("-C")
            .arg(&repo_dir)
            .arg("-c")
            .arg(format!("remote.lit-parity-probe.url={}", self.url))
            .args(["remote", "get-url", "--push", "lit-parity-probe"])
            .output()
            .map_err(|e| e.to_string())?;
        if !out.status.success() || Path::new(String::from_utf8_lossy(&out.stdout).trim()) != target
        {
            return Err("could not isolate the landing push from the original remote".into());
        }
        child
            .arg("land")
            .arg(&self.receipt)
            .arg("--repo")
            .arg(&self.repo);
        Ok(())
    }
}
