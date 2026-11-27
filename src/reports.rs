//! Attribute failed units to changes and score predicted skips against Bun logs.
use crate::args::Args;
use crate::files;
use crate::git::Repo;
use crate::json::{object, Value};
use crate::json_paths::{path_key, read, render};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
type Result<T> = std::result::Result<T, String>;

fn strings(v: &Value) -> Result<BTreeSet<String>> {
    match v {
        Value::Object(map) => Ok(map.keys().cloned().collect()),
        _ => v
            .array()?
            .iter()
            .map(|v| v.string().map(str::to_owned))
            .collect(),
    }
}
fn array(items: impl IntoIterator<Item = String>) -> Value {
    Value::Array(items.into_iter().map(Value::String).collect())
}
fn observed(unit: &Value, key: &str) -> Result<BTreeSet<String>> {
    unit.get(key)
        .map(strings)
        .unwrap_or_else(|| Ok(BTreeSet::new()))
}
pub(crate) fn verdict(
    failed: BTreeMap<String, Value>,
    changes: &BTreeMap<String, Vec<String>>,
) -> Result<Value> {
    let mut suspects = BTreeMap::new();
    let mut named = BTreeSet::new();
    let mut unexplained = Vec::new();
    for (unit, reads) in failed {
        let mut seen = observed(&reads, "tracked")?;
        seen.extend(observed(&reads, "absent")?);
        let dirs = observed(&reads, "listings")?;
        let incomplete = reads.get("incomplete").is_some_and(Value::truthy);
        let mut hits = Vec::new();
        for (id, paths) in changes {
            if incomplete
                || paths.iter().any(|p| {
                    seen.contains(p)
                        || dirs.contains("")
                        || p.match_indices('/').any(|(i, _)| dirs.contains(&p[..i]))
                })
            {
                named.insert(id.clone());
                hits.push(id.clone());
            }
        }
        if hits.is_empty() {
            unexplained.push(unit.clone());
        }
        suspects.insert(unit, array(hits));
    }
    let all: BTreeSet<_> = changes.keys().cloned().collect();
    let empty = Value::Array(vec![]);
    let (act, eject, bisect, keep) = if !unexplained.is_empty() {
        ("rerun-on-base", empty.clone(), array(all), empty)
    } else {
        let keep = array(all.difference(&named).cloned());
        if named.len() == 1 {
            ("eject", array(named), empty, keep)
        } else {
            ("bisect", empty, array(named), keep)
        }
    };
    Ok(object([
        ("action", act.into()),
        ("eject", eject),
        ("bisect", bisect),
        ("keep", keep),
        ("rerun", array(unexplained)),
        ("suspects", Value::Object(suspects)),
    ]))
}
fn blame(args: &Args<'_>) -> Result<Value> {
    let repo = Repo::new(Path::new(args.optional("--workdir").unwrap_or(".")))?;
    let mut changes = BTreeMap::new();
    for spec in args.all("--change") {
        let message = "--change takes ID=BASE..HEAD";
        let (id, span) = spec.split_once('=').ok_or(message)?;
        let (base, head) = span.split_once("..").ok_or(message)?;
        if id.is_empty() || base.is_empty() || head.is_empty() {
            return Err(message.into());
        }
        let paths = repo.bytes(
            &[
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                "--end-of-options",
                base,
                head,
                "--",
            ],
            None,
        )?;
        changes.insert(
            path_key(id.as_bytes()),
            paths
                .split(|&b| b == 0)
                .filter(|p| !p.is_empty())
                .map(path_key)
                .collect(),
        );
    }
    let mut failed = BTreeMap::new();
    for file in &args.positional {
        match read(Path::new(file))?.get("failed_reads") {
            Some(Value::Object(units)) => failed.extend(units.clone()),
            Some(v) if v.truthy() => {
                return Err("Expected failed_reads to contain unit footprints".into())
            }
            _ => (),
        }
    }
    verdict(failed, &changes)
}

