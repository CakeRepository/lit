//! Runs one lit command twice, once with the Python lit and once with the Rust
//! lit, on two copies of the same inputs, and reports every way the two runs
//! differ (CakeRepository/lit#35).
//!
//! A command moves to Rust only when its parity cases match. What is compared:
//! the exit code, stdout and stderr, every file left in the folder (JSON files
//! parsed, so key order and layout do not matter), and the refs of every Git
//! repository in it. What may differ, because it differs between any two runs:
//! the folder itself, the random part of a batch-XXXXXXXX run folder, ISO 8601
//! times, durations in "seconds" fields, and the contents of test logs (*.log),
//! which hold the test command's own output. Batch commits are made at
//! SOURCE_DATE_EPOCH, which parity pins, so commit IDs must match exactly.

use crate::json::Value;
use crate::json_paths as paths;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU32, Ordering};

pub struct Side {
    pub name: &'static str,
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
    pub files: BTreeMap<String, String>,
}

pub struct Options {
    pub program: PathBuf,
    pub source: PathBuf,
    pub args: Vec<String>,
    pub epoch: String,
    pub ignore: Vec<String>,
    /// Whether args[0] has a Rust implementation; without one, Python runs twice.
    pub native: bool,
}

pub struct Report {
    pub python: Side,
    pub rust: Side,
    pub diffs: Vec<String>,
    pub work: PathBuf,
}

static COUNTER: AtomicU32 = AtomicU32::new(0);

/// The path canonicalize gives, without Windows' \\?\ prefix on a drive
/// path, which no other program prints.
fn real_path(p: &Path) -> Option<PathBuf> {
    let real = fs::canonicalize(p).ok()?;
    let text = real.to_string_lossy();
    match text.strip_prefix(r"\\?\") {
        Some(rest) if rest.as_bytes().get(1) == Some(&b':') => Some(PathBuf::from(rest)),
        _ => Some(real),
    }
}

fn work_dir() -> io::Result<PathBuf> {
    // The temp dir's long, real form: on Windows TEMP can be an 8.3 short
    // name (RUNNER~1), which the programs under test print in full.
    let temp = std::env::temp_dir();
    let temp = real_path(&temp).unwrap_or(temp);
    loop {
        let dir = temp.join(format!(
            "lit-parity-{}-{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::SeqCst)
        ));
        match fs::create_dir(&dir) {
            Ok(()) => return Ok(dir),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
}

/// Copies the source twice, runs the command in each copy and compares.
/// On error, the second value names the folder holding what was made so far.
pub fn run(o: &Options) -> Result<Report, (String, Option<PathBuf>)> {
    let work = work_dir().map_err(|e| (e.to_string(), None))?;
    let fail = |e: String| (e, Some(work.clone()));
    let landing = if matches!(o.args.first().map(String::as_str), Some("land" | "propose"))
        && !o
            .args
            .iter()
            .take_while(|a| a.as_str() != "--")
            .any(|a| a == "--help" || a == "-h")
    {
        Some(crate::parity_land::Landing::prepare(&o.source, &work, &o.args).map_err(fail)?)
    } else {
        None
    };
    let proof = if o.args.first().is_some_and(|s| s == "proof") {
        crate::proof::sandbox::Sandbox::prepare(&o.source, &work, &o.args).map_err(fail)?
    } else {
        None
    };
    let mut sides = Vec::new();
    for name in ["python", "rust"] {
        let dir = work.join(name);
        copy_tree(&o.source, &dir).map_err(|e| fail(format!("copy inputs: {e}")))?;
        let mut cmd = Command::new(&o.program);
        // Dispatch outside the observed job environment. A job may use, or
        // record a digest of, every variable it inherits.
        if o.args.first().is_some_and(|s| !s.starts_with('-')) {
            cmd.arg(format!("--internal-parity-{name}"));
        }
        let target = work.join(format!("{name}-landing.git"));
        if let Some(landing) = &landing {
            landing.configure(&mut cmd, &dir, &target).map_err(fail)?;
        } else if let Some(proof) = &proof {
            proof.configure(&mut cmd, &target).map_err(fail)?;
            let date = format!("@{} +0000", o.epoch);
            cmd.env(
                "LIT_PARITY_PROOF_TIME",
                o.epoch
                    .parse::<i64>()
                    .ok()
                    .and_then(|s| jiff::Timestamp::from_second(s).ok())
                    .ok_or_else(|| fail("Invalid parity epoch".into()))?
                    .to_string()
                    .replace('Z', "+00:00"),
            );
            cmd.env("LIT_PARITY_BOOK_DATE", &date)
                .env("GIT_AUTHOR_DATE", &date)
                .env("GIT_COMMITTER_DATE", &date);
        } else {
            cmd.args(&o.args);
        }
        let out = cmd
            .current_dir(&dir)
            .env("SOURCE_DATE_EPOCH", &o.epoch)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .output()
            .map_err(|e| fail(format!("run {}: {e}", o.program.display())))?;
        let renames = rename_runs(&dir).map_err(|e| fail(e.to_string()))?;
        let n = Normalizer {
            dir: &dir,
            renames,
            ignore: &o.ignore,
        };
        let mut files = n.tree().map_err(fail)?;
        if landing.is_some() || proof.as_ref().is_some_and(|p| p.redirected()) {
            let draft = target.join("lit-draft.json");
            if draft.exists() {
                let raw = fs::read_to_string(draft).map_err(|e| fail(e.to_string()))?;
                files.insert(
                    "(draft pull request)".into(),
                    paths::render(&paths::parse(raw.as_bytes()).map_err(fail)?),
                );
            }
            files.insert(
                "(landing target refs)".into(),
                git_refs(&target).map_err(fail)?,
            );
        }
        sides.push(Side {
            name,
            code: exit_code(&out.status),
            stdout: match paths::parse(&out.stdout) {
                Ok(v @ (Value::Object(_) | Value::Array(_))) => paths::render(&n.json(v, "")),
                _ => n.stream(&out.stdout),
            },
            stderr: n.stream(&out.stderr),
            files,
        });
    }
    let rust = sides.pop().unwrap();
    let python = sides.pop().unwrap();
    let diffs = compare(&python, &rust);
    Ok(Report {
        python,
        rust,
        diffs,
        work,
    })
}

pub fn exit_code(status: &std::process::ExitStatus) -> i32 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return 128 + sig;
        }
    }
    status.code().unwrap_or(2)
}

