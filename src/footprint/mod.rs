//! Predict reuse from recorded filesystem observations of a job or its units.
pub(crate) mod cache;
mod compare;
mod digests;
pub(crate) mod dotnet;
mod events;
mod glob;
mod record;
mod summary;
mod symbols;
pub(crate) mod trace_cli;
mod tracer;
pub(crate) mod unit_store;
mod workspace;

use crate::{
    args::{self, Args},
    cache_report::Seconds,
    files,
    git::Repo,
    json::{object, Value},
    json_paths as paths,
};
use compare::{compare, environment_changes, reason, strings, Options};
use glob::Pattern;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};
use workspace::{save, Workspace};
type Result<T> = std::result::Result<T, String>;
const REUSE: &str = include_str!("../help/reuse.txt");
const SELECT: &str = include_str!("../help/select.txt");

fn options(args: &Args) -> Options {
    Options {
        env: args.has("--environment"),
        host: args.has("--host"),
        allow_network: args.has("--allow-network"),
        names: args.optional("--listings") == Some("names"),
    }
}
fn load(args: &Args, name: &str, help: &str, units: bool) -> Result<Value> {
    let label = if units { "bundle" } else { "footprint" };
    let [file] = args.positional[..] else {
        let msg = if args.positional.is_empty() {
            format!("the following arguments are required: {label}")
        } else {
            format!("unrecognized arguments: {}", args.positional[1..].join(" "))
        };
        return Err(args::error(name, help, msg));
    };
    let v = paths::read(Path::new(file))?;
    let kind = if units {
        "lit-unit-footprints"
    } else {
        "lit-job-footprint"
    };
    if v.get("kind").and_then(|v| v.string().ok()) != Some(kind)
        || v.get("version").and_then(|v| v.number().ok()) != Some(1)
    {
        return Err(if units {
            "Not a lit unit footprint bundle"
        } else {
            "Not a lit job footprint"
        }
        .into());
    }
    Ok(v)
}
fn result_output(args: &Args, v: &Value) -> Result<()> {
    if let Some(p) = args.optional("--out") {
        save(Path::new(p), v)?;
    }
    Ok(())
}
fn predict(manifest: &Value, work: &mut Workspace, opt: &Options) -> Result<Value> {
    let reasons = compare(manifest, work, opt)?;
    let mut checked = vec!["checkout".into()];
    if opt.env {
        checked.push("environment".into());
    }
    if opt.host {
        checked.push("host".into());
    }
    Ok(object([
        ("candidate", work.commit.as_str().into()),
        ("tree", work.entries[""].oid.clone()),
        ("footprint_commit", manifest.field("commit")?.clone()),
        (
            "verdict",
            if reasons.is_empty() {
                "reusable"
            } else {
                "run"
            }
            .into(),
        ),
        (
            "expected_exit_code",
            if reasons.is_empty() {
                manifest.field("exit_code")?.clone()
            } else {
                Value::Null
            },
        ),
        ("checked", Value::Array(checked)),
        ("reason_count", (reasons.len() as u64).into()),
        (
            "reasons",
            Value::Array(reasons.into_iter().take(200).collect()),
        ),
    ]))
}
fn reuse_command(argv: &[String]) -> Result<i32> {
    let err = |e| args::error("reuse", REUSE, e);
    let args = Args::with_options(
        argv,
        &["--repo", "--out"],
        &["--allow-network", "--environment", "--host", "--verify"],
        &["--candidate"],
    )
    .map_err(err)?;
    let repo = Repo::new(Path::new(args.required("--repo").map_err(err)?))?;
    args.required("--candidate").map_err(err)?;
    let manifest = load(&args, "reuse", REUSE, false)?;
    let opt = options(&args);
    let mut ok = true;
    let mut rows = Vec::new();
    for rev in args.all("--candidate") {
        let mut work = Workspace::new(&repo, rev, None)?;
        let prediction = predict(&manifest, &mut work, &opt)?;
        let reusable = prediction.field("verdict")?.string()? == "reusable";
        if args.has("--verify") {
            let actual = tracer::verify(&repo, &work.commit, manifest.field("command")?)?;
            let sound = !reusable || prediction.field("expected_exit_code")? == &actual;
            let Value::Object(mut row) = prediction else {
                unreachable!()
            };
            row.insert("actual_exit_code".into(), actual);
            row.insert("sound".into(), Value::Bool(sound));
            ok &= sound;
            rows.push(Value::Object(row));
        } else {
            ok &= reusable;
            rows.push(prediction);
        }
    }
    let result = Value::Array(rows);
    result_output(&args, &result)?;
    println!("{}", paths::sorted(&result, true));
    Ok(i32::from(!ok))
}
fn unit_manifest(bundle: &Value, unit: &Value) -> Result<Value> {
    let mut view = unit.object()?.clone();
    view.insert("commit".into(), bundle.field("commit")?.clone());
    for key in ["tracked", "external"] {
        let all = bundle.field(key)?.object()?;
        let sub = strings(unit.field(key)?)?
            .into_iter()
            .map(|p| {
                Ok((
                    p.into(),
                    all.get(p)
                        .ok_or_else(|| format!("Missing {key} path {p}"))?
                        .clone(),
                ))
            })
            .collect::<Result<_>>()?;
        view.insert(key.into(), Value::Object(sub));
    }
    Ok(Value::Object(view))
}
fn sorted<'a>(s: impl IntoIterator<Item = &'a String>) -> Vec<&'a String> {
    let mut out = s.into_iter().collect::<Vec<_>>();
    out.sort_unstable_by(|a, b| paths::compare(a, b));
    out
}
fn names<'a>(s: impl IntoIterator<Item = &'a String>) -> Value {
    Value::Array(s.into_iter().map(|n| Value::String(n.clone())).collect())
}
fn selection(
    bundle: &Value,
    work: &mut Workspace,
    opt: &Options,
    discover: &[Pattern],
    strip: &str,
) -> Result<Value> {
    let units = bundle.field("units")?.object()?;
    let mut names_to_check = units.keys().cloned().collect::<BTreeSet<_>>();
    let mut new = Vec::new();
    if !discover.is_empty() {
        let mut found = BTreeSet::new();
        for (p, e) in &work.entries {
            if e.kind == "blob" && e.mode != "untracked" && discover.iter().any(|g| g.matches(p)) {
                if let Some(n) = p.strip_prefix(strip) {
                    found.insert(n.to_owned());
                }
            }
        }
        new.extend(found.difference(&names_to_check).cloned());
        names_to_check.retain(|n| found.contains(n));
    }
    let env = if opt.env {
        environment_changes(bundle)?
    } else {
        Vec::new()
    };
    let unit_options = Options { env: false, ..*opt };
    let mut run = BTreeMap::new();
    let mut reusable = Vec::new();
    let (mut run_seconds, mut reused_seconds) = (Seconds::default(), Seconds::default());
    for n in sorted(&names_to_check) {
        let unit = &units[n];
        let exit_code = unit.field("exit_code")?;
        let mut reasons = if exit_code != &0u64.into() {
            let code = if exit_code == &Value::Null {
                "None".into()
            } else {
                paths::sorted(exit_code, false)
            };
            vec![reason(
                "failed",
                "detail",
                Value::String(format!("exited {code}")),
            )]
        } else {
            let view = unit_manifest(bundle, unit)?;
            let mut why = compare::compare_unit(&view, bundle, work, &unit_options)?;
            why.extend_from_slice(&env);
            why
        };
        let seconds = unit.get("seconds").filter(|v| v.truthy());
        if reasons.is_empty() {
            reusable.push(n);
            reused_seconds.add(seconds)?;
        } else {
            reasons.truncate(20);
            run.insert(n.clone(), Value::Array(reasons));
            run_seconds.add(seconds)?;
        }
    }
    for n in new {
        run.insert(
            n,
            Value::Array(vec![reason(
                "new",
                "detail",
                "no footprint for this unit".into(),
            )]),
        );
    }
    Ok(object([
        ("candidate", work.commit.as_str().into()),
        ("footprint_commit", bundle.field("commit")?.clone()),
        ("run", names(sorted(run.keys()))),
        ("reusable", names(reusable)),
        ("reasons", Value::Object(run)),
        ("seconds_recorded_run", run_seconds.rounded()),
        ("seconds_recorded_reusable", reused_seconds.rounded()),
    ]))
}
fn select_command(argv: &[String]) -> Result<i32> {
    let err = |e| args::error("select", SELECT, e);
    let args = Args::with_options(
        argv,
        &[
            "--repo",
            "--candidate",
            "--workdir",
            "--discover",
            "--strip",
            "--listings",
            "--out",
            "--list",
        ],
        &["--host", "--environment", "--allow-network"],
        &[],
    )
    .map_err(err)?;
    if let Some(v) = args
        .optional("--listings")
        .filter(|s| !matches!(*s, "exact" | "names"))
    {
        return Err(err(format!(
            "argument --listings: invalid choice: '{v}' (choose from 'exact', 'names')"
        )));
    }
    let bundle = load(&args, "select", SELECT, true)?;
    let workdir = args.optional("--workdir").map(Path::new);
    let repo = Repo::new(
        args.optional("--repo")
            .map(Path::new)
            .or(workdir)
            .unwrap_or(Path::new(".")),
    )?;
    let mut work = Workspace::new(
        &repo,
        args.optional("--candidate").unwrap_or("HEAD"),
        workdir,
    )?;
    let patterns = args
        .all("--discover")
        .iter()
        .map(|p| Pattern::new(p))
        .collect::<Vec<_>>();
    let result = selection(
        &bundle,
        &mut work,
        &options(&args),
        &patterns,
        args.optional("--strip").unwrap_or(""),
    )?;
    result_output(&args, &result)?;
    if let Some(p) = args.optional("--list") {
        let mut text = String::new();
        for name in strings(result.field("run")?)? {
            text.push_str(&paths::utf8(name)?);
            text.push_str(if cfg!(windows) { "\r\n" } else { "\n" });
        }
        fs::write(p, text).map_err(|e| files::error(Path::new(p), e))?;
    }
    let mut brief = result.object()?.clone();
    for v in brief.values_mut() {
        match v {
            Value::Object(map) => *v = (map.len() as u64).into(),
            Value::Array(items) => *v = (items.len() as u64).into(),
            _ => {}
        }
    }
    println!("{}", paths::sorted(&Value::Object(brief), true));
    Ok(0)
}
fn dispatch(argv: &[String], help: &str, command: fn(&[String]) -> Result<i32>) -> i32 {
    if argv
        .iter()
        .take_while(|a| a.as_str() != "--")
        .any(|s| matches!(s.as_str(), "-h" | "--help"))
    {
        print!("{help}");
        return 0;
    }
    command(argv).unwrap_or_else(|e| {
        eprintln!("{e}");
        2
    })
}
pub fn reuse(argv: &[String]) -> i32 {
    dispatch(argv, REUSE, reuse_command)
}
pub fn select(argv: &[String]) -> i32 {
    dispatch(argv, SELECT, select_command)
}
