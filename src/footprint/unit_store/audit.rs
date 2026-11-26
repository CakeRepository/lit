use super::*;
pub(super) struct Audits {
    pub chosen: BTreeSet<String>,
    vouchers: BTreeMap<String, Vec<String>>,
    book: Map,
}
impl Audits {
    pub fn new(
        config: &Settings,
        units: &[String],
        decisions: &BTreeMap<String, plan::Decision>,
        commit: &str,
    ) -> Result<Self> {
        let mut book = config
            .audit_book
            .as_ref()
            .map(|p| crate::proof::load_book(p))
            .unwrap_or(Value::Object(Map::new()))
            .object()?
            .clone();
        book.entry("signers".into())
            .or_insert_with(|| Value::Object(Map::new()));
        book.entry("parent".into()).or_insert(Value::Null);
        let mut rng = crate::sampling::Sampler::from_commit(commit);
        let mut indices = BTreeMap::new();
        let mut chosen = BTreeSet::new();
        let mut vouchers = BTreeMap::new();
        let mut seen = BTreeSet::new();
        for name in units {
            if !seen.insert(name) || !decisions[name].reuse() {
                continue;
            }
            let mut rate = config.audit_rate;
            if let Some(other) = decisions[name]
                .proof
                .as_ref()
                .filter(|_| config.audit_book.is_some())
            {
                let dir = job_dir(other, &config.job)?;
                let index = indices
                    .entry(dir.clone())
                    .or_insert_with(|| paths::read(&dir.join("proof-signers.json")).ok());
                if let Some(row) = index.as_ref().and_then(|v| v.get(name)) {
                    let signers = strings(row)?
                        .into_iter()
                        .map(str::to_owned)
                        .collect::<Vec<_>>();
                    for signer in &signers {
                        let clean = book["signers"]
                            .get(signer)
                            .and_then(|s| s.get("clean"))
                            .map(Value::number)
                            .transpose()?
                            .unwrap_or(0);
                        rate = rate.max(0.5f64.powi((clean / 20).min(64) as i32).max(0.05));
                    }
                    vouchers.insert(name.clone(), signers);
                }
            }
            if rng.sample() < rate {
                chosen.insert(name.clone());
            }
        }
        if config.mode == "plan" {
            chosen.clear();
        }
        Ok(Self {
            chosen,
            vouchers,
            book,
        })
    }
    pub fn finish(
        &mut self,
        config: &Settings,
        here: &BTreeSet<String>,
        ran: &Map,
        batch_ok: bool,
    ) -> Result<Value> {
        let mut counts: BTreeMap<String, (u64, u64)> = BTreeMap::new();
        let Some(path) = config
            .audit_book
            .as_ref()
            .filter(|_| config.mode == "enforce")
        else {
            return Ok(Value::Object(Map::new()));
        };
        let Some(Value::Object(signers)) = self.book.get_mut("signers") else {
            return Err("Invalid audit book signers".into());
        };
        for name in self.chosen.intersection(here) {
            let ok = if let Some(u) = ran.get(name) {
                u.field("exit_code")? == &0u64.into()
            } else {
                batch_ok
            };
            for signer in self.vouchers.get(name).into_iter().flatten() {
                let row = signers.entry(signer.clone()).or_insert_with(|| {
                    object([
                        ("clean", 0u64.into()),
                        ("failed", 0u64.into()),
                        ("reset", Value::Null),
                    ])
                });
                let Value::Object(state) = row else {
                    return Err("Invalid signer audit record".into());
                };
                let count = counts.entry(signer.clone()).or_default();
                if ok {
                    let n = state
                        .get("clean")
                        .map(Value::number)
                        .transpose()?
                        .unwrap_or(0);
                    state.insert("clean".into(), (n + 1).into());
                    count.0 += 1;
                } else {
                    let n = state
                        .get("failed")
                        .map(Value::number)
                        .transpose()?
                        .unwrap_or(0);
                    state.insert("failed".into(), (n + 1).into());
                    state.insert("clean".into(), 0u64.into());
                    state.insert(
                        "reset".into(),
                        Value::String(cache::iso(jiff::Timestamp::now())),
                    );
                    count.1 += 1;
                }
            }
        }
        if !counts.is_empty() {
            self.book.insert("kind".into(), "lit-audit-book".into());
            self.book.insert("version".into(), 1u64.into());
            save(path, &Value::Object(self.book.clone()))?;
        }
        Ok(Value::Object(
            counts
                .into_iter()
                .map(|(s, (ok, wrong))| (s, object([("ok", ok.into()), ("wrong", wrong.into())])))
                .collect(),
        ))
    }
}
