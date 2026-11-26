//! Select .NET test classes by reachable IL, runtime, and per-class file observations.
mod inputs;
mod map;
mod runner;

use super::{cache, trace_cli, workspace::save, Result};
use crate::{
    args::{self, Args},
    files,
    git::Repo,
    json::{object, Value},
    json_paths as paths,
    sampling::Sampler,
    sha256, store,
};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

type Map = BTreeMap<String, Value>;
const HELP: &str = include_str!("../../help/dotnet-units.txt");
fn io(e: impl std::fmt::Display) -> String {
    e.to_string()
}
fn relative(path: &Path, base: &Path) -> Result<String> {
    Ok(path
        .strip_prefix(base)
        .map_err(io)?
        .iter()
        .map(|s| s.to_str().ok_or("Invalid UTF-8 in .NET input path"))
        .collect::<std::result::Result<Vec<_>, _>>()?
        .join("/"))
}
fn file_hash(path: &Path) -> Result<Value> {
    let hash = super::digests::file(path);
    if hash.len() != 64 {
        return Err(format!("Could not hash {}: {hash}", path.display()));
    }
    Ok(Value::String(hash))
}
fn index(v: Option<&Value>) -> Result<Map> {
    v.map(|v| v.object().cloned())
        .transpose()
        .map(Option::unwrap_or_default)
}
fn names(items: impl IntoIterator<Item = String>) -> Value {
    Value::Array(items.into_iter().map(Value::String).collect())
}
fn count(n: usize) -> Value {
    (n as u64).into()
}

