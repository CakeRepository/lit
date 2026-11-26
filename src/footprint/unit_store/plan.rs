use super::*;
use crate::sha256;
pub(super) fn unit_path(directory: &Path, name: &str) -> Result<PathBuf> {
    Ok(directory.join("units").join(format!(
        "{}.json",
        &sha256::hex(paths::utf8(name)?.as_bytes())[..32]
    )))
}
pub(super) fn symbol_path(dir: &Path, oid: &str) -> Result<PathBuf> {
    if !crate::snapshot::valid_oid(oid) {
        return Err("Invalid symbol blob identity".into());
    }
    Ok(dir.join("symbols").join(format!("{oid}.json")))
}
pub(super) fn slice(bundle: &Value, name: &str) -> Result<Value> {
    let mut out = bundle
        .object()?
        .iter()
        .filter(|(k, _)| {
            !matches!(
                k.as_str(),
                "units" | "tracked" | "external" | "listing_children" | "each"
            )
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect::<Map>();
    let unit = bundle.field("units")?.field(name)?;
    out.insert(
        "units".into(),
        Value::Object(BTreeMap::from([(name.into(), unit.clone())])),
    );
    for k in ["tracked", "external"] {
        let all = bundle.field(k)?;
        let subset = strings(unit.field(k)?)?
            .into_iter()
            .map(|p| Ok((p.into(), all.field(p)?.clone())))
            .collect::<Result<Map>>()?;
        out.insert(k.into(), Value::Object(subset));
    }
    let children = unit
        .field("listings")?
        .object()?
        .keys()
        .filter_map(|p| {
            bundle
                .get("listing_children")?
                .get(p)
                .map(|v| (p.clone(), v.clone()))
        })
        .collect();
    out.insert("listing_children".into(), Value::Object(children));
    out.insert(
        "each".into(),
        if let Some(each) = bundle.get("each").filter(|v| v.truthy()) {
            Value::Object(BTreeMap::from([(name.into(), each.field(name)?.clone())]))
        } else {
            Value::Null
        },
    );
    Ok(Value::Object(out))
}
#[derive(Clone)]
pub(super) struct Decision {
    pub reasons: Vec<Value>,
    pub prior: Option<(Value, Value)>,
    pub functions: Vec<String>,
    pub proof: Option<PathBuf>,
}
impl Decision {
    fn new() -> Self {
        Self {
            reasons: vec![object([("kind", "new".into())])],
            prior: None,
            functions: vec![],
            proof: None,
        }
    }
    pub fn reuse(&self) -> bool {
        self.reasons.is_empty()
    }
    pub fn seconds(&self) -> Value {
        self.prior
            .as_ref()
            .map(|(_, s)| s.clone())
            .unwrap_or(0u64.into())
    }
    pub fn json(&self) -> Value {
        let mut v = Map::from([
            ("reuse".into(), Value::Bool(self.reuse())),
            ("reasons".into(), Value::Array(self.reasons.clone())),
        ]);
        if let Some((c, s)) = &self.prior {
            v.insert("prior_commit".into(), c.clone());
            v.insert("prior_seconds".into(), s.clone());
        }
        if !self.functions.is_empty() {
            v.insert("unchanged_functions".into(), names(&self.functions));
        }
        if let Some(p) = &self.proof {
            v.insert(
                "proof".into(),
                Value::String(paths::path_key(p.as_os_str().as_encoded_bytes())),
            );
        }
        Value::Object(v)
    }
}
pub(super) fn from_store(
    dir: &Path,
    names: &[String],
    work: &mut Workspace,
    settings: &Settings,
) -> Result<BTreeMap<String, Decision>> {
    let mut decisions = BTreeMap::new();
    let mut tables = symbols::Tables::default();
    let mut versions = BTreeMap::new();
    let ignore = settings
        .ignore
        .iter()
        .map(|p| Pattern::new(p))
        .collect::<Vec<_>>();
    let opt = compare::Options {
        env: false,
        host: settings.host,
        allow_network: settings.network,
        names: settings.names_mode,
    };
    for name in names {
        let Some(bundle) = paths::read(&unit_path(dir, name)?).ok().filter(|b| {
            b.get("kind") == Some(&"lit-unit-footprints".into())
                && b.get("units").and_then(|u| u.get(name)).is_some()
        }) else {
            decisions.insert(name.clone(), Decision::new());
            continue;
        };
        let unit = bundle.field("units")?.field(name)?;
        let view = unit_manifest(&bundle, unit)?;
        let mut reasons = compare::compare_unit(&view, &bundle, work, &opt)?;
        reasons.extend(compare::filtered_environment_changes(&bundle, &ignore)?);
        let mut old_scratch = bundle
            .get("scratch")
            .map(strings)
            .transpose()?
            .unwrap_or_default();
        old_scratch.sort_unstable_by(|a, b| paths::compare(a, b));
        let sorted_scratch = super::super::sorted(&settings.scratch);
        if old_scratch != sorted_scratch {
            reasons.push(object([
                ("kind", "scratch".into()),
                (
                    "before",
                    bundle
                        .get("scratch")
                        .cloned()
                        .unwrap_or(Value::Array(vec![])),
                ),
                ("now", super::super::names(sorted_scratch)),
            ]));
        }
        let mut kept = Vec::new();
        let mut functions = Vec::new();
        let mut loaded = bundle
            .field("tracked")?
            .object()?
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>();
        if let Some(Value::Object(untracked)) = bundle.get("untracked") {
            loaded.extend(untracked.keys().cloned());
        }
        for r in reasons {
            if r.get("kind") == Some(&"changed".into()) {
                let p = r.field("path")?.string()?;
                if let (Some(ran), Some(entry)) = (
                    unit.get("symbols").and_then(|s| s.get(p)),
                    bundle.field("tracked")?.get(p),
                ) {
                    let oid = entry
                        .array()?
                        .get(1)
                        .ok_or("Missing tracked identity")?
                        .string()?;
                    let version = symbol_path(dir, oid)?;
                    if let Some(old) = versions
                        .entry(oid.to_owned())
                        .or_insert_with(|| paths::read(&version).ok())
                        .as_ref()
                    {
                        let ctx = symbols::Context {
                            work,
                            root: &settings.root,
                            path: p,
                            loaded: &loaded,
                        };
                        if tables.unaffected(&ctx, old, ran)? {
                            functions.push(p.to_owned());
                            continue;
                        }
                    }
                }
            }
            kept.push(r);
        }
        kept.truncate(10);
        decisions.insert(
            name.clone(),
            Decision {
                reasons: kept,
                functions,
                proof: None,
                prior: Some((
                    bundle.field("commit")?.clone(),
                    unit.get("seconds")
                        .filter(|s| s.truthy())
                        .cloned()
                        .unwrap_or(0u64.into()),
                )),
            },
        );
    }
    Ok(decisions)
}
pub(super) fn shard(
    names: Vec<String>,
    decisions: &BTreeMap<String, Decision>,
    settings: &Settings,
) -> Result<Vec<String>> {
    if settings.shards == 1 {
        return Ok(names);
    }
    let mut loads = vec![0.0f64; settings.shards];
    let mut mine = Vec::new();
    let mut pool = Vec::new();
    for n in names {
        let w = number(&decisions[&n].seconds())?;
        let w = if w == 0.0 { 0.1 } else { w };
        if settings.pinned.contains(&n) {
            loads[0] += w;
            if settings.shard == 0 {
                mine.push(n);
            }
        } else {
            pool.push((n, w));
        }
    }
    pool.sort_by(|(a, wa), (b, wb)| wb.total_cmp(wa).then_with(|| paths::compare(a, b)));
    for (n, w) in pool {
        let i = (0..loads.len())
            .min_by(|&a, &b| loads[a].total_cmp(&loads[b]))
            .unwrap();
        loads[i] += w;
        if i == settings.shard {
            mine.push(n);
        }
    }
    mine.sort_by(|a, b| paths::compare(a, b));
    Ok(mine)
}