/// Lists every difference between two finished sides.
pub fn compare(a: &Side, b: &Side) -> Vec<String> {
    let mut diffs = Vec::new();
    if a.code != b.code {
        diffs.push(format!(
            "exit code: {} {}, {} {}",
            a.name, a.code, b.name, b.code
        ));
    }
    if a.stdout != b.stdout {
        diffs.push(format!(
            "stdout differs:\n{}",
            excerpt(a, b, &a.stdout, &b.stdout)
        ));
    }
    if a.stderr != b.stderr {
        diffs.push(format!(
            "stderr differs:\n{}",
            excerpt(a, b, &a.stderr, &b.stderr)
        ));
    }
    let paths: BTreeSet<&String> = a.files.keys().chain(b.files.keys()).collect();
    for p in paths {
        match (a.files.get(p), b.files.get(p)) {
            (Some(_), None) => diffs.push(format!("{p}: only {} wrote it", a.name)),
            (None, Some(_)) => diffs.push(format!("{p}: only {} wrote it", b.name)),
            (Some(x), Some(y)) if x != y => {
                diffs.push(format!("{p} differs:\n{}", excerpt(a, b, x, y)))
            }
            _ => {}
        }
    }
    diffs
}

fn excerpt(a: &Side, b: &Side, x: &str, y: &str) -> String {
    let (xl, yl): (Vec<&str>, Vec<&str>) = (x.split('\n').collect(), y.split('\n').collect());
    let i = xl.iter().zip(&yl).take_while(|(p, q)| p == q).count();
    let line = |l: &[&str]| l.get(i).map(|s| s.to_string()).unwrap_or("(end)".into());
    format!(
        "  line {}\n  {}: {}\n  {}: {}",
        i + 1,
        a.name,
        line(&xl),
        b.name,
        line(&yl)
    )
}

/// Copies src to dst, keeping modes and symlinks.
pub fn copy_tree(src: &Path, dst: &Path) -> io::Result<()> {
    let info = fs::symlink_metadata(src)?;
    if info.is_dir() {
        fs::create_dir_all(dst)?;
        fs::set_permissions(dst, info.permissions())?;
        for entry in fs::read_dir(src)? {
            let entry = entry?;
            copy_tree(&entry.path(), &dst.join(entry.file_name()))?;
        }
    } else if info.file_type().is_symlink() {
        let target = fs::read_link(src)?;
        #[cfg(unix)]
        std::os::unix::fs::symlink(&target, dst)?;
        #[cfg(windows)]
        {
            if src.is_dir() {
                std::os::windows::fs::symlink_dir(&target, dst)?;
            } else {
                std::os::windows::fs::symlink_file(&target, dst)?;
            }
        }
    } else if info.is_file() {
        fs::copy(src, dst)?;
    }
    // Sockets, devices and pipes are not inputs.
    Ok(())
}

