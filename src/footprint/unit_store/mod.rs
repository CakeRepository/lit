//! Keep one passing footprint per unit across local and signed proof stores.
mod audit;
mod plan;
use super::{
    cache,
    compare::{self, strings},
    digests,
    glob::Pattern,
    names,
    record::{Recording, UnitSource, RUN_ENV},
    symbols, trace_cli, unit_manifest,
    workspace::{self, save, Workspace},
    Result,
};
use crate::{
    args::{self, Args},
    cache_report::Seconds,
    files,
    git::Repo,
    json::{object, Value},
    json_paths as paths,
    store::job_dir,
};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::Instant,
};
type Map = BTreeMap<String, Value>;
const HELP: &str = include_str!("../../help/cached-units.txt");

fn number(v: &Value) -> Result<f64> {
    if let Value::Number(n) = v {
        n.parse().map_err(|_| "Invalid footprint duration".into())
    } else {
        Err("Invalid footprint duration".into())
    }
}
struct Settings {
    root: PathBuf,
    store: PathBuf,
    job: String,
    template: Vec<String>,
    batch: Option<Vec<String>>,
    cwd: String,
    mode: String,
    allow_names: Option<Vec<String>>,
    discover: Vec<Pattern>,
    strip: String,
    ignore: Vec<String>,
    scratch: Vec<String>,
    symbols: Vec<Pattern>,
    proofs: Vec<PathBuf>,
    audit_book: Option<PathBuf>,
    log: Option<PathBuf>,
    host: bool,
    network: bool,
    names_mode: bool,
    audit_rate: f64,
    shard: usize,
    shards: usize,
    pinned: BTreeSet<String>,
}
fn marked(s: &str) -> String {
    paths::path_key(s.as_bytes())
}
impl Settings {
    fn parse(a: &Args) -> Result<Self> {
        let err = |s| args::error("cached-units", HELP, s);
        let job = a.required("--job").map_err(err)?.into();
        let store = PathBuf::from(a.required("--store").map_err(err)?);
        let template = trace_cli::template(&marked(a.required("--each").map_err(err)?))?;
        let allow_names = a
            .optional("--units-file")
            .map(trace_cli::lines)
            .transpose()?;
        if allow_names.is_none() && !a.has("--discover") {
            return Err("Give --units-file or --discover".into());
        }
        if !template.iter().any(|s| s.contains("{}")) {
            return Err("The unit command needs {} where the unit name goes".into());
        }
        let (i, n) = a
            .optional("--shard")
            .unwrap_or("1/1")
            .split_once('/')
            .and_then(|(i, n)| Some((i.parse::<i64>().ok()?, n.parse::<i64>().ok()?)))
            .ok_or_else(|| err("argument --shard: use I/N, for example 2/4".into()))?;
        if i < 1 || i > n {
            return Err(err("argument --shard: I must be between 1 and N".into()));
        }
        let mut ignore = Vec::new();
        if !a.has("--strict-env") {
            ignore.extend(RUN_ENV.iter().map(|s| (*s).into()));
        }
        ignore.extend(a.all("--ignore-env").iter().map(|s| marked(s)));
        let mode = a.optional("--mode").unwrap_or("shadow");
        if !matches!(mode, "shadow" | "enforce" | "plan") {
            return Err(err(format!("argument --mode: invalid choice: '{mode}' (choose from 'shadow', 'enforce', 'plan')")));
        }
        let listings = a.optional("--listings").unwrap_or("names");
        if !matches!(listings, "names" | "exact") {
            return Err(err(format!(
                "argument --listings: invalid choice: '{listings}' (choose from 'exact', 'names')"
            )));
        }
        let patterns = |key| {
            a.all(key)
                .iter()
                .map(|s| Pattern::new(&marked(s)))
                .collect()
        };
        let rate = a.optional("--audit-rate").unwrap_or("0.1");
        Ok(Self {
            root: Repo::new(Path::new(a.optional("--workdir").unwrap_or(".")))?
                .path()
                .to_owned(),
            store,
            job,
            template,
            batch: a
                .optional("--batch")
                .filter(|s| !s.is_empty())
                .map(|s| trace_cli::template(&marked(s)))
                .transpose()?,
            cwd: marked(a.optional("--cwd").unwrap_or("")),
            mode: mode.into(),
            allow_names,
            discover: patterns("--discover"),
            strip: marked(a.optional("--strip").unwrap_or("")),
            ignore,
            scratch: a
                .all("--scratch")
                .iter()
                .map(|s| marked(s.trim_matches('/')))
                .collect(),
            symbols: patterns("--symbols"),
            proofs: a.all("--proof-store").iter().map(PathBuf::from).collect(),
            audit_book: a.optional("--audit-book").map(PathBuf::from),
            log: a.optional("--log").map(PathBuf::from),
            host: !a.has("--no-host"),
            network: a.has("--allow-network"),
            names_mode: listings == "names",
            audit_rate: rate.parse().map_err(|_| {
                err(format!(
                    "argument --audit-rate: invalid float value: '{rate}'"
                ))
            })?,
            shard: (i - 1) as usize,
            shards: n as usize,
            pinned: a
                .optional("--pin-file")
                .map(trace_cli::lines)
                .transpose()?
                .unwrap_or_default()
                .into_iter()
                .collect(),
        })
    }
    fn units(&self, w: &Workspace) -> Vec<String> {
        if let Some(ns) = &self.allow_names {
            return ns.clone();
        }
        let mut ns = w
            .entries
            .iter()
            .filter(|(p, e)| {
                e.kind == "blob"
                    && e.mode != "untracked"
                    && self.discover.iter().any(|g| g.matches(p))
            })
            .filter_map(|(p, _)| p.strip_prefix(&self.strip).map(str::to_owned))
            .collect::<Vec<_>>();
        ns.sort_by(|a, b| paths::compare(a, b));
        ns
    }
}
struct Batch {
    count: usize,
    seconds: Value,
    code: i32,
}
fn batch(config: &Settings, template: &[String], units: &[String]) -> Result<Batch> {
    let mut argv = Vec::new();
    let units = super::sorted(units);
    for part in template {
        if part.contains("{}") {
            for n in &units {
                argv.push(workspace::path(&part.replace("{}", n))?);
            }
        } else {
            argv.push(workspace::path(part)?);
        }
    }
    let Some(exe) = argv.first() else {
        return Err("Empty batch command".into());
    };
    let mut cmd = Command::new(exe);
    cmd.args(&argv[1..])
        .current_dir(config.root.join(workspace::path(&config.cwd)?));
    if let Some(p) = &config.log {
        let out = fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(p)
            .map_err(|e| files::error(p, e))?;
        cmd.stdout(out.try_clone().map_err(|e| e.to_string())?)
            .stderr(out);
    } else {
        cmd.stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null());
    }
    let clock = Instant::now();
    let st = cmd.status().map_err(|e| files::error(exe, e))?;
    #[cfg(unix)]
    let code = {
        use std::os::unix::process::ExitStatusExt;
        st.code().unwrap_or_else(|| -st.signal().unwrap_or(1))
    };
    #[cfg(not(unix))]
    let code = st.code().unwrap_or(2);
    Ok(Batch {
        count: units.len(),
        code,
        seconds: Value::Number(format!("{:.3}", clock.elapsed().as_secs_f64())),
    })
}
fn record(config: &Settings, dir: &Path, repo: &Repo, commit: &str, run: &[String]) -> Result<Map> {
    if run.is_empty() {
        return Ok(Map::new());
    }
    let tmp = crate::run::private_dir("lit-coverage-")
        .tempdir()
        .map_err(|e| e.to_string())?;
    let mut reports = BTreeMap::new();
    let mut each = Vec::new();
    let mut seen = BTreeMap::new();
    for (i, name) in run.iter().enumerate() {
        let mut argv = Vec::new();
        for part in &config.template {
            if part == "{lcov}" {
                if !config.symbols.is_empty() {
                    let dir = tmp.path().join(i.to_string());
                    reports.insert(name.clone(), dir.join("lcov.info"));
                    argv.extend([
                        "--coverage".into(),
                        "--coverage-reporter=lcov".into(),
                        format!(
                            "--coverage-dir={}",
                            paths::path_key(dir.as_os_str().as_encoded_bytes())
                        ),
                    ]);
                }
            } else {
                argv.push(part.replace("{}", name));
            }
        }
        let pair = (name.clone(), argv);
        if let Some(&index) = seen.get(name) {
            each[index] = pair;
        } else {
            seen.insert(name.clone(), each.len());
            each.push(pair);
        }
    }
    let mut bundle = Recording::new(
        repo,
        commit,
        Some(&config.root),
        &config.ignore,
        config.log.as_deref(),
        true,
    )?
    .units(UnitSource::Each(&each), &config.cwd, &config.scratch)?;
    if !config.symbols.is_empty() {
        for (oid, table) in symbols::records(
            &config.root,
            &config.cwd,
            &mut bundle,
            &reports,
            &config.symbols,
        )? {
            let file = plan::symbol_path(dir, &oid)?;
            if !file.exists() {
                save(&file, &table)?;
            }
        }
    }
    for (name, u) in bundle.field("units")?.object()? {
        if u.field("exit_code")? == &0u64.into() && !u.field("incomplete")?.truthy() {
            save(&plan::unit_path(dir, name)?, &plan::slice(&bundle, name)?)?;
        }
    }
    let Value::Object(mut data) = bundle else {
        unreachable!()
    };
    let Some(Value::Object(units)) = data.remove("units") else {
        unreachable!()
    };
    Ok(units)
}
fn sort_names(mut list: Vec<String>) -> Vec<String> {
    list.sort_by(|a, b| paths::compare(a, b));
    list
}
fn execute(config: &Settings) -> Result<Value> {
    let repo = Repo::new(&config.root)?;
    let dir = job_dir(&config.store, &config.job)?;
    let started = cache::iso(jiff::Timestamp::now());
    let db = config.store.join("digests.json");
    digests::load(&db);
    let mut work = Workspace::new(&repo, "HEAD", Some(&config.root))?;
    let units = config.units(&work);
    let mut decisions = plan::from_store(&dir, &units, &mut work, config)?;
    for other in &config.proofs {
        let waiting = units
            .iter()
            .filter(|n| !decisions[*n].reuse())
            .cloned()
            .collect::<Vec<_>>();
        if waiting.is_empty() {
            break;
        }
        for (name, mut d) in
            plan::from_store(&job_dir(other, &config.job)?, &waiting, &mut work, config)?
        {
            if d.reuse() {
                d.proof = Some(other.clone());
                decisions.insert(name, d);
            }
        }
    }
    let mut audits = audit::Audits::new(config, &units, &decisions, &work.commit)?;
    let to_run = match config.mode.as_str() {
        "shadow" => units.clone(),
        "plan" => Vec::new(),
        _ => units
            .iter()
            .filter(|n| !decisions[*n].reuse() || audits.chosen.contains(*n))
            .cloned()
            .collect(),
    };
    let mut to_run = plan::shard(to_run, &decisions, config)?;
    let here = to_run.iter().cloned().collect::<BTreeSet<_>>();
    let batched = config
        .batch
        .as_ref()
        .filter(|v| config.mode == "enforce" && !to_run.is_empty() && !v.is_empty())
        .map(|t| batch(config, t, &to_run))
        .transpose()?;
    let batch_ok = batched.as_ref().is_some_and(|b| b.code == 0);
    if batch_ok {
        to_run.clear();
    }
    let ran = record(config, &dir, &repo, &work.commit, &to_run)?;
    let mut failed = Vec::new();
    let mut unsound = Vec::new();
    let mut reads = Map::new();
    let mut ran_seconds = Seconds::default();
    for (name, u) in &ran {
        ran_seconds.add(u.get("seconds").filter(|s| s.truthy()))?;
        if u.field("exit_code")? != &0u64.into() {
            failed.push(name.clone());
            if decisions[name].reuse() {
                unsound.push(name.clone());
            }
            reads.insert(
                name.clone(),
                object([
                    ("tracked", u.field("tracked")?.clone()),
                    (
                        "listings",
                        names(super::sorted(u.field("listings")?.object()?.keys())),
                    ),
                    ("absent", u.field("absent")?.clone()),
                    ("incomplete", u.field("incomplete")?.clone()),
                ]),
            );
        }
    }
    if let Some(b) = &batched {
        ran_seconds.add(Some(&b.seconds))?;
    }
    let checked = audits.finish(config, &here, &ran, batch_ok)?;
    let predicted = units
        .iter()
        .filter(|n| decisions[*n].reuse())
        .collect::<Vec<_>>();
    let mut predicted_seconds = Seconds::default();
    let mut measured = false;
    for n in &predicted {
        if let Some(s) = ran
            .get(*n)
            .and_then(|u| u.get("seconds"))
            .filter(|s| s.truthy())
        {
            predicted_seconds.add(Some(s))?;
            measured = true;
        }
    }
    if !measured {
        for n in &predicted {
            predicted_seconds.add(Some(&decisions[*n].seconds()))?;
        }
    }
    let mut first = BTreeMap::<String, u64>::new();
    let mut functions = 0u64;
    let mut proofs = 0u64;
    for d in decisions.values() {
        let k = if d.reuse() {
            "reusable"
        } else {
            d.reasons[0].field("kind")?.string()?
        };
        *first.entry(k.into()).or_default() += 1;
        functions += u64::from(d.reuse() && !d.functions.is_empty());
        proofs += u64::from(d.reuse() && d.proof.is_some());
    }
    let failed = sort_names(failed);
    let starved = decisions.len() >= 10
        && predicted.is_empty()
        && decisions.values().filter(|d| d.prior.is_some()).count() * 2 >= decisions.len();
    let reused = if config.mode == "enforce" {
        predicted.len() - audits.chosen.len()
    } else {
        0
    };
    let row = object([
        ("job", marked(&config.job).as_str().into()),
        ("mode", config.mode.as_str().into()),
        (
            "shard",
            Value::String(format!("{}/{}", config.shard + 1, config.shards)),
        ),
        ("commit", work.commit.as_str().into()),
        ("started", Value::String(started)),
        ("units", (units.len() as u64).into()),
        ("predicted_reusable", (predicted.len() as u64).into()),
        (
            "ran",
            ((ran.len()
                + batched
                    .as_ref()
                    .filter(|_| to_run.is_empty())
                    .map_or(0, |b| b.count)) as u64)
                .into(),
        ),
        (
            "batch",
            batched
                .as_ref()
                .map(|b| {
                    object([
                        ("files", (b.count as u64).into()),
                        ("seconds", b.seconds.clone()),
                        ("exit_code", Value::Number(b.code.to_string())),
                        ("traced_after", Value::Bool(!to_run.is_empty())),
                    ])
                })
                .unwrap_or(Value::Null),
        ),
        ("reused", (reused as u64).into()),
        ("audited", (audits.chosen.len() as u64).into()),
        ("failed", names(failed.iter().take(200))),
        ("unsound", names(&sort_names(unsound))),
        ("seconds_ran", ran_seconds.rounded()),
        ("seconds_predicted_reusable", predicted_seconds.rounded()),
        (
            "first_reasons",
            Value::Object(first.into_iter().map(|(k, v)| (k, v.into())).collect()),
        ),
        ("reusable_by_function", functions.into()),
        ("reusable_by_proof", proofs.into()),
        ("signer_audits", checked),
        ("starved", Value::Bool(starved)),
        ("exit_code", u64::from(!failed.is_empty()).into()),
    ]);
    cache::append(&dir.join("ledger.jsonl"), &row)?;
    digests::flush(&db)?;
    let Value::Object(mut result) = row else {
        unreachable!()
    };
    result.insert("reusable".into(), names(predicted));
    result.insert("failed_reads".into(), Value::Object(reads));
    result.insert(
        "decisions".into(),
        Value::Object(
            decisions
                .into_iter()
                .filter(|(_, d)| !d.reuse())
                .map(|(n, d)| (n, d.json()))
                .collect(),
        ),
    );
    Ok(Value::Object(result))
}
fn run(argv: &[String]) -> Result<i32> {
    let err = |e| args::error("cached-units", HELP, e);
    let a = Args::with_options(
        argv,
        &[
            "--store",
            "--job",
            "--workdir",
            "--cwd",
            "--each",
            "--batch",
            "--units-file",
            "--discover",
            "--strip",
            "--proof-store",
            "--audit-book",
            "--mode",
            "--audit-rate",
            "--listings",
            "--ignore-env",
            "--log",
            "--receipt",
            "--list",
            "--shard",
            "--pin-file",
            "--symbols",
            "--scratch",
        ],
        &["--allow-network", "--no-host", "--strict-env"],
        &[],
    )
    .map_err(err)?;
    if !a.positional.is_empty() {
        return Err(err(format!(
            "unrecognized arguments: {}",
            a.positional.join(" ")
        )));
    }
    let settings = Settings::parse(&a)?;
    let result = execute(&settings)?;
    if let Some(p) = a.optional("--receipt") {
        save(Path::new(p), &result)?;
    }
    let exits = result.field("exit_code")?.number()? as i32;
    if let Some(p) = a.optional("--list") {
        let mut text = String::new();
        for n in super::sorted(result.field("decisions")?.object()?.keys()) {
            text.push_str(&paths::utf8(n)?);
            text.push_str(if cfg!(windows) { "\r\n" } else { "\n" });
        }
        fs::write(p, text).map_err(|e| files::error(Path::new(p), e))?;
    }
    let Value::Object(mut brief) = result else {
        unreachable!()
    };
    for k in ["decisions", "reusable"] {
        brief.remove(k);
    }
    let failed = brief["failed"].array()?.len();
    brief.insert("failed".into(), (failed as u64).into());
    println!("{}", paths::sorted(&Value::Object(brief), true));
    Ok(exits)
}
pub fn command(argv: &[String]) -> i32 {
    super::dispatch(argv, HELP, run)
}
