//! Attribute failed units to changes and score predicted skips against Bun logs.
use crate::args::Args;
use crate::files;
use crate::git::Repo;
use crate::json::{self, object, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
type Result<T> = std::result::Result<T, String>;

// JSON footprints can name Git paths containing arbitrary bytes via Python's
// surrogateescape. Preserve escaped surrogates even in object keys while
// working with the regular JSON parser's Unicode strings.
const MARK: char = '\u{fdd0}';
fn mark_surrogate(out: &mut String, unit: u32) {
    out.push(MARK);
    out.push(char::from_u32(0xe000 + unit - 0xd800).unwrap());
}
fn mark_char(out: &mut String, c: char) {
    if c == MARK {
        out.push(MARK);
    }
    out.push(c);
}
fn path_key(path: &[u8]) -> String {
    let mut s = String::new();
    for chunk in path.utf8_chunks() {
        for c in chunk.valid().chars() {
            mark_char(&mut s, c);
        }
        for b in chunk.invalid() {
            mark_surrogate(&mut s, 0xdc00 + u32::from(*b));
        }
    }
    s
}
fn unicode_escape(text: &str) -> Option<u32> {
    let h = text.strip_prefix("\\u")?.get(..4)?;
    h.bytes()
        .all(|c| c.is_ascii_hexdigit())
        .then(|| u32::from_str_radix(h, 16).ok())
        .flatten()
}
fn read(path: &str) -> Result<Value> {
    let bytes = files::read(Path::new(path))?;
    let mut text = std::str::from_utf8(&bytes).map_err(|e| e.to_string())?;
    let mut escaped = String::new();
    while !text.is_empty() {
        if let Some(unit) = unicode_escape(text) {
            text = &text[6..];
            if (0xd800..0xdc00).contains(&unit) {
                if let Some(low @ 0xdc00..=0xdfff) = unicode_escape(text) {
                    text = &text[6..];
                    let c =
                        char::from_u32(0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00)).unwrap();
                    mark_char(&mut escaped, c);
                    continue;
                }
            }
            if (0xd800..0xe000).contains(&unit) {
                mark_surrogate(&mut escaped, unit);
            } else if unit == MARK as u32 {
                mark_char(&mut escaped, MARK);
            } else {
                use std::fmt::Write;
                write!(escaped, "\\u{unit:04x}").unwrap();
            }
        } else {
            let mut chars = text.chars();
            let c = chars.next().unwrap();
            mark_char(&mut escaped, c);
            if c == '\\' {
                if let Some(next) = chars.next() {
                    mark_char(&mut escaped, next);
                }
            }
            text = chars.as_str();
        }
    }
    json::parse(&escaped)
}
fn render(value: &Value) -> String {
    use std::fmt::Write;
    let encoded = json::pretty(value);
    let mut raw = String::new();
    let mut chars = encoded.chars();
    while let Some(c) = chars.next() {
        if c != MARK {
            raw.push(c);
            continue;
        }
        match chars.next() {
            Some(MARK) => raw.push(MARK),
            Some(c @ '\u{e000}'..='\u{e7ff}') => {
                write!(raw, "\\u{:04x}", 0xd800 + (c as u32 - 0xe000)).unwrap()
            }
            Some(other) => {
                raw.push(MARK);
                raw.push(other);
            }
            None => raw.push(MARK),
        }
    }
    raw
}
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
fn verdict(
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
        match read(file)?.get("failed_reads") {
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
fn score(args: &Args<'_>) -> Result<Value> {
    let plan = read(args.positional[0])?;
    let reusable = strings(plan.field("reusable")?)?;
    let mut failed = BTreeSet::new();
    for log in args.all("--log") {
        let bytes = files::read(Path::new(log))?;
        let text = String::from_utf8_lossy(&bytes);
        let mut current = None;
        for line in text.split([
            '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
            '\u{2029}',
        ]) {
            if let Some(path) = bun_header(line) {
                current = Some(path);
            } else if let Some(path) = current {
                if line.starts_with("(fail)")
                    || line.starts_with("# Unhandled error")
                    || line.starts_with("error: ")
                {
                    failed.insert(path_key(path.as_bytes()));
                }
            }
        }
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