fn is_run_folder(name: &str) -> bool {
    name.strip_prefix("batch-")
        .is_some_and(|r| r.len() == 8 && r.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_'))
}

fn is_git_dir(path: &Path) -> bool {
    path.join("HEAD").is_file() && path.join("objects").is_dir()
}

/// Gives each batch-XXXXXXXX folder, oldest first, a stable name (batch-1,
/// batch-2, ...) so the two sides line up, and returns the renames.
fn rename_runs(dir: &Path) -> io::Result<HashMap<String, String>> {
    let mut renames = HashMap::new();
    fn visit(dir: &Path, renames: &mut HashMap<String, String>) -> io::Result<()> {
        if is_git_dir(dir) {
            return Ok(());
        }
        let mut runs = Vec::new();
        let mut subdirs = Vec::new();
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            if !entry.file_type()?.is_dir() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().into_owned();
            if is_run_folder(&name) {
                runs.push((entry.metadata()?.modified()?, name));
            } else {
                subdirs.push(entry.path());
            }
        }
        runs.sort();
        for (i, (_, name)) in runs.iter().enumerate() {
            let stable = format!("batch-{}", i + 1);
            fs::rename(dir.join(name), dir.join(&stable))?;
            subdirs.push(dir.join(&stable));
            renames.insert(name.clone(), stable);
        }
        for sub in subdirs {
            visit(&sub, renames)?;
        }
        Ok(())
    }
    visit(dir, &mut renames)?;
    Ok(renames)
}

struct Normalizer<'a> {
    dir: &'a Path,
    renames: HashMap<String, String>,
    ignore: &'a [String],
}

/// Replaces every ISO 8601 time (2026-11-26T00:07:45, with optional fraction
/// and zone) with <time>.
fn mask_times(s: &str) -> String {
    let b = s.as_bytes();
    let digit = |i: usize| b.get(i).is_some_and(|c| c.is_ascii_digit());
    let at = |i: usize, c: u8| b.get(i) == Some(&c);
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut last = 0;
    while i < b.len() {
        let shape = [(0, 4), (5, 2), (8, 2), (11, 2), (14, 2), (17, 2)];
        let boundary = i == 0 || !b[i - 1].is_ascii_alphanumeric();
        if boundary
            && shape.iter().all(|&(o, n)| (0..n).all(|k| digit(i + o + k)))
            && at(i + 4, b'-')
            && at(i + 7, b'-')
            && at(i + 10, b'T')
            && at(i + 13, b':')
            && at(i + 16, b':')
        {
            let mut j = i + 19;
            if at(j, b'.') && digit(j + 1) {
                j += 1;
                while digit(j) {
                    j += 1;
                }
            }
            if at(j, b'Z') {
                j += 1;
            } else if (at(j, b'+') || at(j, b'-'))
                && digit(j + 1)
                && digit(j + 2)
                && at(j + 3, b':')
                && digit(j + 4)
                && digit(j + 5)
            {
                j += 6;
            }
            out.push_str(&s[last..i]);
            out.push_str("<time>");
            i = j;
            last = j;
            continue;
        }
        i += 1;
    }
    out.push_str(&s[last..]);
    out
}

