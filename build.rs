//! Stamp releases with their tag and local builds with the checked-out revision.
use std::{env, process::Command};

fn git(args: &[&str]) -> Option<String> {
    let output = Command::new("git").args(args).output().ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn main() {
    println!("cargo:rerun-if-env-changed=LIT_VERSION");
    println!("cargo:rerun-if-changed=build.rs");
    let version = env::var("LIT_VERSION")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| {
            let mut refs = vec![String::from("HEAD"), String::from("packed-refs")];
            if let Some(branch) = git(&["symbolic-ref", "-q", "HEAD"]) {
                refs.push(branch);
            }
            for name in refs {
                if let Some(path) = git(&["rev-parse", "--git-path", &name]) {
                    if std::path::Path::new(&path).exists() {
                        println!("cargo:rerun-if-changed={path}");
                    }
                }
            }
            git(&["rev-parse", "--short=12", "HEAD"])
                .map_or_else(|| "dev".into(), |rev| format!("dev-{rev}"))
        });
    println!("cargo:rustc-env=LIT_BUILD_VERSION={version}");
}
