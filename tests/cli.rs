//! Checks the lit binary itself: it reports its version, runs today's lit
//! without writing anything beside its source, and lit parity works on a batch.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn lit() -> Command {
    Command::new(env!("CARGO_BIN_EXE_lit"))
}

fn python() -> String {
    std::env::var("LIT_PYTHON")
        .ok()
        .into_iter()
        .chain(["python3".into(), "python".into()])
        .find(|bin| {
            Command::new(bin)
                .args(["-c", "import sys; sys.exit(sys.version_info < (3, 12))"])
                .output()
                .is_ok_and(|o| o.status.success())
        })
        .expect("CLI parity tests require Python 3.12 or later")
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
    let python = python();
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
            &python,
            "-c",
            "from pathlib import Path; assert Path('b.txt').is_file()",
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

/// Check the reference and binary batch results, commit IDs included.
/// The test takes a second, so the two sides
/// run in different seconds: their commit IDs match only because batch commits
/// are made at SOURCE_DATE_EPOCH.
#[test]
fn parity_on_a_batch() {
    let python = python();
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
        .args([
            "--test",
            &python,
            "-c",
            "from pathlib import Path; import time; time.sleep(1); assert Path('b.txt').is_file()",
        ])
        .current_dir(&work)
        .output()
        .unwrap();
    let text =
        String::from_utf8_lossy(&out.stdout).into_owned() + &String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success() && text.contains("matches between Python and Rust (exit 0)"),
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

#[test]
fn agree_records_exact_bytes_without_python_and_matches_the_reference() {
    let work = temp("agree");
    git(&work, &["init", "--bare", "batch.git"]);
    let key = "a".repeat(64);
    let who = "pr:3:Zoë";
    let dir = work.join("state/conflicts");
    fs::create_dir_all(&dir).unwrap();
    let record = dir.join(format!("{key}.json"));
    fs::write(
        &record,
        format!(r#"{{"mode":"100644","authors":["{who}"]}}"#),
    )
    .unwrap();
    let contents = b"a\0b\r\n\xff";
    fs::write(work.join("replacement"), contents).unwrap();
    let args = |id, participant| {
        vec![
            "agree",
            id,
            participant,
            "replacement",
            "--repo=batch.git",
            "--state",
            "state",
        ]
    };
    let parity = |args: &[&str], code| {
        let out = lit()
            .arg("parity")
            .arg("--")
            .args(args)
            .current_dir(&work)
            .output()
            .unwrap();
        assert!(
            out.status.success()
                && String::from_utf8_lossy(&out.stdout)
                    .contains(&format!("matches between Python and Rust (exit {code})")),
            "{}{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
    };
    parity(&args(&key, who), 0);
    parity(&args("invalid-id", who), 2);
    parity(&args(&key, "other"), 2);
    let repo = work.join("batch.git");
    for _ in 0..2 {
        let out = lit()
            .args(args(&key, who))
            .env("LIT_PYTHON", work.join("missing-python"))
            .env_remove("LIT_IMPL")
            .current_dir(&work)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let blob = git(&repo, &["hash-object", "../replacement"]);
    assert_eq!(
        git(
            &repo,
            &[
                "rev-parse",
                &format!("refs/concurrent-merge/votes/{key}/{blob}")
            ]
        ),
        blob
    );
    assert_eq!(
        fs::read_dir(work.join("state/votes").join(&key))
            .unwrap()
            .count(),
        1
    );
    fs::write(&record, format!(r#"{{"mode":null,"authors":["{who}"]}}"#)).unwrap();
    parity(&args(&key, who), 2);
    fs::remove_dir_all(work).unwrap();
}

#[test]
fn native_failure_reports_match_reference_with_repeated_inputs() {
    let work = scratch_batch("failure-reports");
    fs::write(
        work.join("reads.json"),
        r#"{"failed_reads":{"A":{"tracked":["b.txt"]},"B":{"listings":[""]}}}"#,
    )
    .unwrap();
    fs::write(
        work.join("other.json"),
        r#"{"failed_reads":{"A":{"absent":["b.txt"]},"C":{"incomplete":["missed reads"]}}}"#,
    )
    .unwrap();
    fs::write(work.join("plan.json"),r#"{"commit":"abc","units":3,"reusable":["one.ctsx","one.ctsx","src/a.ts"],"seconds_predicted_reusable":12.5,"first_reasons":{"x":2}}"#).unwrap();
    fs::write(
        work.join("first.log"),
        "./one.ctsx:\n(pass) yes\n# Unhandled error\n./src/a.ts:\n(pass) yes\n",
    )
    .unwrap();
    fs::write(
        work.join("second.log"),
        "./suite.jsx:\nerror: no\nsuite.jsx:\n(fail) still broken\n",
    )
    .unwrap();
    let blame = [
        "blame",
        "reads.json",
        "other.json",
        "--change",
        "PR=main..pr",
        "--workdir",
        "src",
        "--out",
        "result/blame.json",
    ];
    let score = [
        "score-bun",
        "plan.json",
        "--log",
        "first.log",
        "--log",
        "second.log",
        "--out",
        "score.json",
    ];
    for argv in [
        &blame[..],
        &score[..],
        &["blame", "reads.json", "--change", "bad"][..],
    ] {
        let o = lit()
            .arg("parity")
            .args(argv)
            .current_dir(&work)
            .output()
            .unwrap();
        assert!(
            o.status.success(),
            "{}{}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        );
    }
    for argv in [&blame[..], &score[..]] {
        let o = lit()
            .args(argv)
            .env("LIT_PYTHON", work.join("missing"))
            .env_remove("LIT_IMPL")
            .current_dir(&work)
            .output()
            .unwrap();
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        if argv[0] == "blame" {
            assert_eq!(String::from_utf8(o.stdout).unwrap().lines().count(), 1);
        }
    }
    let report = fs::read_to_string(work.join("score.json")).unwrap();
    assert!(report.contains("one.ctsx") && report.contains("\"failed_in_run\": 2"));
    fs::remove_dir_all(work).unwrap();
}

#[test]
fn blame_preserves_distinct_non_unicode_git_paths() {
    use std::io::Write;
    use std::process::Stdio;
    let work = scratch_batch("blame-paths");
    let repo = work.join("batch.git");
    let parent = git(&repo, &["rev-parse", "pr"]);
    // A Git tree can contain names the host's filesystem cannot check out.
    let mut import = Command::new("git")
        .args(["fast-import", "--quiet"])
        .current_dir(&repo)
        .stdin(Stdio::piped())
        .spawn()
        .unwrap();
    let stream = format!("commit refs/heads/pr\ncommitter t <t@example.com> 1700000000 +0000\ndata 4\npath\nfrom {parent}\nM 100644 inline \"\\377\"\ndata 3\nnew\n\ndone\n");
    import
        .stdin
        .take()
        .unwrap()
        .write_all(stream.as_bytes())
        .unwrap();
    assert!(import.wait().unwrap().success());
    fs::write(work.join("reads.json"),r#"{"failed_reads":{"bad\udcff":{"tracked":["\udcff"]},"other\udcfe":{"tracked":["\udcfe"]},"literal":{"tracked":["\ufdd0\ue4ff"]}}}"#).unwrap();
    let o = lit()
        .args([
            "parity",
            "blame",
            "reads.json",
            "--change",
            "PR=pr~..pr",
            "--workdir",
            "batch.git",
            "--out",
            "verdict.json",
        ])
        .current_dir(&work)
        .output()
        .unwrap();
    assert!(
        o.status.success(),
        "{}{}",
        String::from_utf8_lossy(&o.stdout),
        String::from_utf8_lossy(&o.stderr)
    );
    fs::remove_dir_all(work).unwrap();
}
