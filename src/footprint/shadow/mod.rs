//! Compare a batch's real receipt with the groups and tests Frontier would require.
mod contents;
mod store;
use super::{glob::Pattern, workspace::save, Result};
use crate::{
    args::{self, Args},
    git::Repo,
    json::{object, Value},
    json_paths as paths, sha256,
};
use lit::frontier::{Admission, Frontier, Inputs, Intent, Members, Units};
use std::{collections::BTreeMap, fs, path::Path};
type Map = BTreeMap<String, Value>;
const HELP: &str = include_str!("../../help/shadow.txt");
fn names(items: impl IntoIterator<Item = String>) -> Value {
    let mut keys = items.into_iter().collect::<Vec<_>>();
    keys.sort_by(|a, b| paths::compare(a, b));
    Value::Array(keys.into_iter().map(Value::String).collect())
}
fn ids(items: impl IntoIterator<Item = u64>) -> Value {
    Value::Array(items.into_iter().map(Value::from).collect())
}

pub(crate) struct Options<'a> {
    pub store: &'a Path,
    pub job: &'a str,
    pub proofs: Option<&'a Path>,
    pub discover: Vec<&'a str>,
    pub strip: &'a str,
}
pub(crate) fn report(
    repo: &Repo,
    snapshot: &Value,
    opt: &Options,
    receipt: Option<&Value>,
    attempts: Option<&Value>,
) -> Result<Value> {
    let base = snapshot.field("base")?.string()?;
    let mut contents = contents::Contents::new(repo);
    let mut frontier = store::load(opt.store, opt.job, opt.proofs)?;
    let mut main = Inputs::new();
    let mut files = BTreeMap::<String, Vec<String>>::new();
    for key in frontier.keys() {
        let p = key.split_once('#').map_or(key.as_str(), |(p, _)| p);
        files.entry(p.into()).or_default().push(key.clone());
        let v = if let Some(d) = store::runner(key)? {
            Some(d)
        } else {
            contents.digest(base, key)?
        };
        if v.is_some() {
            main.insert(key.clone(), v);
        }
    }
    frontier.base = main;
    let patterns: Vec<_> = opt.discover.iter().map(|p| Pattern::new(p)).collect();
    let on_main = contents.discovered(base, &patterns, opt.strip)?;
    let known: Units = frontier.units().cloned().collect();
    let unrecorded: Units = on_main.difference(&known).cloned().collect();
    let mut changed = Members::new();
    let mut added = BTreeMap::new();
    let mut writes = Map::new();
    let mut conflicts = Map::new();
    for pr in snapshot.field("changes")?.array()? {
        let id = pr.field("number")?.number()?;
        let head = pr.field("head")?.string()?;
        let w = contents.writes(base, head, &files)?;
        let new = contents.discovered(head, &patterns, opt.strip)?;
        added.insert(
            id,
            new.difference(&on_main)
                .filter(|n| !known.contains(*n))
                .cloned()
                .collect::<Units>(),
        );
        writes.insert(id.to_string(), names(w.keys().cloned()));
        changed.insert(id);
        match frontier.admit(
            id,
            w,
            Intent {
                title: Some(format!("PR #{id}")),
                ..Intent::default()
            },
        ) {
            Ok(()) => (),
            Err(Admission::Duplicate) => return Err("Change already admitted".into()),
            Err(Admission::Conflict(packet)) => {
                conflicts.insert(
                    id.to_string(),
                    object([
                        ("inputs", names(packet.incoming.diffs.into_keys())),
                        (
                            "held_by",
                            ids(packet.landed_first.into_iter().map(|s| s.change)),
                        ),
                    ]),
                );
            }
        }
    }
    let mut components = Vec::new();
    let mut unrun = Members::new();
    let mut component_runs = 0u64;
    for group in frontier.components() {
        let members = group.iter().copied().collect();
        let cone = frontier.affected(&members);
        let needs = frontier.needs(&members);
        let untested: Units = group
            .iter()
            .flat_map(|id| added[id].iter().cloned())
            .collect();
        let proofs = cone
            .iter()
            .filter(|u| frontier.proves(u, Some(&members)).is_some_and(|c| c.proof))
            .cloned();
        if !needs.is_empty() {
            component_runs += 1;
        } else if untested.is_empty() && unrecorded.is_empty() {
            unrun.extend(&group);
        }
        components.push(object([
            ("members", ids(group)),
            ("cone", (cone.len() as u64).into()),
            ("needs", names(needs)),
            ("overlaps", names(frontier.overlaps(&members))),
            ("unrecorded", names(untested)),
            ("by_proof", names(proofs)),
        ]));
    }
    let main = Members::new();
    let decision = object([
        ("units", (known.len() as u64).into()),
        (
            "stale",
            names(
                known
                    .iter()
                    .filter(|u| frontier.proves(u, Some(&main)).is_none())
                    .cloned(),
            ),
        ),
        ("components", Value::Array(components)),
        ("conflicts", Value::Object(conflicts)),
        ("unrecorded", names(unrecorded)),
        ("unrun", ids(unrun.iter().copied())),
    ]);
    let mut out = Map::from([
        ("kind".into(), "lit-shadow-decision".into()),
        ("base".into(), base.into()),
        ("job".into(), opt.job.into()),
        ("proofs".into(), Value::Bool(opt.proofs.is_some())),
        (
            "discover".into(),
            Value::Array(opt.discover.iter().map(|&s| s.into()).collect()),
        ),
        ("writes".into(), Value::Object(writes)),
        ("decision".into(), decision),
    ]);
    if let Some(receipt) = receipt {
        let passed = receipt
            .get("tested")
            .map(Value::array)
            .transpose()?
            .unwrap_or_default()
            .iter()
            .map(Value::number)
            .collect::<Result<Members>>()?;
        let runs = attempts
            .map(Value::array)
            .transpose()?
            .unwrap_or_default()
            .iter()
            .filter(|r| {
                matches!(
                    r.get("result").and_then(|v| v.string().ok()),
                    Some("pass" | "fail" | "known-failure")
                )
            })
            .count();
        out.insert(
            "outcome".into(),
            object([
                ("passed", ids(passed.iter().copied())),
                ("failed", ids(changed.difference(&passed).copied())),
                ("suite_runs", (runs as u64).into()),
                ("component_runs", component_runs.into()),
                ("unrun_but_failed", ids(unrun.difference(&passed).copied())),
            ]),
        );
    }
    Ok(Value::Object(out))
}
pub(crate) fn for_run(repo: &Repo, snapshot: &Path, opt: &Options, directory: &Path) -> Result<()> {
    let result = (|| {
        report(
            repo,
            &paths::read(snapshot)?,
            opt,
            Some(&paths::read(&directory.join("result.json"))?),
            Some(&paths::read(&directory.join("attempts.json"))?),
        )
    })();
    let row = result.unwrap_or_else(|e| {
        eprintln!("lit: shadow decision failed: {e}");
        object([
            ("kind", "lit-shadow-decision".into()),
            ("error", Value::String(e)),
        ])
    });
    save(&directory.join("shadow.json"), &row)
}
fn run(argv: &[String]) -> Result<i32> {
    let err = |e| args::error("shadow", HELP, e);
    let a = Args::with_options(
        argv,
        &[
            "--repo", "--store", "--job", "--result", "--proofs", "--strip", "--out",
        ],
        &[],
        &["--discover"],
    )
    .map_err(err)?;
    let repo = a.required("--repo").map_err(err)?;
    let opt = Options {
        store: Path::new(a.required("--store").map_err(err)?),
        job: a.required("--job").map_err(err)?,
        proofs: a.optional("--proofs").map(Path::new),
        discover: a.all("--discover").to_vec(),
        strip: a.optional("--strip").unwrap_or(""),
    };
    let (snapshot, rest) = a
        .positional
        .split_first()
        .ok_or_else(|| err("the following arguments are required: snapshot".into()))?;
    if !rest.is_empty() {
        return Err(err(format!("unrecognized arguments: {}", rest.join(" "))));
    }
    let snapshot = paths::read(Path::new(snapshot))?;
    let mut receipt = None;
    let mut attempts = None;
    if let Some(p) = a.optional("--result") {
        let p = Path::new(p);
        let row = paths::read(p)?;
        if row.truthy() && row.get("snapshot") != Some(&snapshot) {
            return Err("--result is from another snapshot".into());
        }
        let history = p.with_file_name("attempts.json");
        if history.exists() {
            attempts = Some(paths::read(&history)?);
        }
        receipt = Some(row);
    }
    let result = report(
        &Repo::new(Path::new(repo))?,
        &snapshot,
        &opt,
        receipt.as_ref(),
        attempts.as_ref(),
    )?;
    if let Some(out) = a.optional("--out") {
        save(Path::new(out), &result)?;
    }
    let mut brief = result.field("decision")?.object()?.clone();
    if let Some(outcome) = result
        .get("outcome")
        .filter(|_| receipt.as_ref().is_some_and(Value::truthy))
    {
        brief.insert("outcome".into(), outcome.clone());
    }
    println!("{}", paths::sorted(&Value::Object(brief), true));
    Ok(0)
}
pub fn command(argv: &[String]) -> i32 {
    super::dispatch(argv, HELP, run)
}