impl Normalizer<'_> {
    fn stream(&self, bytes: &[u8]) -> String {
        let text = String::from_utf8_lossy(bytes);
        // Python's Windows text streams use CRLF. Rust's println uses LF.
        #[cfg(windows)]
        let text = text.replace("\r\n", "\n");
        self.text(&text)
    }

    fn text(&self, s: &str) -> String {
        let mut s = s.to_string();
        let mut dirs = vec![self.dir.to_string_lossy().into_owned()];
        if let Some(real) = real_path(self.dir) {
            let real = real.to_string_lossy().into_owned();
            if !dirs.contains(&real) {
                dirs.push(real);
            }
        }
        // Longest first, so a path that contains the other is replaced whole.
        dirs.sort_by_key(|d| std::cmp::Reverse(d.len()));
        for d in dirs {
            s = s.replace(&d, "<dir>");
        }
        for (from, to) in &self.renames {
            s = s.replace(from, to);
        }
        mask_times(&s)
    }

    fn ignored(&self, key: &str) -> bool {
        self.ignore.iter().any(|k| k == key)
    }

    fn json(&self, v: Value, key: &str) -> Value {
        match v {
            Value::Object(map) => Value::Object(
                map.into_iter()
                    .map(|(k, x)| {
                        let x = self.json(x, &k);
                        (k, x)
                    })
                    .collect(),
            ),
            Value::Array(items) => {
                Value::Array(items.into_iter().map(|x| self.json(x, key)).collect())
            }
            _ if self.ignored(key) => Value::String("<varies>".into()),
            Value::Number(_) if key == "seconds" || key.ends_with("_seconds") => {
                Value::String("<varies>".into())
            }
            Value::String(s) => Value::String(self.text(&s)),
            other => other,
        }
    }

    /// Reads every file and Git repository under the side's folder.
    fn tree(&self) -> Result<BTreeMap<String, String>, String> {
        let mut files = BTreeMap::new();
        self.visit(self.dir, "", &mut files)
            .map_err(|e| e.to_string())?;
        Ok(files)
    }

    fn visit(
        &self,
        path: &Path,
        rel: &str,
        files: &mut BTreeMap<String, String>,
    ) -> Result<(), String> {
        let info = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
        let child = |name: &str| {
            if rel.is_empty() {
                name.to_string()
            } else {
                format!("{rel}/{name}")
            }
        };
        if info.is_dir() {
            if is_git_dir(path) {
                files.insert(format!("{rel}/(refs)"), self.text(&git_refs(path)?));
                return Ok(());
            }
            let mut entries: Vec<_> = fs::read_dir(path)
                .map_err(|e| e.to_string())?
                .flatten()
                .collect();
            entries.sort_by_key(|e| e.file_name());
            for e in entries {
                self.visit(&e.path(), &child(&e.file_name().to_string_lossy()), files)?;
            }
        } else if info.file_type().is_symlink() {
            let link = fs::read_link(path).map_err(|e| e.to_string())?;
            files.insert(
                rel.to_string(),
                format!("symlink {}", self.text(&link.to_string_lossy())),
            );
        } else if info.is_file() {
            if rel.ends_with(".log") {
                files.insert(rel.to_string(), "(test log)".into());
                return Ok(());
            }
            let data = fs::read(path).map_err(|e| e.to_string())?;
            let text = match std::str::from_utf8(&data) {
                Ok(text) if !text.contains('\0') => text,
                _ => {
                    files.insert(
                        rel.to_owned(),
                        format!("binary SHA-256 {}", crate::sha256::hex(&data)),
                    );
                    return Ok(());
                }
            };
            if rel.ends_with(".json") {
                if let Ok(v) = paths::parse(text.as_bytes()) {
                    files.insert(rel.to_string(), paths::render(&self.json(v, "")));
                    return Ok(());
                }
            }
            files.insert(rel.to_string(), self.text(text));
        }
        Ok(())
    }
}

