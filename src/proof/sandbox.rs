//! Give each parity run its own proof destination, including when --remote names a live server.
use super::*;
use std::process::Command;

pub struct Sandbox {
    arguments: Vec<String>,
    seed: Option<PathBuf>,
}
fn inside(root: &Path, path: &Path) -> Result<PathBuf> {
    let mut existing = root.join(path);
    let mut tail = Vec::new();
    while !existing.exists() {
        let name = existing
            .file_name()
            .ok_or("Invalid proof parity path")?
            .to_owned();
        tail.push(name);
        if !existing.pop() {
            return Err("Invalid proof parity path".into());
        }
    }
    let real = existing.canonicalize().map_err(|e| e.to_string())?;
    let mut relative = real
        .strip_prefix(root)
        .map_err(|_| "Proof parity writes its outputs inside the input directory")?
        .to_owned();
    for name in tail.into_iter().rev() {
        relative.push(name);
    }
    if relative.as_os_str().is_empty() {
        relative.push(".");
    }
    Ok(relative)
}
impl Sandbox {
    pub fn prepare(source: &Path, work: &Path, argv: &[String]) -> Result<Option<Self>> {
        let Some(sub) = argv.get(1) else {
            return Ok(None);
        };
        let Some(form) = form(sub) else {
            return Ok(None);
        };
        if argv[2..]
            .iter()
            .take_while(|s| s.as_str() != "--")
            .any(|s| matches!(s.as_str(), "--help" | "-h"))
        {
            return Ok(None);
        }
        let args = parse(sub, &form, &argv[2..])?;
        let root = source.canonicalize().map_err(|e| e.to_string())?;
        let repo = inside(&root, Path::new(args.optional("--repo").unwrap_or(".")))?;
        let mut output = vec!["proof".to_owned(), sub.clone()];
        for p in &args.positional {
            output.push(inside(&root, Path::new(p))?.to_string_lossy().into_owned());
        }
        for &key in form.flags {
            if key == "--remote" {
                continue;
            }
            for raw in args.all(key) {
                let v = match key {
                    "--into" | "--out" | "--repo" => inside(&root, Path::new(raw))?
                        .to_string_lossy()
                        .into_owned(),
                    "--workdir" | "--store" | "--key" | "--revoked" | "--allowed-signers"
                    | "--audit-book" => {
                        if Path::new(raw).is_absolute() {
                            inside(&root, Path::new(raw))
                                .unwrap_or_else(|_| PathBuf::from(raw))
                                .to_string_lossy()
                                .into_owned()
                        } else {
                            raw.to_string()
                        }
                    }
                    _ => raw.to_string(),
                };
                output.push(format!("{key}={v}"));
            }
        }
        let seed = if let Some(remote) = args.optional("--remote") {
            let repo = root.join(repo);
            let effective = Repo::new(&repo)?
                .text(&["remote", "get-url", "--", remote], None)
                .unwrap_or_else(|_| remote.into());
            let target = work.join("proof-source.git");
            let out = Command::new("git")
                .args(["clone", "--mirror", "--no-local", "--"])
                .arg(effective)
                .arg(&target)
                .current_dir(&repo)
                .output()
                .map_err(|e| e.to_string())?;
            if !out.status.success() {
                return Err(format!(
                    "Read proof destination: {}",
                    String::from_utf8_lossy(&out.stderr).trim()
                ));
            }
            Some(target)
        } else {
            None
        };
        Ok(Some(Self {
            arguments: output,
            seed,
        }))
    }
    pub fn redirected(&self) -> bool {
        self.seed.is_some()
    }
    pub fn configure(&self, cmd: &mut Command, target: &Path) -> Result<()> {
        cmd.args(&self.arguments);
        if let Some(seed) = &self.seed {
            crate::parity::copy_tree(seed, target).map_err(|e| e.to_string())?;
            cmd.arg("--remote")
                .arg(target)
                .env("GIT_ALLOW_PROTOCOL", "file");
        }
        Ok(())
    }
}
