//! Run the selected test command on exact combined commits and retain the passing revisions.
use crate::args::Args;
use crate::assembly;
use crate::changes::{self, Change};
use crate::files;
use crate::git::Repo;
use crate::json::{self, object, Value};
use crate::merge::{self, Engine, Owners};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;
use std::process::Command;
use std::time::SystemTime;

type Result<T> = std::result::Result<T, String>;
fn numbers<'a>(items: impl IntoIterator<Item = &'a u64>) -> Value {
    Value::Array(items.into_iter().map(|&n| n.into()).collect())
}
fn note(owners: &mut Owners, paths: impl IntoIterator<Item = Vec<u8>>, change: &Change) {
    let name = change.participant();
    for path in paths {
        owners.entry(path).or_default().insert(name.clone());
    }
}

pub(crate) fn private_dir(prefix: &'static str) -> tempfile::Builder<'static, 'static> {
    let mut builder = tempfile::Builder::new();
    builder.prefix(prefix).rand_bytes(8);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(fs::Permissions::from_mode(0o700));
    }
    builder
}

pub(crate) fn checkout(repo: &Repo, destination: &Path, oid: &str) -> Result<()> {
    let work = Repo::new(destination)?;
    let src = repo
        .path()
        .to_str()
        .ok_or("Non-Unicode repository directory")?;
    let quote = |s: &str| format!("'{}'", s.replace('\'', "'\\''"));
    let pack = format!(
        "git -c {} -c {} upload-pack",
        quote(&format!("safe.directory={src}")),
        quote(&format!("safe.directory={src}/.git"))
    );
    work.text(
        &[
            "clone",
            "-q",
            "--shared",
            "--no-checkout",
            "--upload-pack",
            &pack,
            src,
            ".",
        ],
        None,
    )?;
    work.text(&["remote", "remove", "origin"], None)?;
    work.text(&["checkout", "-q", "--detach", oid], None)?;
    Ok(())
}

fn candidate_test(repo: &Repo, directory: &Path, oid: &str, args: &[String]) -> Result<bool> {
    let tmp = private_dir("candidate-")
        .tempdir()
        .map_err(|e| e.to_string())?;
    checkout(repo, tmp.path(), oid)?;
    let path = directory.join(format!("{oid}.log"));
    let log = fs::File::create(&path).map_err(|e| e.to_string())?;
    let out = Command::new(&args[0])
        .args(&args[1..])
        .current_dir(tmp.path())
        .stdout(log.try_clone().map_err(|e| e.to_string())?)
        .stderr(log)
        .status()
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                format!("[Errno 2] No such file or directory: '{}'", args[0])
            } else {
                e.to_string()
            }
        })?;
    #[cfg(unix)]
    let code = {
        use std::os::unix::process::ExitStatusExt;
        out.code().unwrap_or_else(|| -out.signal().unwrap_or(1))
    };
    #[cfg(not(unix))]
    let code = out.code().unwrap_or(2);
    match code {
        0 => Ok(true),
        1 => Ok(false),
        _ => Err(format!(
            "Test infrastructure exited {code}. See {}",
            path.display()
        )),
    }
}

struct Batch<'a> {
    engine: Engine<'a>,
    directory: &'a Path,
    test: &'a [String],
    stamp: i64,
    tip: String,
    accepted: BTreeSet<u64>,
    owners: Owners,
    failed: BTreeSet<String>,
    history: Vec<Value>,
}

