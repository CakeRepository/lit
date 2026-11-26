//! Keep job footprints on a trusted runner and audit predictions against real test exits.
use super::{
    compare::{compare, Options},
    digests,
    record::{Recording, RUN_ENV},
    summary::array,
    workspace::{self, save, Workspace},
    Result,
};
use crate::{
    args::{self, Args},
    files,
    git::Repo,
    json::{object, Value},
    json_paths as paths, parity,
    store::job_dir,
};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
};
const HELP: &str = include_str!("../help/cached-run.txt");
pub fn append(path: &Path, row: &Value) -> Result<()> {
    fs::create_dir_all(path.parent().unwrap_or(Path::new(".")))
        .map_err(|e| files::error(path, e))?;
    let mut line = paths::sorted(row, false);
    line.push('\n');
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .and_then(|mut f| f.write_all(line.as_bytes()))
        .map_err(|e| files::error(path, e))
}
fn records(directory: &Path) -> Result<Vec<PathBuf>> {
    let iter = match fs::read_dir(directory) {
        Ok(i) => i,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(files::error(directory, e)),
    };
    let mut out = Vec::new();
    for entry in iter {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.extension().is_some_and(|e| e == "json") {
            out.push(path);
        }
    }
    out.sort();
    Ok(out)
}
fn audited(rate: f64) -> Result<bool> {
    if !(0.0..1.0).contains(&rate) {
        return Ok(rate >= 1.0);
    }
    if rate == 0.0 {
        return Ok(false);
    }
    let draw = if parity::epoch().is_some() {
        0.5
    } else {
        let mut bytes = [0; 8];
        getrandom::fill(&mut bytes).map_err(|e| e.to_string())?;
        ((u64::from_ne_bytes(bytes) >> 11) as f64) / (1u64 << 53) as f64
    };
    Ok(draw < rate)
}
pub fn now() -> Result<jiff::Timestamp> {
    parity::epoch()
        .map(|s| jiff::Timestamp::from_second(s).map_err(|e| e.to_string()))
        .unwrap_or_else(|| Ok(jiff::Timestamp::now()))
}
pub fn iso(t: jiff::Timestamp) -> String {
    let text = format!("{t:.6}").replace('Z', "+00:00");
    text.replace(".000000+", "+")
}
struct Job<'a> {
    store: &'a Path,
    job: &'a str,
    dir: &'a Path,
    test: Vec<String>,
    ignore: Vec<String>,
    mode: &'a str,
    allow_network: bool,
    audit_rate: f64,
    log: Option<&'a Path>,
}
impl Job<'_> {
    fn cached(&self) -> Result<Value> {
        let repo = Repo::new(self.dir)?;
        let commit = repo.text(&["rev-parse", "HEAD"], None)?;
        let directory = job_dir(self.store, self.job)?;
        let clock = now()?;
        let digest_file = self.store.join("digests.json");
        digests::load(&digest_file);
        let result = self.run(&repo, &commit, &directory, clock);
        digests::flush(&digest_file)?;
        result
    }
    fn run(
        &self,
        repo: &Repo,
        commit: &str,
        directory: &Path,
        clock: jiff::Timestamp,
    ) -> Result<Value> {
        let folder = directory.join("footprints");
        let cmd = array(self.test.iter().map(String::as_str));
        let mut prior = None;
        let mut checked = 0u64;
        let options = Options {
            env: true,
            host: true,
            allow_network: self.allow_network,
            names: false,
        };
        let mut work = None;
        for path in records(&folder)?.into_iter().rev() {
            let Ok(v) = paths::read(&path) else { continue };
            if v.get("kind") != Some(&"lit-job-footprint".into())
                || v.get("version") != Some(&1u64.into())
                || v.get("command") != Some(&cmd)
                || v.get("exit_code") != Some(&0u64.into())
            {
                continue;
            }
            checked += 1;
            if work.is_none() {
                work = Some(Workspace::new(repo, commit, Some(repo.path()))?);
            }
            if compare(&v, work.as_mut().unwrap(), &options)?.is_empty() {
                prior = Some((path.file_name().unwrap().to_string_lossy().into_owned(), v));
                break;
            }
            if checked >= 50 {
                break;
            }
        }
        let row = object([
            ("job", self.job.into()),
            ("mode", self.mode.into()),
            ("commit", commit.into()),
            ("started", Value::String(iso(clock))),
            ("compared", checked.into()),
            (
                "prior",
                prior
                    .as_ref()
                    .map(|(p, _)| Value::String(p.clone()))
                    .unwrap_or(Value::Null),
            ),
            (
                "prior_commit",
                prior
                    .as_ref()
                    .map(|(_, v)| v.field("commit").cloned())
                    .transpose()?
                    .unwrap_or(Value::Null),
            ),
        ]);
        let mut row = row.object()?.clone();
        let audit = prior.is_some() && self.mode == "enforce" && audited(self.audit_rate)?;
        let ledger = directory.join("ledger.jsonl");
        if let Some((_, old)) = &prior {
            if self.mode == "enforce" && !audit {
                row.insert("decision".into(), "reused".into());
                row.insert("exit_code".into(), 0u64.into());
                let mut receipt = row.clone();
                receipt.insert("seconds_saved".into(), old.field("seconds")?.clone());
                append(&ledger, &Value::Object(receipt))?;
                return Ok(Value::Object(row));
            }
        }
        let manifest = Recording::new(
            repo,
            commit,
            Some(repo.path()),
            &self.ignore,
            self.log,
            false,
        )?
        .job(&self.test)?;
        let code = manifest.field("exit_code")?;
        if code == &0u64.into() && !manifest.field("incomplete")?.truthy() {
            let stamp = format!("{clock:.6}").replace(['-', ':', '.'], "");
            save(
                &folder.join(format!("{stamp}-{}.json", &commit[..12])),
                &manifest,
            )?;
            let all = records(&folder)?;
            let excess = all.len().saturating_sub(200);
            for p in all.into_iter().take(excess) {
                if let Err(e) = fs::remove_file(&p) {
                    if e.kind() != std::io::ErrorKind::NotFound {
                        return Err(files::error(&p, e));
                    }
                }
            }
        }
        row.extend(
            [
                ("decision", if audit { "audited" } else { "ran" }.into()),
                ("predicted_reuse", Value::Bool(prior.is_some())),
                (
                    "sound",
                    Value::Bool(prior.is_none() || code == &0u64.into()),
                ),
                ("network", Value::Bool(manifest.field("network")?.truthy())),
            ]
            .map(|(k, v)| (k.into(), v)),
        );
        for field in ["exit_code", "seconds", "incomplete", "reads_git_metadata"] {
            row.insert(field.into(), manifest.field(field)?.clone());
        }
        let result = Value::Object(row);
        append(&ledger, &result)?;
        Ok(result)
    }
}
fn plain(work: &Path, test: &[String]) -> Result<i32> {
    let mut args = test
        .iter()
        .map(|s| workspace::path(s))
        .collect::<Result<Vec<_>>>()?
        .into_iter();
    let exe = args.next().ok_or("Supply the job command after --test")?;
    let status = Command::new(&exe)
        .args(args)
        .current_dir(work)
        .status()
        .map_err(|e| files::error(&exe, e))?;
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        Ok(status
            .code()
            .unwrap_or_else(|| -status.signal().unwrap_or(1))
            .rem_euclid(256))
    }
    #[cfg(not(unix))]
    {
        Ok(status.code().unwrap_or(2))
    }
}
fn run(argv: &[String]) -> Result<i32> {
    let end = argv
        .iter()
        .position(|s| s == "--test")
        .unwrap_or(argv.len());
    if argv[..end]
        .iter()
        .any(|s| matches!(s.as_str(), "-h" | "--help"))
    {
        print!("{HELP}");
        return Ok(0);
    }
    let err = |m| args::error("cached-run", HELP, m);
    let a = Args::with_options(
        &argv[..end],
        &[
            "--store",
            "--job",
            "--workdir",
            "--mode",
            "--audit-rate",
            "--ignore-env",
            "--log",
            "--receipt",
        ],
        &["--strict-env", "--allow-network"],
        &[],
    )
    .map_err(err)?;
    if !a.positional.is_empty() {
        return Err(err(format!(
            "unrecognized arguments: {}",
            a.positional.join(" ")
        )));
    }
    let mode = a.optional("--mode").unwrap_or("shadow");
    if !matches!(mode, "shadow" | "enforce") {
        return Err(err(format!(
            "argument --mode: invalid choice: '{mode}' (choose from 'shadow', 'enforce')"
        )));
    }
    let rate = a.optional("--audit-rate").unwrap_or("0.1");
    let audit_rate = rate.parse::<f64>().map_err(|_| {
        err(format!(
            "argument --audit-rate: invalid float value: '{rate}'"
        ))
    })?;
    let mut ignore = Vec::new();
    if !a.has("--strict-env") {
        ignore.extend(RUN_ENV.iter().map(|s| (*s).into()));
    }
    ignore.extend(
        a.all("--ignore-env")
            .iter()
            .map(|s| paths::path_key(s.as_bytes())),
    );
    let test = argv
        .get(end + 1..)
        .unwrap_or_default()
        .iter()
        .map(|s| paths::path_key(s.as_bytes()))
        .collect::<Vec<_>>();
    let job = Job {
        job: a.required("--job").map_err(err)?,
        store: Path::new(a.required("--store").map_err(err)?),
        dir: Path::new(a.optional("--workdir").unwrap_or(".")),
        test,
        ignore,
        mode,
        audit_rate,
        allow_network: a.has("--allow-network"),
        log: a.optional("--log").map(Path::new),
    };
    if end == argv.len() {
        return Err(err("the following arguments are required: --test".into()));
    }
    if job.test.is_empty() {
        return Err("Supply the job command after --test".into());
    }
    let result = match job.cached() {
        Ok(v) => v,
        Err(e) => {
            eprintln!("lit: {e}; running the job without a footprint");
            return plain(job.dir, &job.test);
        }
    };
    if let Some(path) = a.optional("--receipt") {
        save(Path::new(path), &result)?;
    }
    match result.field("decision")?.string()? {
        "reused" => println!(
            "lit: reused {} from {}",
            result.field("prior")?.string()?,
            result.field("prior_commit")?.string()?
        ),
        _ if result.get("predicted_reuse").is_some_and(Value::truthy) => println!(
            "lit: {} predicted this result and the run {}",
            result.field("prior")?.string()?,
            if result.field("sound")?.truthy() {
                "matched"
            } else {
                "DID NOT MATCH"
            }
        ),
        _ => {}
    }
    // A child terminated by a signal uses the shell's modulo-256 convention.
    let Value::Number(code) = result.field("exit_code")? else {
        return Err("Invalid test exit code".into());
    };
    Ok(code
        .parse::<i32>()
        .map_err(|e| e.to_string())?
        .rem_euclid(256))
}
pub fn command(args: &[String]) -> i32 {
    run(args).unwrap_or_else(|e| {
        eprintln!("{e}");
        2
    })
}