fn bun_header(line: &str) -> Option<&str> {
    let line = line.strip_prefix("::group::").unwrap_or(line);
    let path = line.strip_suffix(':')?;
    let (prefix, ext) = path.rsplit_once('.')?;
    if prefix.is_empty()
        || path.chars().any(char::is_whitespace)
        || path.chars().skip(1).any(|c| c == ':')
    {
        return None;
    }
    let ext = ext.strip_prefix(['c', 'm']).unwrap_or(ext);
    matches!(ext, "js" | "jsx" | "ts" | "tsx").then_some(path.strip_prefix("./").unwrap_or(path))
}
/// Outside a terminal, bun prints a file's path, then a colon, only before that file's failures, so the files
/// named that way are the ones that failed. Under GitHub Actions it prints every file's path that way, as a log
/// group that ends with "::endgroup::", so a file counts only when a failure line follows its path
/// (concurrent_merge/bun_log.py).
pub(crate) fn bun_failed(text: &str) -> BTreeSet<String> {
    let mut failed = BTreeSet::new();
    let mut current = None;
    for line in text.split([
        '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
        '\u{2029}',
    ]) {
        if let Some(path) = bun_header(line) {
            current = Some(path);
        } else if line == "::endgroup::" {
            current = None;
        } else if let Some(path) = current {
            if line.starts_with("(fail)")
                || line.starts_with("# Unhandled error")
                || line.starts_with("error: ")
            {
                failed.insert(path_key(path.as_bytes()));
            }
        }
    }
    failed
}
fn score(args: &Args<'_>) -> Result<Value> {
    let plan = read(Path::new(args.positional[0]))?;
    let reusable = strings(plan.field("reusable")?)?;
    let mut failed = BTreeSet::new();
    for log in args.all("--log") {
        let bytes = files::read(Path::new(log))?;
        failed.extend(bun_failed(&String::from_utf8_lossy(&bytes)));
    }
    Ok(object([
        ("commit", plan.field("commit")?.clone()),
        ("units", plan.field("units")?.clone()),
        ("predicted_reusable", (reusable.len() as u64).into()),
        (
            "seconds_predicted_reusable",
            plan.field("seconds_predicted_reusable")?.clone(),
        ),
        ("failed_in_run", (failed.len() as u64).into()),
        ("unsound", array(reusable.intersection(&failed).cloned())),
        ("first_reasons", plan.field("first_reasons")?.clone()),
    ]))
}

fn execute(
    argv: &[String],
    name: &str,
    help: &str,
    flags: &[&'static str],
    required: &str,
    run: fn(&Args<'_>) -> Result<Value>,
) -> i32 {
    if argv
        .iter()
        .take_while(|s| s.as_str() != "--")
        .any(|s| matches!(s.as_str(), "-h" | "--help"))
    {
        print!("{help}");
        return 0;
    }
    let res = (|| {
        let args = Args::parse(argv, flags)
            .and_then(|args| {
                let mut missing = Vec::new();
                if args.positional.is_empty() {
                    missing.push(if name == "blame" { "receipts" } else { "plan" });
                }
                if !args.has(required) {
                    missing.push(required);
                }
                if !missing.is_empty() {
                    return Err(format!(
                        "the following arguments are required: {}",
                        missing.join(", ")
                    ));
                }
                Ok(args)
            })
            .map_err(|e| crate::args::error(name, help, e))?;
        if name == "score-bun" && args.positional.len() > 1 {
            return Err(format!(
                "{}\nconcurrent-merge: error: unrecognized arguments: {}",
                crate::args::ROOT_USAGE,
                args.positional[1..].join(" ")
            ));
        }
        let result = run(&args)?;
        let display = render(&result);
        if let Some(path) = args.optional("--out") {
            let data = format!("{display}\n");
            if name == "blame" {
                files::replace(Path::new(path), data.as_bytes())
            } else {
                std::fs::write(path, data)
            }
            .map_err(|e| e.to_string())?;
        }
        if name == "blame" {
            let fields = ["action", "eject", "bisect", "rerun"];
            let entries = fields
                .iter()
                .map(|k| {
                    let value = render(result.field(k)?);
                    let one_line = value.lines().map(str::trim_start).collect::<String>();
                    Ok(format!("\"{k}\": {one_line}"))
                })
                .collect::<Result<Vec<_>>>()?;
            println!("{{{}}}", entries.join(", "));
        } else {
            println!("{display}");
        }
        Ok(())
    })();
    match res {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}
pub fn blame_command(argv: &[String]) -> i32 {
    execute(
        argv,
        "blame",
        include_str!("help/blame.txt"),
        &["--workdir", "--change", "--out"],
        "--change",
        blame,
    )
}
pub fn score_command(argv: &[String]) -> i32 {
    execute(
        argv,
        "score-bun",
        include_str!("help/score-bun.txt"),
        &["--log", "--out"],
        "--log",
        score,
    )
}