fn decisions(
    program: &map::Program,
    old: &Map,
    record: &Value,
    inputs: &inputs::Inputs,
) -> Result<Map> {
    let mut reasons = Map::new();
    let mut digests = Map::new();
    for (name, unit) in &program.units {
        let reason = if let Some(prior) = old.get(name) {
            if prior.field("key")? != unit.field("key")? {
                let mut changed = BTreeSet::new();
                for i in unit.field("types")?.array()? {
                    let t = program
                        .type_names
                        .get(i.number()? as usize)
                        .ok_or("Invalid .NET type index")?
                        .string()?;
                    if record.get("types").and_then(|v| v.get(t)) != program.types.get(t) {
                        changed.insert(t.to_owned());
                    }
                }
                object([
                    ("kind", "code".into()),
                    ("types", names(changed.into_iter().take(5))),
                ])
            } else {
                let mut moved = Vec::new();
                for (k, expected) in index(prior.get("files"))? {
                    if !digests.contains_key(&k) {
                        digests.insert(k.clone(), inputs.digest(&k)?);
                    }
                    if digests[&k] != expected {
                        moved.push(k);
                    }
                }
                if moved.is_empty() {
                    object([("kind", "reusable".into())])
                } else {
                    object([
                        ("kind", "files".into()),
                        ("files", names(moved.into_iter().take(5))),
                    ])
                }
            }
        } else {
            let seen = record
                .get("classes")
                .and_then(|v| v.get(name))
                .is_some_and(Value::truthy);
            object([("kind", if seen { "globals" } else { "new" }.into())])
        };
        reasons.insert(name.clone(), reason);
    }
    Ok(reasons)
}
fn duration(v: Option<&Value>) -> f64 {
    match v {
        Some(Value::Number(s)) => s.parse().unwrap_or(0.),
        _ => 0.,
    }
}
fn run(argv: &[String]) -> Result<i32> {
    let err = |e| args::error("dotnet-units", HELP, e);
    let a = Args::parse(
        argv,
        &[
            "--store",
            "--job",
            "--workdir",
            "--tests-dll",
            "--run",
            "--mode",
            "--jobs",
            "--audit-rate",
            "--log",
            "--receipt",
            "--list",
        ],
    )
    .map_err(err)?;
    if !a.positional.is_empty() {
        return Err(err(format!(
            "unrecognized arguments: {}",
            a.positional.join(" ")
        )));
    }
    let store = Path::new(a.required("--store").map_err(err)?);
    let job = a.required("--job").map_err(err)?;
    let tests = Path::new(a.required("--tests-dll").map_err(err)?);
    let template = trace_cli::template(a.required("--run").map_err(err)?)?;
    if !template.iter().any(|s| s == "{filter}") {
        return Err(
            "The test command needs {filter} where lit puts --filter and the classes to run".into(),
        );
    }
    let mode = a.optional("--mode").unwrap_or("enforce");
    if !matches!(mode, "enforce" | "refresh" | "sweep" | "plan") {
        return Err(err(format!("argument --mode: invalid choice: '{mode}' (choose from 'enforce', 'refresh', 'sweep', 'plan')")));
    }
    let jobs = a
        .optional("--jobs")
        .map(|s| {
            s.parse::<isize>()
                .map_err(|_| err(format!("argument --jobs: invalid int value: '{s}'")))
        })
        .transpose()?
        .unwrap_or(0);
    let jobs = if jobs == 0 {
        std::thread::available_parallelism().map_or(1, |n| (n.get() / 2).max(1))
    } else {
        jobs.max(1) as usize
    };
    let rate = a.optional("--audit-rate").unwrap_or("0");
    let rate = rate.parse::<f64>().map_err(|_| {
        err(format!(
            "argument --audit-rate: invalid float value: '{rate}'"
        ))
    })?;
    let mut log = a
        .optional("--log")
        .map(|p| {
            fs::OpenOptions::new()
                .append(true)
                .create(true)
                .open(p)
                .map_err(|e| files::error(Path::new(p), e))
        })
        .transpose()?;
    let repo = Repo::new(Path::new(a.optional("--workdir").unwrap_or(".")))?;
    let tests = tests.canonicalize().map_err(|e| files::error(tests, e))?;
    let commit = repo.text(&["rev-parse", "HEAD"], None)?;
    let mut rng = Sampler::from_commit(&commit);
    let started = cache::iso(cache::now()?);
    let record_path = store::job_dir(store, job)?.join("dotnet-record.json");
    let record = paths::read(&record_path)
        .ok()
        .filter(|v| v.get("version") == Some(&2u64.into()))
        .unwrap_or(Value::Null);
    let program = map::load(store, &tests, &mut log)?;
    let old = if record.get("globals") == Some(&program.globals) {
        index(record.get("classes"))?
    } else {
        Map::new()
    };
    let inputs = inputs::Inputs::new(
        &repo,
        tests.parent().ok_or("Missing test output folder")?,
        &program.content,
    )?;
    let decisions = decisions(&program, &old, &record, &inputs)?;
    let runner = runner::Runner {
        template: &template,
        root: repo.path(),
        inputs,
    };
    let mut to_run = Vec::new();
    let mut reusable = 0;
    // The IL helper emits class names in .NET ordinal (UTF-16) order.
    let mut candidates: Vec<_> = decisions
        .iter()
        .filter(|(_, d)| d.get("kind") == Some(&"reusable".into()))
        .map(|(n, _)| n)
        .collect();
    candidates.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
    let audits: BTreeSet<_> = candidates
        .into_iter()
        .filter(|_| matches!(mode, "enforce" | "refresh") && rng.sample() < rate)
        .collect();
    let mut reasons = BTreeMap::<String, u64>::new();
    let mut why = Map::new();
    for (name, decision) in &decisions {
        let kind = decision.field("kind")?.string()?;
        *reasons.entry(kind.into()).or_default() += 1;
        let reuse = kind == "reusable";
        reusable += usize::from(reuse);
        let audit = audits.contains(name);
        if mode != "plan" && (mode == "sweep" || !reuse || audit) {
            if to_run.len() < 50 && !reuse {
                why.insert(name.clone(), decision.clone());
            }
            to_run.push(name.clone());
        }
    }
    let mut code = 0;
    let mut elapsed = Value::Number("0.0".into());
    let mut recorded = 0;
    let mut failed = BTreeSet::new();
    if !to_run.is_empty() && matches!(mode, "refresh" | "sweep") {
        let mut classes = Map::new();
        for (name, prior) in old {
            if to_run.binary_search(&name).is_err()
                && program.units.get(&name).and_then(|u| u.get("key")) == prior.get("key")
                && program.units.contains_key(&name)
            {
                classes.insert(name, prior);
            }
        }
        let save_classes = |classes: &Map| {
            save(
                &record_path,
                &object([
                    ("classes", Value::Object(classes.clone())),
                    ("commit", commit.as_str().into()),
                    ("globals", program.globals.clone()),
                    ("types", Value::Object(program.types.clone())),
                    ("version", 2u64.into()),
                ]),
            )
        };
        let mut order = to_run.clone();
        let prior = index(record.get("classes"))?;
        order.sort_by(|a, b| {
            let time = |name: &str| duration(prior.get(name).and_then(|v| v.get("seconds")));
            time(b).total_cmp(&time(a))
        });
        elapsed = runner.record(&order, jobs, &mut log, |class| {
            if class.status == 0 {
                let key = program.units[&class.name].field("key")?.clone();
                classes.insert(
                    class.name,
                    object([
                        ("key", key),
                        ("files", Value::Object(class.files)),
                        ("commit", commit.as_str().into()),
                        ("seconds", class.elapsed),
                    ]),
                );
                recorded += 1;
            } else {
                failed.insert(class.name);
                if code == 0 {
                    code = class.status;
                }
            }
            if recorded % 10 == 0 {
                save_classes(&classes)?;
            }
            Ok(())
        })?;
        save_classes(&classes)?;
    } else if !to_run.is_empty() {
        (code, elapsed) = runner.run(
            &to_run,
            to_run.len() == program.units.len(),
            log.as_mut(),
            None,
        )?;
    }
    let reasons = Value::Object(reasons.into_iter().map(|(k, n)| (k, n.into())).collect());
    let brief = object([
        ("mode", mode.into()),
        ("units", count(program.units.len())),
        ("ran", count(to_run.len())),
        ("reused", count(program.units.len() - to_run.len())),
        ("audited", count(audits.len())),
        ("exit_code", Value::Number(code.to_string())),
        ("seconds_ran", elapsed),
        ("reasons", reasons),
    ]);
    if let Some(path) = a.optional("--receipt") {
        let mut row = brief.object()?.clone();
        row.extend(
            object([
                ("job", job.into()),
                ("commit", Value::String(commit)),
                ("started", Value::String(started)),
                ("predicted_reusable", count(reusable)),
                ("run", names(to_run.iter().take(200).cloned())),
                ("why", Value::Object(why)),
                (
                    "dynamic",
                    names(
                        program
                            .units
                            .iter()
                            .filter(|(_, v)| v.get("dynamic").is_some_and(Value::truthy))
                            .map(|(n, _)| n.clone()),
                    ),
                ),
                ("recorded", count(recorded)),
                ("failed", names(failed)),
            ])
            .object()?
            .clone(),
        );
        save(Path::new(path), &Value::Object(row))?;
    }
    if let Some(path) = a.optional("--list") {
        let mut text = String::new();
        for n in to_run {
            text.push_str(&n);
            text.push_str(if cfg!(windows) { "\r\n" } else { "\n" });
        }
        fs::write(path, text).map_err(io)?;
    }
    println!("{}", crate::json::canonical(&brief));
    Ok(code)
}
pub fn command(argv: &[String]) -> i32 {
    super::dispatch(argv, HELP, run)
}