impl Batch<'_> {
    fn attempt(&mut self, group: &[Change]) -> Result<()> {
        let mut ours = self.tip.clone();
        let mut owners = self.owners.clone();
        let mut members = Vec::new();
        let mut pending = Vec::new();
        let mut fixes = Vec::new();
        let mut available = self.accepted.clone();
        let fast = if group.iter().all(|c| c.requires.is_subset(&available)) {
            assembly::batch(self.engine.repo, &ours, group, self.engine.jobs, self.stamp)?
        } else {
            None
        };
        let kind = if let Some((tip, mut paths)) = fast {
            ours = tip;
            for change in group {
                members.push(change.number);
                note(
                    &mut owners,
                    paths.remove(&change.head).ok_or("Incomplete change set")?,
                    change,
                );
            }
            "disjoint"
        } else {
            for change in group {
                let missing: Vec<_> = change.requires.difference(&available).collect();
                if !missing.is_empty() {
                    pending.push(object([
                        ("number", change.number.into()),
                        ("author", change.author.as_str().into()),
                        ("requires", numbers(missing)),
                    ]));
                    continue;
                }
                let merged = self.engine.merge(&ours, change, &owners, self.stamp)?;
                fixes.extend(merged.fixes);
                if let Some(head) = merged.head {
                    note(
                        &mut owners,
                        merge::path_changes(self.engine.repo, &ours, &head)?,
                        change,
                    );
                    ours = head;
                    members.push(change.number);
                    available.insert(change.number);
                } else {
                    pending.push(object([
                        ("number", change.number.into()),
                        ("author", change.author.as_str().into()),
                        ("conflicts", Value::Array(merged.conflicts)),
                    ]));
                }
            }
            "three-way"
        };
        let entry = object([
            ("assembly", kind.into()),
            ("base", self.tip.as_str().into()),
            ("candidate", ours.as_str().into()),
            ("members", numbers(&members)),
            ("pending", Value::Array(pending)),
            ("resolved", Value::Array(fixes)),
            ("result", "empty".into()),
        ]);
        self.history.push(entry);
        let Value::Object(record) = self.history.last_mut().unwrap() else {
            unreachable!()
        };
        if members.is_empty() {
            return Ok(());
        }
        let tree = self
            .engine
            .repo
            .text(&["rev-parse", &format!("{ours}^{{tree}}")], None)?;
        record.insert("tree".into(), Value::String(tree));
        record.insert("result".into(), "interrupted".into());
        let result = if self.failed.contains(&ours) {
            "known-failure"
        } else if candidate_test(self.engine.repo, self.directory, &ours, self.test)? {
            "pass"
        } else {
            self.failed.insert(ours.clone());
            "fail"
        };
        record.insert("result".into(), result.into());
        if result == "pass" {
            self.owners = owners;
            self.tip = ours;
            self.accepted.extend(members);
        } else if group.len() > 1 {
            let (left, right) = group.split_at(group.len() / 2);
            self.attempt(left)?;
            self.attempt(right)?;
        } else {
            let one = &group[0];
            record.insert(
                "returned".into(),
                object([
                    ("number", one.number.into()),
                    ("head", one.head.as_str().into()),
                    ("author", one.author.as_str().into()),
                    ("tested_on", self.tip.as_str().into()),
                ]),
            );
        }
        Ok(())
    }
}

fn shadow(repo: &Repo, snapshot: &Path, args: &Args<'_>, directory: &Path) -> Result<()> {
    let Some(store) = args.optional("--shadow-store") else {
        return Ok(());
    };
    let options = crate::footprint::shadow::Options {
        store: Path::new(store),
        job: args.required("--shadow-job")?,
        proofs: args.optional("--shadow-proofs").map(Path::new),
        discover: args.all("--shadow-discover").to_vec(),
        strip: args.optional("--shadow-strip").unwrap_or(""),
    };
    crate::footprint::shadow::for_run(repo, snapshot, &options, directory)
}

fn argument_error(message: String) -> String {
    crate::args::error("run", include_str!("help/run.txt"), message)
}
fn test_option(arg: &str) -> bool {
    let flag = arg.split('=').next().unwrap_or(arg);
    flag.len() > 2 && "--test".starts_with(flag)
}

