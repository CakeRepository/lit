//! Aggregate job ledgers written by shadow, enforce, and audited cache runs.
use crate::args::Args;
use crate::files;
use crate::json::{self, object, Value};
use crate::store::job_dir;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

type Result<T> = std::result::Result<T, String>;
const HELP: &str = include_str!("help/cache-report.txt");

#[derive(Default)]
pub(crate) struct Seconds {
    integer: i128,
    float: bool,
    sum: f64,
    correction: f64,
}
impl Seconds {
    pub(crate) fn add(&mut self, value: Option<&Value>) -> Result<()> {
        let Some(Value::Number(n)) = value else {
            return if value.is_some() {
                Err("Expected a number of seconds".into())
            } else {
                Ok(())
            };
        };
        if !self.float {
            if let Ok(integer) = n.parse::<i128>() {
                if let Some(total) = self.integer.checked_add(integer) {
                    self.integer = total;
                    return Ok(());
                }
            }
            self.float = true;
            self.sum = self.integer as f64;
        }
        // Python 3.12 uses compensated summation for floats before rounding.
        let x: f64 = n.parse().map_err(|_| "Expected a number of seconds")?;
        let total = self.sum + x;
        self.correction += if self.sum.abs() >= x.abs() {
            (self.sum - total) + x
        } else {
            (x - total) + self.sum
        };
        self.sum = total;
        Ok(())
    }
    pub(crate) fn rounded(&self) -> Value {
        Value::Number(if self.float {
            format!("{:.1}", self.sum + self.correction)
        } else {
            self.integer.to_string()
        })
    }
}
fn summary(folder: &Path) -> Result<Value> {
    let data = files::read(&folder.join("ledger.jsonl"))?;
    let text = std::str::from_utf8(&data).map_err(|e| e.to_string())?;
    let (mut runs, mut executed, mut reused, mut predicted, mut unsound, mut incomplete) =
        (0u64, 0u64, 0u64, 0u64, 0u64, 0u64);
    let (mut seconds, mut predicted_seconds, mut saved) =
        (Seconds::default(), Seconds::default(), Seconds::default());
    for line in text.lines().filter(|s| !s.trim().is_empty()) {
        let row = json::parse(line)?;
        runs += 1;
        saved.add(row.get("seconds_saved"))?;
        match row.field("decision")?.string()? {
            "ran" | "audited" => {
                executed += 1;
                seconds.add(row.get("seconds"))?;
                incomplete += u64::from(row.get("incomplete").is_some_and(Value::truthy));
                if row.get("predicted_reuse").is_some_and(Value::truthy) {
                    predicted += 1;
                    if row.field("sound")?.truthy() {
                        predicted_seconds.add(Some(row.field("seconds")?))?;
                    } else {
                        unsound += 1;
                    }
                }
            }
            "reused" => reused += 1,
            _ => (),
        }
    }
    Ok(object([
        ("runs", runs.into()),
        ("executed", executed.into()),
        ("reused", reused.into()),
        ("predicted_reusable", predicted.into()),
        ("unsound", unsound.into()),
        ("incomplete_runs", incomplete.into()),
        ("seconds_executed", seconds.rounded()),
        ("seconds_predicted_reusable", predicted_seconds.rounded()),
        ("seconds_saved", saved.rounded()),
    ]))
}
fn report(argv: &[String]) -> Result<()> {
    let args = Args::parse(argv, &["--store", "--job"])
        .map_err(|e| crate::args::error("cache-report", HELP, e))?;
    let store = Path::new(
        args.required("--store")
            .map_err(|e| crate::args::error("cache-report", HELP, e))?,
    );
    if !args.positional.is_empty() {
        return Err(crate::args::error(
            "cache-report",
            HELP,
            format!("unrecognized arguments: {}", args.positional.join(" ")),
        ));
    }
    let mut jobs = Vec::new();
    if let Some(job) = args.optional("--job").filter(|s| !s.is_empty()) {
        jobs.push(job_dir(store, job)?);
    } else {
        for entry in fs::read_dir(store).map_err(|e| files::error(store, e))? {
            let p = entry.map_err(|e| e.to_string())?.path();
            if p.join("ledger.jsonl").exists() {
                jobs.push(p);
            }
        }
        jobs.sort();
    }
    let mut out = BTreeMap::new();
    for dir in jobs {
        out.insert(
            dir.file_name()
                .and_then(|n| n.to_str())
                .ok_or("Non-Unicode job name")?
                .to_owned(),
            summary(&dir)?,
        );
    }
    println!("{}", json::pretty(&Value::Object(out)));
    Ok(())
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
    match report(argv) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}