/// Render the receipt for the cached-run composite action after the test exits.
pub fn action_receipt(path: &str) -> i32 {
    fn append_line(variable: &str, text: &str) -> Result<()> {
        let path = std::env::var_os(variable).ok_or_else(|| format!("missing {variable}"))?;
        let mut file = fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .map_err(|e| files::error(Path::new(&path), e))?;
        writeln!(file, "{text}").map_err(|e| e.to_string())
    }
    fn render(path: &Path) -> Result<()> {
        let receipt = paths::parse(&files::read(path)?)?;
        let decision = receipt.field("decision")?.string()?;
        let prior = receipt
            .get("prior_commit")
            .filter(|v| v.truthy())
            .map(Value::string)
            .transpose()?
            .unwrap_or("");
        if !matches!(decision, "ran" | "audited" | "reused")
            || !prior.bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err("invalid cache receipt decision or prior commit".into());
        }
        let mode = receipt.field("mode")?.string()?;
        let job = receipt.field("job")?.string()?;
        let mut line = format!("lit {mode}: {decision} {job}");
        if !prior.is_empty() {
            line.push_str(&format!(
                ", predicted by the passing run on {}",
                &prior[..12.min(prior.len())]
            ));
            if decision != "reused" {
                line.push_str(if receipt.get("sound").is_some_and(Value::truthy) {
                    " (matched)"
                } else {
                    " (DID NOT MATCH)"
                });
            }
        }
        append_line(
            "GITHUB_OUTPUT",
            &format!("decision={decision}\nprior-commit={prior}"),
        )?;
        append_line("GITHUB_STEP_SUMMARY", &line)
    }
    match render(Path::new(path)) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("lit: {e}");
            2
        }
    }
}