fn execute(argv: &[String]) -> Result<i32> {
    let mut options = Vec::new();
    let mut test = None;
    let mut pos = 0;
    while let Some(arg) = argv.get(pos) {
        pos += 1;
        if arg == "--" {
            options.push(arg.clone());
            options.extend_from_slice(&argv[pos..]);
            break;
        }
        if test_option(arg) {
            if let Some((_, inline)) = arg.split_once('=') {
                test = Some(vec![inline.to_owned()]);
            } else {
                test = Some(argv[pos..].to_vec());
                break;
            }
        } else {
            options.push(arg.clone());
        }
    }
    let args = Args::parse(
        &options,
        &[
            "--repo",
            "--state",
            "--jobs",
            "--shadow-store",
            "--shadow-job",
            "--shadow-proofs",
            "--shadow-discover",
            "--shadow-strip",
        ],
    )
    .map_err(argument_error)?;
    let job_count = args
        .optional("--jobs")
        .map(|s| {
            s.parse::<i64>()
                .map_err(|_| argument_error(format!("argument --jobs: invalid int value: '{s}'")))
        })
        .transpose()?;
    let mut missing = Vec::new();
    if args.positional.is_empty() {
        missing.push("snapshot");
    }
    if test.is_none() {
        missing.push("--test");
    }
    for flag in ["--state", "--repo"] {
        if !args.has(flag) {
            missing.push(flag);
        }
    }
    if !missing.is_empty() {
        return Err(argument_error(format!(
            "the following arguments are required: {}",
            missing.join(", ")
        )));
    }
    if args.positional.len() > 1 {
        return Err(argument_error(format!(
            "unrecognized arguments: {}",
            args.positional[1..].join(" ")
        )));
    }
    let test = test.unwrap();
    if args.has("--shadow-store") && !args.has("--shadow-job") {
        return Err("--shadow-store needs --shadow-job".into());
    }
    if test.is_empty() {
        return Err("Supply the full suite command after --test".into());
    }
    let snapshot_path = args.positional[0];
    let snapshot_path = Path::new(snapshot_path);
    let raw = files::read(snapshot_path)?;
    let snapshot = json::parse(std::str::from_utf8(&raw).map_err(|e| e.to_string())?)?;
    let repo = Repo::new(Path::new(args.required("--repo")?))?;
    let state = Path::new(args.required("--state")?);
    let jobs = match job_count {
        Some(n) => usize::try_from(n)
            .ok()
            .filter(|&n| n > 0)
            .ok_or("jobs must be positive")?,
        None => std::thread::available_parallelism().map_or(1, |n| n.get().min(8)),
    };
    let base = snapshot.field("base")?.string()?;
    let changes = changes::ordered(&repo, base, snapshot.field("changes")?.array()?)?;
    let runs = state.join("runs");
    fs::create_dir_all(&runs).map_err(|e| e.to_string())?;
    let directory = private_dir("batch-")
        .tempdir_in(runs)
        .map_err(|e| e.to_string())?
        .keep();
    let stamp = match std::env::var("SOURCE_DATE_EPOCH")
        .ok()
        .filter(|s| !s.is_empty())
    {
        Some(s) => s.parse().map_err(|_| "Invalid SOURCE_DATE_EPOCH")?,
        None => SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_secs() as i64,
    };
    let mut batch = Batch {
        engine: Engine {
            repo: &repo,
            state,
            jobs,
            hinted: BTreeSet::new(),
        },
        directory: &directory,
        test: &test,
        stamp,
        tip: base.into(),
        accepted: BTreeSet::new(),
        failed: BTreeSet::new(),
        owners: BTreeMap::new(),
        history: Vec::new(),
    };
    let tested = batch.attempt(&changes);
    merge::write_json(
        &directory.join("attempts.json"),
        &Value::Array(batch.history),
    )?;
    tested?;
    if !batch.accepted.is_empty() {
        let name = directory
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or("Missing batch name")?;
        repo.text(
            &[
                "update-ref",
                &format!("refs/concurrent-merge/tested/{name}"),
                &batch.tip,
            ],
            None,
        )?;
    }
    let result = object([
        ("snapshot", snapshot),
        ("final", Value::String(batch.tip)),
        ("tested", numbers(&batch.accepted)),
        (
            "command",
            Value::Array(test.iter().cloned().map(Value::String).collect()),
        ),
    ]);
    merge::write_json(&directory.join("result.json"), &result)?;
    shadow(&repo, snapshot_path, &args, &directory)?;
    println!(
        "{}",
        Repo::new(&directory)?.path().join("result.json").display()
    );
    Ok(i32::from(batch.accepted.len() != changes.len()))
}

pub fn command(argv: &[String]) -> i32 {
    if argv
        .iter()
        .take_while(|s| !(test_option(s) && !s.contains('=')))
        .any(|s| matches!(s.as_str(), "-h" | "--help"))
    {
        print!("{}", include_str!("help/run.txt"));
        return 0;
    }
    match execute(argv) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}
