//! Embeds the Python lit (concurrent_merge) into the binary, so commands that
//! have not moved to Rust yet still run (CakeRepository/lit#35), and stamps the
//! version: LIT_VERSION at release time, else dev-<commit>.
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const KINDS: &[&str] = &["py", "ps1", "cs", "csproj"];

fn walk(dir: &Path, root: &Path, out: &mut Vec<(String, PathBuf)>) {
    let mut entries: Vec<_> = fs::read_dir(dir)
        .expect("read concurrent_merge")
        .map(|e| e.unwrap().path())
        .collect();
    entries.sort();
    for path in entries {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if path.is_dir() {
            if name != "__pycache__" {
                walk(&path, root, out);
            }
        } else if path
            .extension()
            .is_some_and(|e| KINDS.contains(&e.to_string_lossy().as_ref()))
        {
            let rel = path
                .strip_prefix(root)
                .unwrap()
                .components()
                .map(|c| c.as_os_str().to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join("/");
            out.push((rel, path.canonicalize().unwrap()));
        }
    }
}

fn main() {
    let root = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let mut files = Vec::new();
    walk(&root.join("concurrent_merge"), &root, &mut files);
    files.sort();
    let mut code = String::from("pub static FILES: &[(&str, &[u8])] = &[\n");
    for (name, path) in &files {
        code.push_str(&format!(
            "    ({:?}, include_bytes!({:?})),\n",
            name,
            path.to_string_lossy()
        ));
    }
    code.push_str("];\n");
    fs::write(
        PathBuf::from(env::var("OUT_DIR").unwrap()).join("embedded.rs"),
        code,
    )
    .unwrap();
    println!("cargo:rerun-if-changed=concurrent_merge");
    println!("cargo:rerun-if-env-changed=LIT_VERSION");

    let version = env::var("LIT_VERSION")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| {
            let rev = Command::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(&root)
                .output()
                .ok()
                .filter(|o| o.status.success())
                .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
                .unwrap_or_default();
            if rev.len() >= 12 {
                format!("dev-{}", &rev[..12])
            } else {
                "dev".to_string()
            }
        });
    println!("cargo:rustc-env=LIT_BUILD_VERSION={version}");
}
