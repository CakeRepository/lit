//! Checks the lit binary itself: it reports its version, runs today's lit
//! without writing anything beside its source, and lit parity works on a batch.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn lit() -> Command {
    Command::new(env!("CARGO_BIN_EXE_lit"))
}

fn have(program: &str) -> bool {
    Command::new(program).arg("--version").output().is_ok()
}

fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .args(args)
        .current_dir(dir)
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@example.com")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@example.com")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

fn temp(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("lit-cli-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

/// A folder with batch.git and queue.json for one PR that adds b.txt.
fn scratch_batch(name: &str) -> PathBuf {
    let work = temp(name);
    let src = work.join("src");
    fs::create_dir(&src).unwrap();
    git(&src, &["init", "-q", "-b", "main"]);
    fs::write(src.join("a.txt"), "a\n").unwrap();
    git(&src, &["add", "."]);
    git(&src, &["commit", "-qm", "base"]);
    let base = git(&src, &["rev-parse", "HEAD"]);
    git(&src, &["checkout", "-qb", "pr"]);
    fs::write(src.join("b.txt"), "b\n").unwrap();
    git(&src, &["add", "."]);
    git(&src, &["commit", "-qm", "add b"]);
    let head = git(&src, &["rev-parse", "HEAD"]);
    git(&work, &["clone", "-q", "--bare", "src", "batch.git"]);
    fs::write(
        work.join("queue.json"),
        format!(
            r#"{{"repository":"example/repo","branch":"main","base":"{base}","changes":[{{"number":1,"author":"octocat","head":"{head}"}}]}}"#
        ),
    )
    .unwrap();
    work
}

/// Every path under root except .git and target, for a before/after comparison.
fn tree(root: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    fn walk(p: &Path, out: &mut Vec<PathBuf>) {
        let name = p
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        if name == ".git" || name == "target" {
            return;
        }
        out.push(p.to_path_buf());
        if p.is_dir() && !p.is_symlink() {
            let mut entries: Vec<_> = fs::read_dir(p)
                .unwrap()
                .flatten()
                .map(|e| e.path())
                .collect();
            entries.sort();
            for e in entries {
                walk(&e, out);
            }
        }
    }
    walk(root, &mut out);
    out
}

#[test]
fn version() {
    let out = lit().arg("version").output().unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success() && text.starts_with("lit ") && text.contains("embedded python"),
        "{text}"
    );
}

/// Drives a real batch through the embedded Python and checks that nothing was
/// written next to the binary's source or into the checkout.
#[test]
fn runs_todays_lit() {
    if !have("python3") {
        eprintln!("skipped: python3 not installed");
        return;
    }
    let work = scratch_batch("run");
    let checkout = Path::new(env!("CARGO_MANIFEST_DIR"));
    let before = tree(checkout);
    let out = lit()
        .args([
            "run",
            "queue.json",
            "--repo",
            "batch.git",
            "--state",
            "state",
            "--test",
            "test",
            "-f",
            "b.txt",
        ])
        .current_dir(&work)
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success(),
        "lit run: {stdout}{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let receipt = fs::read_to_string(stdout.trim().lines().last().unwrap()).unwrap();
    // Python writes text files with CRLF on Windows, so compare without whitespace.
    let compact: String = receipt.chars().filter(|c| !c.is_whitespace()).collect();
    assert!(compact.contains("\"tested\":[1]"), "{receipt}");
    assert_eq!(
        tree(checkout),
        before,
        "lit wrote into its own source tree (bytecode or other files)"
    );
    fs::remove_dir_all(work).unwrap();
}

/// run is not in Rust yet, so parity checks that two Python runs of the same
/// batch agree, commit IDs included. The test takes a second, so the two sides
/// run in different seconds: their commit IDs match only because batch commits
/// are made at SOURCE_DATE_EPOCH.
#[test]
fn parity_on_a_batch() {
    if !have("python3") {
        eprintln!("skipped: python3 not installed");
        return;
    }
    let work = scratch_batch("parity");
    let out = lit()
        .args([
            "parity",
            "--",
            "run",
            "queue.json",
            "--repo",
            "batch.git",
            "--state",
            "state",
        ])
        .args(["--test", "sh", "-c", "sleep 1 && test -f b.txt"])
        .current_dir(&work)
        .output()
        .unwrap();
    let text =
        String::from_utf8_lossy(&out.stdout).into_owned() + &String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && text.contains("the runs match (exit 0)"),
        "{text}"
    );
    assert!(
        !work.join("state").exists(),
        "lit parity ran the command in the real folder"
    );
    assert!(
        !lit().arg("parity").status().unwrap().success(),
        "lit parity with no command should fail"
    );
    fs::remove_dir_all(work).unwrap();
}