fn git_refs(dir: &Path) -> Result<String, String> {
    let out = Command::new("git")
        .args(["for-each-ref", "--format=%(refname) %(objectname)"])
        .env("GIT_DIR", dir)
        .output()
        .map_err(|e| format!("git refs of {}: {e}", dir.display()))?;
    if !out.status.success() {
        return Err(format!(
            "git refs of {}: {}",
            dir.display(),
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    let head = fs::read_to_string(dir.join("HEAD")).unwrap_or_default();
    Ok(format!(
        "HEAD {}\n{}",
        head.trim(),
        String::from_utf8_lossy(&out.stdout)
    ))
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    /// Runs sh in place of lit: the script is the "command" both sides run.
    fn run_sh(script: &str, setup: impl FnOnce(&Path)) -> Report {
        let src = work_dir().unwrap();
        setup(&src);
        let r = run(&Options {
            program: "sh".into(),
            source: src.clone(),
            args: vec!["-c".into(), script.into()],
            epoch: "1700000000".into(),
            ignore: vec![],
            native: true,
        })
        .map_err(|(e, _)| e)
        .unwrap();
        fs::remove_dir_all(src).unwrap();
        r
    }

    fn done(r: Report) -> Vec<String> {
        let _ = fs::remove_dir_all(&r.work);
        r.diffs
    }

    #[test]
    fn catches_output_that_differs_between_runs() {
        let d = done(run_sh(
            "mkdir out; od -An -N8 -tx1 /dev/urandom > out/value.txt; od -An -N8 -tx1 /dev/urandom",
            |_| {},
        ))
        .join("\n");
        assert!(
            d.contains("out/value.txt differs") && d.contains("stdout differs"),
            "{d}"
        );
    }

    #[test]
    fn catches_a_file_only_one_side_wrote() {
        let d = done(run_sh(
            r#"[ "$(basename "$PWD")" = rust ] && echo x > extra.txt; true"#,
            |_| {},
        ));
        assert_eq!(d, vec!["extra.txt: only rust wrote it"]);
    }

    #[test]
    fn catches_an_exit_code_that_differs() {
        let d = done(run_sh(
            r#"[ "$(basename "$PWD")" = rust ] && exit 3; exit 0"#,
            |_| {},
        ));
        assert_eq!(d, vec!["exit code: python 0, rust 3"]);
    }

    /// What differs between any two runs is allowed to: the folder, the random
    /// part of a run folder, times, durations and test logs.
    #[test]
    fn ignores_what_varies_between_any_two_runs() {
        let r = run_sh(
            r#"set -e
            d=state/runs/batch-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
            mkdir -p "$d"
            printf '{"receipt":"%s","started":"%s","seconds":%s,"unit_seconds":%s.5,"tested":[1]}' \
                "$PWD/$d/result.json" "$(date -u +%Y-%m-%dT%H:%M:%S.%N+00:00)" "$$" "$$" > "$d/result.json"
            od -An -N8 -tx1 /dev/urandom > "$d/abc.log"
            echo "$PWD/$d/result.json""#,
            |_| {},
        );
        assert!(
            r.python
                .files
                .contains_key("state/runs/batch-1/result.json"),
            "{:?}",
            r.python.files.keys()
        );
        assert_eq!(r.python.stdout, "<dir>/state/runs/batch-1/result.json\n");
        assert_eq!(done(r), Vec::<String>::new());
    }

    #[test]
    fn compares_json_by_value_not_layout() {
        let d = done(run_sh(
            r#"if [ "$(basename "$PWD")" = rust ]; then echo '{"b":2,"a":1}' > r.json; else printf '{\n "a": 1,\n "b": 2\n}' > r.json; fi"#,
            |_| {},
        ));
        assert!(d.is_empty(), "{d:?}");
        let d = done(run_sh(
            r#"if [ "$(basename "$PWD")" = rust ]; then echo '{"a":1,"b":3}' > r.json; else echo '{"a":1,"b":2}' > r.json; fi"#,
            |_| {},
        ));
        assert!(d.len() == 1 && d[0].contains("r.json differs"), "{d:?}");
    }

    /// Commits made at SOURCE_DATE_EPOCH get the same IDs on both sides, so their
    /// refs are compared exactly; a commit made at another time is caught.
    #[test]
    fn compares_git_refs() {
        let setup = |dir: &Path| {
            let ok = Command::new("git")
                .args(["init", "-q", "--bare"])
                .arg(dir.join("repo.git"))
                .status()
                .unwrap();
            assert!(ok.success());
        };
        let commit = r#"export GIT_DIR=repo.git GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x
            export GIT_AUTHOR_DATE="@$WHEN +0000" GIT_COMMITTER_DATE="@$WHEN +0000"
            tree=$(git mktree </dev/null)
            git update-ref refs/heads/main "$(echo batch | git commit-tree "$tree")""#;
        let d = done(run_sh(&format!("WHEN=$SOURCE_DATE_EPOCH; {commit}"), setup));
        assert!(d.is_empty(), "{d:?}");
        let d = done(run_sh(
            &format!("WHEN=$(od -An -N2 -tu2 /dev/urandom | tr -d ' '); {commit}"),
            setup,
        ));
        assert!(
            d.len() == 1 && d[0].contains("repo.git/(refs) differs"),
            "{d:?}"
        );
    }

    #[test]
    fn masks_iso_times_only() {
        assert_eq!(
            mask_times("at 2026-11-26T00:07:45.123+00:00 and 2026-11-26T00:07:45Z, not 2026-11-26 or x2026-11-26T00:07:45"),
            "at <time> and <time>, not 2026-11-26 or x2026-11-26T00:07:45"
        );
    }

    #[test]
    fn copy_tree_keeps_symlinks_and_modes() {
        use std::os::unix::fs::PermissionsExt;
        let src = work_dir().unwrap();
        fs::write(src.join("run.sh"), "#!/bin/sh\n").unwrap();
        fs::set_permissions(src.join("run.sh"), fs::Permissions::from_mode(0o755)).unwrap();
        std::os::unix::fs::symlink("run.sh", src.join("link")).unwrap();
        let dst = work_dir().unwrap().join("copy");
        copy_tree(&src, &dst).unwrap();
        assert_eq!(
            fs::metadata(dst.join("run.sh"))
                .unwrap()
                .permissions()
                .mode()
                & 0o111,
            0o111
        );
        assert_eq!(
            fs::read_link(dst.join("link")).unwrap(),
            PathBuf::from("run.sh")
        );
        fs::remove_dir_all(src).unwrap();
        fs::remove_dir_all(dst.parent().unwrap()).unwrap();
    }
}
