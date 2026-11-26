//! Coordinate reviewed source revisions through the protected GitHub merge queue.
mod audit;
mod coordinator;
mod direct;
mod guard;
mod ledger;
mod split;
use crate::{
    args::{self, Args},
    collect,
    git::Repo,
    github::Client,
    json::{self, object, Value},
};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::PathBuf,
    process::Command,
};
type Map = BTreeMap<String, Value>;
type Refs = BTreeMap<String, String>;
type Member = (u64, String);
type Edit = (String, String, String);
type Result<T> = std::result::Result<T, Error>;
const HELP: &str = include_str!("../help/intake.txt");
#[derive(Debug)]
enum Error {
    Hold(String),
    Resolution(u64),
    Operation(String),
    Usage(String),
}
impl From<String> for Error {
    fn from(s: String) -> Self {
        Self::Operation(s)
    }
}
impl From<&str> for Error {
    fn from(s: &str) -> Self {
        s.to_owned().into()
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Hold(s) | Self::Operation(s) | Self::Usage(s) => f.write_str(s),
            Self::Resolution(_) => {
                f.write_str("Source merge needs an author resolution or contains extra changes")
            }
        }
    }
}
fn hold(s: impl Into<String>) -> Error {
    Error::Hold(s.into())
}
fn string<'a>(v: &'a Value, k: &str) -> Option<&'a str> {
    v.get(k)?.string().ok()
}
fn with(v: &Value, k: &str, val: Value) -> Result<Value> {
    let mut m = v.object()?.clone();
    m.insert(k.into(), val);
    Ok(Value::Object(m))
}
fn number(v: &Value) -> Result<f64> {
    if let Value::Number(s) = v {
        s.parse().map_err(|_| "Invalid counter".into())
    } else {
        Err("Invalid counter".into())
    }
}
fn now() -> Result<String> {
    Ok(crate::footprint::cache::iso(crate::footprint::cache::now()?))
}
fn date(s: &str) -> Result<jiff::Timestamp> {
    s.parse()
        .map_err(|_| format!("Invalid timestamp: {s}").into())
}
fn seconds(start: &str, end: &str) -> Result<i64> {
    Ok(((date(end)?.as_nanosecond() - date(start)?.as_nanosecond()) / 1_000_000_000) as i64)
}
fn members(v: &Value) -> Result<Vec<Member>> {
    v.array()?
        .iter()
        .map(|r| {
            let [n, h] = r.array()? else {
                return Err("Invalid intake member".into());
            };
            let n = n.number()?;
            let h = h.string()?;
            if n == 0 || n >= 1 << 31 || !guard::oid(h) {
                return Err(hold("Invalid pinned source"));
            }
            Ok((n, h.into()))
        })
        .collect()
}
fn rows(m: &[Member]) -> Value {
    Value::Array(
        m.iter()
            .map(|(n, h)| Value::Array(vec![(*n).into(), h.as_str().into()]))
            .collect(),
    )
}
fn span(v: &Value, k: &str) -> Result<Vec<Value>> {
    Ok(v.get(k)
        .map(Value::array)
        .transpose()?
        .unwrap_or_default()
        .to_vec())
}

