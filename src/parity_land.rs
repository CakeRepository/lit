//! Give each implementation its own copy of the destination branch for a landing comparison.
use crate::{files, json, land};
use std::path::{Path, PathBuf};
use std::process::Command;

pub struct Landing {
    seed: PathBuf,
    receipt: PathBuf,
    repo: PathBuf,
    command: String,
    options: Vec<String>,
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
        let root = source.canonicalize().map_err(|e| e.to_string())?;
        let mut options = Vec::new();
        let (receipt, repo) = if args[0] == "propose" {
            let parsed = crate::propose::parse(&args[1..])?;
            for flag in ["--head", "--title", "--notes"] {
                if let Some(v) = parsed.optional(flag) {
                    let value = if flag == "--notes" {
                        inside(&root, Path::new(v))?.to_string_lossy().into_owned()
                    } else {
                        v.to_owned()
                    };
                    options.push(format!("{flag}={value}"));
                }
            }
            (
                Path::new(parsed.positional[0]),
                Path::new(parsed.required("--repo")?),
            )
        } else {
            land::inputs(&args[1..])?
        };
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
        let mut clone = Command::new("git");
        clone.args(["clone", "--bare", "--no-local", "--no-tags"]);
        if args[0] == "land" {
            clone.args(["--single-branch", "--branch", branch]);
        }
        let out = clone
            .arg("--")
            .arg(&url)
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
            command: args[0].clone(),
            options,
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
        // Only the publication destination changes. Both implementations still read live
        // eligibility and run their normal compare-and-swap push against a real Git repo.
        child
            .env("LIT_PARITY_LAND_TARGET", target)
            .env("GIT_ALLOW_PROTOCOL", "file");
        child
            .arg(&self.command)
            .arg(&self.receipt)
            .arg("--repo")
            .arg(&self.repo)
            .args(&self.options);
        Ok(())
    }
}
