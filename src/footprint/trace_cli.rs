use super::{
    record::{Recording, UnitSource, RUN_ENV},
    workspace::save,
    Result,
};
use crate::{
    args::{self, Args},
    cache_report::Seconds,
    files,
    git::Repo,
    json::{object, Value},
    json_paths as paths,
};
use std::{collections::BTreeMap, path::Path};
const TRACE: &str = include_str!("../help/trace.txt");
const UNITS: &str = include_str!("../help/trace-units.txt");

pub fn template(text: &str) -> Result<Vec<String>> {
    let mut parts = Vec::new();
    let mut part = String::new();
    let mut started = false;
    let mut quote = None;
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match (quote, c) {
            (None, ' ' | '\t' | '\n' | '\r') => {
                if started {
                    parts.push(std::mem::take(&mut part));
                    started = false;
                }
            }
            (None, '\'' | '"') => {
                quote = Some(c);
                started = true;
            }
            (Some(q), c) if q == c => quote = None,
            (None, '\\') => {
                part.push(chars.next().ok_or("No escaped character")?);
                started = true;
            }
            (Some('"'), '\\') if chars.peek().is_some_and(|c| matches!(c, '\\' | '"')) => {
                part.push(chars.next().unwrap())
            }
            _ => {
                started = true;
                part.push(c);
            }
        }
    }
    if quote.is_some() {
        return Err("No closing quotation".into());
    }
    if started {
        parts.push(part);
    }
    Ok(parts)
}
fn words(args: &[String]) -> Vec<String> {
    args.iter().map(|s| paths::path_key(s.as_bytes())).collect()
}
pub fn lines(path: &str) -> Result<Vec<String>> {
    let raw = files::read(Path::new(path))?;
    Ok(std::str::from_utf8(&raw)
        .map_err(|e| e.to_string())?
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(|s| paths::path_key(s.as_bytes()))
        .collect())
}
pub fn commands(names: Vec<String>, argv: &[String]) -> Vec<(String, Vec<String>)> {
    let mut seen = std::collections::BTreeSet::new();
    names
        .into_iter()
        .filter(|n| seen.insert(n.clone()))
        .map(|n| {
            let args = argv.iter().map(|s| s.replace("{}", &n)).collect();
            (n, args)
        })
        .collect()
}
fn count(v: &Value) -> Result<Value> {
    Ok(match v {
        Value::Object(o) => o.len() as u64,
        Value::Array(a) => a.len() as u64,
        _ => return Err("Expected a collection".into()),
    }
    .into())
}
fn brief(row: &Value, units: bool) -> Result<Value> {
    let mut out = BTreeMap::new();
    for k in ["commit", "exit_code", "seconds"] {
        out.insert(k.into(), row.field(k)?.clone());
    }
    if units {
        let units = row.field("units")?.object()?;
        let (mut failed, mut incomplete, mut network) = (0u64, 0u64, 0u64);
        let mut seconds = Seconds::default();
        for u in units.values() {
            failed += u64::from(u.field("exit_code")? != &0u64.into());
            incomplete += u64::from(u.field("incomplete")?.truthy());
            network += u64::from(u.field("network")?.truthy());
            seconds.add(u.get("seconds").filter(|v| v.truthy()))?;
        }
        out.extend(
            object([
                ("units", (units.len() as u64).into()),
                ("failed", failed.into()),
                ("incomplete", incomplete.into()),
                ("network", network.into()),
                ("unit_seconds", seconds.rounded()),
            ])
            .object()?
            .clone(),
        );
    } else {
        for k in ["tracked", "listings", "absent", "external"] {
            out.insert(k.into(), count(row.field(k)?)?);
        }
        for k in ["reads_git_metadata", "incomplete"] {
            out.insert(k.into(), row.field(k)?.clone());
        }
        out.insert(
            "network".into(),
            Value::Bool(row.field("network")?.truthy()),
        );
    }
    Ok(Value::Object(out))
}
fn run(argv: &[String], units: bool) -> Result<i32> {
    let (name, help) = if units {
        ("trace-units", UNITS)
    } else {
        ("trace", TRACE)
    };
    let err = |m| args::error(name, help, m);
    let end = argv
        .iter()
        .position(|a| a == "--test")
        .unwrap_or(argv.len());
    if argv[..end]
        .iter()
        .any(|a| matches!(a.as_str(), "-h" | "--help"))
    {
        print!("{help}");
        return Ok(0);
    }
    let options = &[
        "--repo",
        "--commit",
        "--out",
        "--log",
        "--ignore-env",
        "--workdir",
        "--pattern",
        "--each",
        "--units-file",
        "--cwd",
    ];
    let args = Args::with_options(
        &argv[..end],
        if units { options } else { &options[..5] },
        if units { &["--strict-env"] } else { &[] },
        &[],
    )
    .map_err(err)?;
    if !args.positional.is_empty() {
        return Err(err(format!(
            "unrecognized arguments: {}",
            args.positional.join(" ")
        )));
    }
    let out = Path::new(args.required("--out").map_err(err)?);
    let test = words(argv.get(end + 1..).unwrap_or_default());
    let dir = args.optional("--workdir").map(Path::new);
    let mut ignore = Vec::new();
    if units && !args.has("--strict-env") {
        ignore.extend(RUN_ENV.iter().map(|s| (*s).into()));
    }
    ignore.extend(
        args.all("--ignore-env")
            .iter()
            .map(|s| paths::path_key(s.as_bytes())),
    );
    let repo = Repo::new(
        args.optional("--repo")
            .map(Path::new)
            .or(dir)
            .unwrap_or(Path::new(".")),
    )?;
    let rev = args.optional("--commit").unwrap_or("HEAD");
    let rec = || {
        Recording::new(
            &repo,
            rev,
            dir,
            &ignore,
            args.optional("--log").map(Path::new),
            units,
        )
    };
    let result = if units {
        let each;
        let source = if let Some(tpl) = args.optional("--each").filter(|s| !s.is_empty()) {
            let file = args
                .optional("--units-file")
                .filter(|_| test.is_empty() && args.optional("--pattern").is_none())
                .ok_or("--each takes --units-file and no --pattern or --test")?;
            let argv = template(&paths::path_key(tpl.as_bytes()))?;
            if !argv.iter().any(|s| s.contains("{}")) {
                return Err("--each needs {} where the unit name goes".into());
            }
            each = commands(lines(file)?, &argv);
            UnitSource::Each(&each)
        } else {
            let pattern = args
                .optional("--pattern")
                .filter(|s| !s.is_empty() && !test.is_empty())
                .ok_or("Give --pattern and the command after --test, or --each")?;
            UnitSource::Command(&test, pattern)
        };
        rec()?.units(source, args.optional("--cwd").unwrap_or(""), &[])?
    } else {
        args.required("--repo").map_err(err)?;
        args.required("--commit").map_err(err)?;
        if end == argv.len() {
            return Err(err("the following arguments are required: --test".into()));
        }
        if test.is_empty() {
            return Err("Supply the job command after --test".into());
        }
        rec()?.job(&test)?
    };
    save(out, &result)?;
    println!("{}", paths::sorted(&brief(&result, units)?, true));
    Ok(0)
}
fn command(args: &[String], units: bool) -> i32 {
    run(args, units).unwrap_or_else(|e| {
        eprintln!("{e}");
        2
    })
}
pub fn trace(args: &[String]) -> i32 {
    command(args, false)
}
pub fn units(args: &[String]) -> i32 {
    command(args, true)
}
