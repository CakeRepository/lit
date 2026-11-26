use super::super::{
    compare::{environment, under, MACHINE_LOCAL},
    record::RUN_ENV,
    workspace,
};
use super::*;
use lit::frontier::Certificate;
const ENV: &str = "/proc/self/environ?ignore=";

pub(super) fn value(v: Option<&Value>) -> Option<String> {
    match v {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(v) => Some(format!("\0json:{}", paths::sorted(v, false))),
    }
}
fn ignore(entries: &Value) -> Result<Vec<Pattern>> {
    let mut names = super::super::compare::strings(entries)?;
    if names.contains(&"SHLVL") {
        names.extend_from_slice(RUN_ENV);
    }
    Ok(names.into_iter().map(Pattern::new).collect())
}
fn hash_env(env: &Map, ignore: &[Pattern]) -> String {
    let kept = env
        .iter()
        .filter(|(k, _)| !ignore.iter().any(|p| p.matches(k)))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    sha256::hex(paths::sorted(&Value::Object(kept), false).as_bytes())
}
pub(super) fn runner(key: &str) -> Result<Option<String>> {
    if let Some(raw) = key.strip_prefix(ENV) {
        let ignored = ignore(&paths::parse(raw.as_bytes())?)?;
        Ok(Some(hash_env(&environment(&ignored), &ignored)))
    } else if key.starts_with('/') {
        Ok(Some(super::super::digests::file(&workspace::path(key)?)))
    } else {
        Ok(None)
    }
}

fn records(dir: &Path, proof: bool) -> Result<Vec<(String, Certificate)>> {
    let entries = match fs::read_dir(dir.join("units")) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(e.to_string()),
    };
    let mut files = entries
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|s| s == "json"))
        .collect::<Vec<_>>();
    files.sort();
    let mut certs = Vec::new();
    let mut tables = BTreeMap::new();
    for p in files {
        let Some(bundle) = paths::read(&p)
            .ok()
            .filter(|b| b.get("kind") == Some(&"lit-unit-footprints".into()))
        else {
            continue;
        };
        for (name, unit) in bundle.field("units")?.object()? {
            if unit.get("exit_code").is_some_and(Value::truthy)
                || unit.get("incomplete").is_some_and(Value::truthy)
            {
                continue;
            }
            let mut inputs = Inputs::new();
            for file in super::super::compare::strings(unit.field("tracked")?)? {
                let oid = bundle
                    .field("tracked")?
                    .field(file)?
                    .array()?
                    .get(1)
                    .ok_or("Missing tracked blob")?
                    .string()?;
                let ran = unit.get("symbols").and_then(|v| v.get(file));
                let table = if ran.is_some() && crate::snapshot::valid_oid(oid) {
                    tables
                        .entry(oid.to_owned())
                        .or_insert_with(|| {
                            paths::read(&dir.join("symbols").join(format!("{oid}.json"))).ok()
                        })
                        .as_ref()
                } else {
                    None
                };
                if let (Some(ran), Some(table)) = (ran, table) {
                    for symbol in std::iter::once("").chain(super::super::compare::strings(ran)?) {
                        inputs.insert(format!("{file}#{symbol}"), value(table.get(symbol)));
                    }
                } else {
                    inputs.insert(file.into(), Some(oid.into()));
                }
            }
            if let Some(env) = bundle.get("environment") {
                let mut patterns = bundle
                    .get("ignored_environment")
                    .map(super::super::compare::strings)
                    .transpose()?
                    .unwrap_or_default();
                patterns.sort_by(|a, b| paths::compare(a, b));
                let entries = Value::Array(patterns.into_iter().map(Value::from).collect());
                inputs.insert(
                    format!("{ENV}{}", paths::sorted(&entries, false)),
                    Some(hash_env(env.object()?, &ignore(&entries)?)),
                );
            }
            if let Some(external) = unit.get("external") {
                for p in super::super::compare::strings(external)? {
                    if p.starts_with('/') && !MACHINE_LOCAL.iter().any(|d| under(p, d)) {
                        inputs.insert(
                            p.into(),
                            value(bundle.get("external").and_then(|v| v.get(p))),
                        );
                    }
                }
            }
            let seconds = match unit.get("seconds") {
                Some(Value::Number(s)) => s.parse().map_err(|_| "Invalid unit duration")?,
                _ => 0.,
            };
            certs.push((
                name.clone(),
                Certificate {
                    inputs,
                    seconds,
                    proof,
                },
            ));
        }
    }
    Ok(certs)
}
pub(super) fn load(store: &Path, job: &str, proofs: Option<&Path>) -> Result<Frontier> {
    let primary = records(&crate::store::job_dir(store, job)?, false)?;
    let base = primary.iter().flat_map(|(_, c)| c.inputs.clone()).collect();
    let mut f = Frontier::new(base);
    for (name, c) in primary {
        f.record(name, c);
    }
    if let Some(proofs) = proofs {
        for (name, c) in records(&crate::store::job_dir(proofs, job)?, true)? {
            f.record(name, c);
        }
    }
    Ok(f)
}