#[derive(Clone, Copy, PartialEq)]
enum Limit {
    All,
    Fixed(usize),
    Auto,
}
struct Intake<'a> {
    src: guard::Sources<'a>,
    repo: Repo,
    url: String,
    target: String,
    active: String,
    originals: bool,
    limit: Limit,
    direct: Option<String>,
    verified: Option<(String, String, Vec<Member>)>,
    preview: Option<(String, String, bool)>,
    last_wait: Option<(u64, String, i64)>,
}
fn execute(argv: &[String], default_cache: &mut Option<PathBuf>) -> Result<()> {
    let err = |s: String| Error::Usage(args::error("intake", HELP, s));
    let a = Args::with_options(
        argv,
        &[
            "--context",
            "--direct-context",
            "--branch",
            "--max-batch",
            "--repo-dir",
            "--label",
            "--since",
        ],
        &["--original-prs-only", "--no-wait"],
        &[],
    )
    .map_err(err)?;
    let repository = *a
        .positional
        .first()
        .ok_or_else(|| err("the following arguments are required: repository".into()))?;
    let context = a.required("--context").map_err(err)?;
    collect::repository_parts(repository).map_err(|_| hold("Invalid GitHub repository"))?;
    let branch = a.optional("--branch").unwrap_or("main");
    let action = *a
        .positional
        .get(1)
        .ok_or_else(|| err("the following arguments are required: action".into()))?;
    let rest = &a.positional[2..];
    let limit = match a.optional("--max-batch") {
        None => Limit::All,
        Some("auto") => Limit::Auto,
        Some(s) => Limit::Fixed(
            s.parse::<usize>()
                .ok()
                .filter(|&n| n > 0 && !s.starts_with('0') && s.bytes().all(|b| b.is_ascii_digit()))
                .ok_or_else(|| {
                    err("argument --max-batch: expected auto or a positive whole number".into())
                })?,
        ),
    };
    if !matches!(action, "ready" | "drain" | "tick" | "withdraw" | "report") {
        return Err(err(format!("invalid intake action: {action}")));
    }
    if (a.has("--label") && action != "tick")
        || (a.has("--since") && action != "report")
        || (a.has("--no-wait") && action != "drain")
    {
        return Err(err("unrecognized action option".into()));
    }
    let correct = match action {
        "ready" => !rest.is_empty(),
        "withdraw" => rest.len() == 2,
        "report" => rest.len() == 1,
        _ => rest.is_empty(),
    };
    if !correct {
        return Err(err(format!("wrong number of arguments for {action}")));
    }
    let number = |s: &str| {
        s.parse::<u64>()
            .ok()
            .filter(|&n| n > 0 && n < 1 << 31)
            .ok_or_else(|| hold("Invalid source PR number"))
    };
    let target = format!("refs/heads/{branch}");
    let active = format!("refs/heads/lit/active/{branch}");
    // Check before using the branch in the default cache path.
    let checked = Command::new("git")
        .args(["check-ref-format", &active])
        .status()
        .map_err(|e| e.to_string())?;
    if !checked.success() {
        return Err(hold("Invalid intake branch"));
    }
    let dir = match a.optional("--repo-dir") {
        Some(s) => PathBuf::from(s),
        None => std::env::home_dir()
            .ok_or("Home directory not available")?
            .join(".cache/lit")
            .join(repository)
            .join(format!("{branch}.git")),
    };
    if !a.has("--repo-dir") {
        *default_cache = Some(dir.clone());
    }
    if !dir.exists() {
        fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        let s = Command::new("git")
            .args(["init", "--bare", "--quiet"])
            .arg(&dir)
            .status()
            .map_err(|e| e.to_string())?;
        if !s.success() {
            return Err("Could not initialize bare repository".into());
        }
    }
    let repo = Repo::new(&dir)?;
    if repo.text(&["rev-parse", "--is-bare-repository"], None)? != "true" {
        return Err(Error::Usage(
            "--repo-dir must be a dedicated bare repository".into(),
        ));
    }
    let client = Client::new();
    let mut intake = Intake {
        src: guard::Sources {
            repository,
            branch,
            context,
            client: &client,
        },
        repo,
        url: format!("https://github.com/{repository}.git"),
        target,
        active,
        originals: a.has("--original-prs-only"),
        limit,
        direct: a.optional("--direct-context").map(str::to_owned),
        verified: None,
        preview: None,
        last_wait: None,
    };
    match action {
        "withdraw" => {
            let n = number(rest[0])?;
            if !guard::oid(rest[1]) {
                return Err(Error::Usage(
                    "withdraw requires the exact 40-hex source head".into(),
                ));
            }
            intake.push(vec![(guard::pin(n), rest[1].into(), String::new())])
        }
        "report" => intake.report(number(rest[0])?, a.optional("--since")),
        "tick" => intake.tick(a.optional("--label").unwrap_or("lit-ready")),
        "ready" => {
            intake.register(&rest.iter().map(|s| number(s)).collect::<Result<Vec<_>>>()?)?;
            intake.drain(true)
        }
        _ => intake.drain(!a.has("--no-wait")),
    }
}
pub fn command(argv: &[String]) -> i32 {
    if argv
        .iter()
        .take_while(|a| a.as_str() != "--")
        .any(|a| matches!(a.as_str(), "-h" | "--help"))
    {
        print!("{HELP}");
        return 0;
    }
    let session = crate::handback::begin();
    let mut default_cache = None;
    let result = execute(argv, &mut default_cache);
    if let Some(cache) = default_cache {
        if let Err(e) = session.finish(&[cache]) {
            eprintln!("lit: could not give the intake cache back to its folder's owner: {e}");
        }
    }
    match result {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("{e}");
            if matches!(e, Error::Usage(_)) {
                2
            } else {
                1
            }
        }
    }
}

fn repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::String(s) => {
            let quote = if s.contains('\'') && !s.contains('"') {
                '"'
            } else {
                '\''
            };
            let mut out = String::from(quote);
            for c in s.chars() {
                match c {
                    '\\' => out.push_str("\\\\"),
                    '\n' => out.push_str("\\n"),
                    '\r' => out.push_str("\\r"),
                    '\t' => out.push_str("\\t"),
                    c if c == quote => {
                        out.push('\\');
                        out.push(c)
                    }
                    c => out.push(c),
                }
            }
            out.push(quote);
            out
        }
        Value::Array(v) => format!("[{}]", v.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => {
            let mut keys = m.keys().collect::<Vec<_>>();
            keys.sort_by_key(|k| {
                ["number", "author", "requires", "conflicts"]
                    .iter()
                    .position(|s| s == *k)
                    .unwrap_or(5)
            });
            format!(
                "{{{}}}",
                keys.iter()
                    .map(|k| format!("{}: {}", repr(&Value::String((**k).clone())), repr(&m[*k])))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        v => json::canonical(v),
    }
}
