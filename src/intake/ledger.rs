use super::*;
use guard::{batch_name, manifest, pin, rejected};

fn compact(v: &Value, order: &[String]) -> String {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<_> = m.keys().collect();
            keys.sort_by_key(|k| order.iter().position(|s| s == *k).unwrap_or(usize::MAX));
            let green: Vec<_> = v
                .get("members")
                .and_then(|v| members(v).ok())
                .unwrap_or_default()
                .into_iter()
                .map(|(n, _)| n.to_string())
                .collect();
            let entries = keys
                .iter()
                .map(|k| {
                    let inner = match k.as_str() {
                        "source_green_at" => green.clone(),
                        "closing" => vec!["remove".into(), "pending".into(), "reject".into()],
                        "conflict_retry" => vec!["event".into(), "boundary".into()],
                        _ => vec![],
                    };
                    format!(
                        "{}:{}",
                        json::canonical(&Value::String((**k).clone())),
                        compact(&m[*k], &inner)
                    )
                })
                .collect::<Vec<_>>();
            format!("{{{}}}", entries.join(","))
        }
        Value::Array(a) => format!(
            "[{}]",
            a.iter()
                .map(|v| compact(v, &[]))
                .collect::<Vec<_>>()
                .join(",")
        ),
        v => json::canonical(v),
    }
}
impl Intake<'_> {
    pub(super) fn refs(&self) -> Result<Refs> {
        let text = self.repo.text(
            &[
                "ls-remote",
                "--heads",
                &self.url,
                &self.target,
                &self.active,
                "refs/heads/lit/ready/*",
                "refs/heads/lit/rejected/*",
            ],
            None,
        )?;
        text.lines()
            .map(|l| {
                let (oid, r) = l.split_once('\t').ok_or("Invalid Git reference")?;
                if !guard::oid(oid) {
                    return Err("Invalid Git reference".into());
                }
                Ok((r.into(), oid.into()))
            })
            .collect()
    }
    pub(super) fn push(&self, edits: Vec<Edit>) -> Result<()> {
        let mut args = vec!["push".to_owned(), "--atomic".to_owned()];
        for (r, old, _) in &edits {
            args.push(format!("--force-with-lease={r}:{old}"));
        }
        args.push(self.url.clone());
        args.extend(edits.into_iter().map(|(r, _, new)| format!("{new}:{r}")));
        self.repo
            .bytes(&args.iter().map(String::as_str).collect::<Vec<_>>(), None)?;
        Ok(())
    }
    pub(super) fn fetch<'a>(&self, heads: impl IntoIterator<Item = &'a str>) -> Result<()> {
        let mut seen = BTreeSet::new();
        let heads: Vec<_> = heads.into_iter().filter(|h| seen.insert(*h)).collect();
        if heads.is_empty() {
            return Ok(());
        }
        if heads.iter().any(|h| !guard::oid(h)) {
            return Err(hold("Expected full commit IDs from GitHub"));
        }
        let raw = self.repo.text(
            &["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            Some(format!("{}\n", heads.join("\n")).as_bytes()),
        )?;
        let body: Vec<_> = raw.lines().collect();
        if body.len() != heads.len() {
            return Err(hold("Incomplete Git object check"));
        }
        let mut missing = Vec::new();
        for (id, row) in heads.into_iter().zip(body) {
            if row == format!("{id} missing") {
                missing.push(id);
            } else if row != format!("{id} commit") {
                return Err(hold("Unexpected Git object in source commit set"));
            }
        }
        if !missing.is_empty() {
            self.repo.bytes(
                &[
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--stdin",
                    &self.url,
                ],
                Some(format!("{}\n", missing.join("\n")).as_bytes()),
            )?;
        }
        Ok(())
    }
    pub(super) fn record(&self, value: &Value, parent: &str) -> Result<String> {
        let tree = self
            .repo
            .text(&["rev-parse", &format!("{parent}^{{tree}}")], None)?;
        let ordered = [
            "base",
            "tip",
            "members",
            "remaining",
            "started",
            "source_green_at",
            "originals",
            "individual",
            "red",
            "conflict_retry",
            "closing",
        ]
        .map(str::to_owned);
        Ok(self.repo.text(
            &[
                "-c",
                "user.name=lit",
                "-c",
                "user.email=batch@localhost",
                "commit-tree",
                &tree,
                "-p",
                parent,
                "-m",
                &compact(value, &ordered),
            ],
            None,
        )?)
    }
    pub(super) fn state(&mut self, lock: &str) -> Result<Value> {
        self.fetch([lock])?;
        let record = json::parse(&self.repo.text(&["show", "-s", "--format=%B", lock], None)?)?;
        if let Some(tip) = string(&record, "tip") {
            let base = record.field("base")?.string()?;
            if !self
                .verified
                .as_ref()
                .is_some_and(|(b, t, _)| b == base && t == tip)
            {
                self.verified = Some((
                    base.into(),
                    tip.into(),
                    manifest(&self.repo, &batch_name(base, tip), tip, base)?,
                ));
            }
            if self.verified.as_ref().unwrap().2 != members(record.field("members")?)? {
                return Err(hold(
                    "Stored source list disagrees with the batch commit graph",
                ));
            }
        }
        Ok(record)
    }
    pub(super) fn register(&self, numbers: &[u64]) -> Result<()> {
        let refs = self.refs()?;
        let sources = self.src.pull_many(numbers)?;
        let mut edits = Vec::new();
        let mut admitted = 0;
        let mut seen = BTreeSet::new();
        for n in numbers.iter().filter(|n| seen.insert(**n)) {
            let pr = &sources[n].0;
            let head = string(pr, "headRefOid").unwrap_or("");
            if let Some(why) = self.src.eligibility(pr, head)? {
                println!("Hold #{n}: {why}");
                continue;
            }
            let held = refs.get(&rejected(*n));
            if held.map(String::as_str) == Some(head) {
                println!(
                    "#{n} failed checks alone at {}; waiting for a new push",
                    &head[..head.len().min(12)]
                );
                continue;
            }
            if let Some(old) = held {
                edits.push((rejected(*n), old.clone(), String::new()));
            }
            admitted += 1;
            let r = pin(*n);
            if refs.get(&r).map(String::as_str) != Some(head) {
                let old = refs.get(&r).cloned().unwrap_or_default();
                edits.push((r, old, head.to_owned()));
            }
        }
        if admitted == 0 {
            return Err(hold("None of the requested source revisions are ready"));
        }
        if !edits.is_empty() {
            self.fetch(
                edits
                    .iter()
                    .map(|(_, _, h)| h.as_str())
                    .filter(|h| !h.is_empty()),
            )?;
            self.push(edits)?;
        }
        Ok(())
    }
    pub(super) fn pins(refs: &Refs) -> Vec<Member> {
        let mut pins = refs
            .iter()
            .filter_map(|(r, h)| {
                let id = r.strip_prefix("refs/heads/lit/ready/")?;
                if id.starts_with('0') || !id.bytes().all(|c| c.is_ascii_digit()) {
                    return None;
                }
                Some((id.parse().ok()?, h.clone()))
            })
            .collect::<Vec<_>>();
        pins.sort();
        pins
    }
    pub(super) fn assemble(
        &self,
        refs: &Refs,
        selected: Option<&[Member]>,
        limit: Option<usize>,
    ) -> Result<Option<(String, String)>> {
        let base = refs
            .get(&self.target)
            .ok_or("Missing intake target branch")?;
        let pins = Self::pins(refs);
        let requests = selected
            .unwrap_or(&pins)
            .iter()
            .filter(|(n, h)| refs.get(&pin(*n)) == Some(h))
            .collect::<Vec<_>>();
        let found = self
            .src
            .pull_many(&requests.iter().map(|(n, _)| *n).collect::<Vec<_>>())?;
        let mut source = Vec::new();
        for (n, h) in requests {
            let (pr, pinned) = &found[n];
            let reason = self.src.eligibility(pr, h)?;
            if reason.is_some() || pinned.as_ref() != Some(h) {
                println!(
                    "Hold #{n}: {}",
                    reason.as_deref().unwrap_or("ready ref changed")
                );
                continue;
            }
            source.push(object([
                ("number", (*n).into()),
                ("head", h.as_str().into()),
                ("author", pr.field("author")?.field("login")?.clone()),
            ]));
        }
        if let Some(limit) = limit.filter(|&n| source.len() > n) {
            println!(
                "Batching {limit} of {} ready PRs; the rest wait for the next batch.",
                source.len()
            );
            source.truncate(limit);
        }
        if source.is_empty() {
            return Ok(None);
        }
        self.fetch(
            std::iter::once(base.as_str()).chain(source.iter().map(|v| string(v, "head").unwrap())),
        )?;
        let mut changes = crate::changes::ordered(&self.repo, base, &source)?;
        let state = self.repo.path().join("lit-state");
        let jobs = std::thread::available_parallelism()
            .map_or(1, |n| n.get())
            .min(8);
        let mut engine = crate::merge::Engine {
            repo: &self.repo,
            state: &state,
            jobs,
            hinted: BTreeSet::new(),
        };
        let stamp = std::env::var("SOURCE_DATE_EPOCH")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or_else(|| jiff::Timestamp::now().as_second());
        while !changes.is_empty() {
            let batch = crate::run::assemble(
                &mut engine,
                base,
                &changes,
                stamp,
                &BTreeSet::new(),
                &BTreeMap::new(),
            )?;
            for issue in batch.entry.field("pending")?.array()? {
                println!(
                    "Hold #{}: {}",
                    issue.field("number")?.number()?,
                    super::repr(issue)
                );
            }
            if batch.members.is_empty() {
                return Ok(None);
            }
            match manifest(&self.repo, &batch_name(base, &batch.tip), &batch.tip, base) {
                Ok(_) => return Ok(Some((base.clone(), batch.tip))),
                Err(e @ Error::Resolution(n)) => {
                    println!("Hold #{n}: {e}");
                    changes.retain(|c| c.number != n);
                }
                Err(e) => return Err(e),
            }
        }
        Ok(None)
    }
    pub(super) fn claim(
        &self,
        refs: &Refs,
        base: &str,
        tip: &str,
        remaining: Vec<Value>,
        red: Option<&Value>,
    ) -> Result<()> {
        let target = refs.get(&self.target).ok_or("Missing target branch")?;
        self.fetch([target.as_str()])?;
        let name = batch_name(base, tip);
        let m = manifest(&self.repo, &name, tip, target)?;
        let green = self.src.verify(&m)?;
        let mut row = object([
            ("base", base.into()),
            ("tip", tip.into()),
            ("members", rows(&m)),
            ("remaining", Value::Array(remaining)),
            ("started", Value::String(now()?)),
            ("source_green_at", green),
        ]);
        if let Some(red) = red.filter(|r| r.truthy()) {
            row = with(&row, "red", red.clone())?;
        }
        let next = self.record(&row, tip)?;
        self.push(vec![
            (
                self.active.clone(),
                refs.get(&self.active).cloned().unwrap_or_default(),
                next,
            ),
            (format!("refs/heads/{name}"), String::new(), tip.into()),
        ])
    }
    pub(super) fn change_record(&self, lock: &str, row: &Value) -> Result<()> {
        self.push(vec![(
            self.active.clone(),
            lock.into(),
            self.record(row, lock)?,
        )])
    }
    pub(super) fn close_later(
        &self,
        lock: &str,
        row: &Value,
        remove: &[Member],
        pending: Vec<Value>,
    ) -> Result<()> {
        self.close_rejecting(lock, row, remove, pending, &[])
    }
    /// Like `close_later`, and also records `reject` (sources proven red at these heads)
    /// so the same push that drops their pins leaves a durable rejection.
    pub(super) fn close_rejecting(
        &self,
        lock: &str,
        row: &Value,
        remove: &[Member],
        pending: Vec<Value>,
        reject: &[Member],
    ) -> Result<()> {
        let mut plan = object([("remove", rows(remove)), ("pending", Value::Array(pending))]);
        if !reject.is_empty() {
            plan = with(&plan, "reject", rows(reject))?;
        }
        self.change_record(lock, &with(row, "closing", plan)?)
    }
    pub(super) fn finish(
        &self,
        refs: &Refs,
        lock: &str,
        record: &Value,
        remove: &[Member],
        pending: Vec<Value>,
    ) -> Result<()> {
        self.finish_rejecting(refs, lock, record, remove, pending, &[])
    }
    pub(super) fn finish_rejecting(
        &self,
        refs: &Refs,
        lock: &str,
        record: &Value,
        remove: &[Member],
        pending: Vec<Value>,
        reject: &[Member],
    ) -> Result<()> {
        self.push(self.finish_edits(refs, lock, record, remove, pending, reject)?)
    }
    /// The ref updates that close `record`: drop the pins of `remove`, record `reject`, and
    /// leave `pending` (or nothing) as the next record.
    pub(super) fn finish_edits(
        &self,
        refs: &Refs,
        lock: &str,
        record: &Value,
        remove: &[Member],
        pending: Vec<Value>,
        reject: &[Member],
    ) -> Result<Vec<Edit>> {
        let mut edits: Vec<_> = remove
            .iter()
            .filter(|(n, h)| refs.get(&pin(*n)) == Some(h))
            .map(|(n, h)| (pin(*n), h.clone(), String::new()))
            .collect();
        edits.extend(
            reject
                .iter()
                .filter(|(n, h)| refs.get(&rejected(*n)) != Some(h))
                .map(|(n, h)| {
                    (
                        rejected(*n),
                        refs.get(&rejected(*n)).cloned().unwrap_or_default(),
                        h.clone(),
                    )
                }),
        );
        let red = record.get("red").filter(|v| v.truthy());
        let next = if !pending.is_empty()
            || (red.is_some() && self.limit == Limit::Auto && !self.originals)
        {
            let mut row = object([("remaining", Value::Array(pending))]);
            if let Some(red) = red {
                row = with(&row, "red", red.clone())?;
            }
            self.record(&row, lock)?
        } else {
            String::new()
        };
        edits.push((self.active.clone(), lock.into(), next));
        Ok(edits)
    }
}
