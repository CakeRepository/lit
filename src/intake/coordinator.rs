use super::*;
use guard::{approved, batch_name, pin};
use std::time::Duration;
const REMOVED: &str = "REMOVED_FROM_MERGE_QUEUE_EVENT";
enum Step {
    Changed,
    Waiting,
    Idle,
}

impl Intake<'_> {
    fn enqueue(&self, pr: &Value, head: &str) -> Result<()> {
        self.src.query("mutation($pr:ID!,$head:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$pr,expectedHeadOid:$head}){mergeQueueEntry{position}}}",object([("pr",pr.field("id")?.clone()),("head",head.into())]))?;
        Ok(())
    }
    fn batch_pr(&self, record: &Value) -> Result<Value> {
        let tip = record.field("tip")?.string()?;
        let name = batch_name(record.field("base")?.string()?, tip);
        let find = || -> Result<Option<Value>> {
            let mut rows = crate::propose::prior(
                self.src.client,
                self.src.repository,
                self.src.branch,
                &name,
            )?
            .into_iter()
            .filter(|v| v.get("isCrossRepository") == Some(&Value::Bool(false)));
            let row = rows.next();
            if rows.next().is_some()
                || row
                    .as_ref()
                    .is_some_and(|r| string(r, "headRefOid") != Some(tip))
            {
                return Err(hold("The batch branch or PR was replaced"));
            }
            Ok(row)
        };
        if let Some(row) = find()? {
            return Ok(row);
        }
        let m = members(record.field("members")?)?;
        self.src.verify(&m)?;
        let nums = m
            .iter()
            .map(|(n, _)| format!("#{n}"))
            .collect::<Vec<_>>()
            .join(", ");
        let body=format!("lit collected the following ready source revisions before native queue admission. The protected merge-group workflow validates the combined change.\n\n{}\n",m.iter().map(|(n,h)|format!("- #{n} at \x60{h}\x60")).collect::<Vec<_>>().join("\n"));
        let url = crate::propose::create(
            self.src.client,
            self.src.repository,
            self.src.branch,
            &name,
            &format!("lit batch for {}", &nums[..nums.len().min(180)]),
            &body,
            false,
        )?;
        println!("{url}");
        find()?.ok_or_else(|| "New batch PR is not visible yet".into())
    }
    fn submit(
        &self,
        n: u64,
        head: &str,
        pr: &Value,
        pinned: Option<&str>,
        started: &str,
    ) -> Result<bool> {
        if string(pr, "headRefOid") == Some(head)
            && pr.get("isInMergeQueue") == Some(&Value::Bool(true))
        {
            println!("Original PR #{n} is in the protected queue.");
        } else if pinned == Some(head) {
            if let Some(reason) = self.src.eligibility(pr, head)? {
                if reason.contains("(UNKNOWN)") {
                    return Ok(false);
                }
                println!("Released #{n}: {reason}");
            } else {
                let timeline = self.src.timeline(
                    n,
                    REMOVED,
                    "... on RemovedFromMergeQueueEvent{createdAt}",
                )?;
                let removed = timeline
                    .last()
                    .map(|v| date(v.field("createdAt")?.string()?))
                    .transpose()?;
                let started = date(started)?;
                if removed.is_some_and(|r| r >= started) {
                    println!("Original PR #{n} already left the native queue.");
                } else {
                    self.enqueue(pr, head)?;
                    println!("Submitted original PR #{n} to the protected queue.");
                }
            }
        }
        Ok(true)
    }
    fn originals(&self, refs: &Refs, lock: &str, row: &Value) -> Result<Step> {
        let pins = Self::pins(refs);
        let mut originals = span(row, "originals")?;
        let tracked = originals
            .iter()
            .map(|r| {
                let [n, h, _] = r.array()? else {
                    return Err("Invalid original intake record".into());
                };
                Ok((n.number()?, h.string()?.to_owned()))
            })
            .collect::<Result<BTreeSet<_>>>()?;
        let started = now()?;
        let len = originals.len();
        originals.extend(
            pins.into_iter()
                .filter(|p| !tracked.contains(p))
                .map(|(n, h)| {
                    Value::Array(vec![n.into(), Value::String(h), started.as_str().into()])
                }),
        );
        if len != originals.len() {
            let parent = if lock.is_empty() {
                refs.get(&self.target).ok_or("Missing target")?
            } else {
                lock
            };
            self.fetch([parent])?;
            self.push(vec![(
                self.active.clone(),
                lock.into(),
                self.record(&object([("originals", Value::Array(originals))]), parent)?,
            )])?;
            return Ok(Step::Changed);
        }
        if originals.is_empty() {
            if !lock.is_empty() {
                self.finish(refs, lock, row, &[], vec![])?;
            }
            return Ok(Step::Idle);
        }
        let found = self
            .src
            .pull_many(&tracked.iter().map(|(n, _)| *n).collect::<Vec<_>>())?;
        let mut pending = vec![];
        let mut done = vec![];
        for r in originals {
            let a = r.array()?;
            let n = a[0].number()?;
            let head = a[1].string()?;
            let (pr, pin) = &found[&n];
            if self.submit(n, head, pr, pin.as_deref(), a[2].string()?)? {
                done.push((n, head.into()));
            } else {
                pending.push(r);
            }
        }
        if pending.is_empty() {
            self.finish(refs, lock, row, &done, vec![])?;
        } else if done.is_empty() {
            return Ok(Step::Waiting);
        } else {
            let next = self.record(&object([("originals", Value::Array(pending))]), lock)?;
            let mut edits: Vec<_> = done
                .into_iter()
                .filter(|(n, h)| refs.get(&pin(*n)) == Some(h))
                .map(|(n, h)| (pin(n), h, String::new()))
                .collect();
            edits.push((self.active.clone(), lock.into(), next));
            self.push(edits)?;
        }
        Ok(Step::Changed)
    }
    fn wait_reason(&mut self, n: u64, row: &Value, reason: String) -> Result<bool> {
        let current = now()?;
        let age = seconds(row.field("started")?.string()?, &current)?;
        let oldest = row
            .get("source_green_at")
            .map(Value::object)
            .transpose()?
            .into_iter()
            .flat_map(|m| m.values())
            .map(|v| date(v.string()?))
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .min();
        let notice = (n, reason, age.div_euclid(120));
        if self.last_wait.as_ref() != Some(&notice) {
            let source = if let Some(t) = oldest {
                format!(
                    ", {}s since oldest source check passed (target 120s)",
                    seconds(&t.to_string(), &current)?
                )
            } else {
                String::new()
            };
            println!("Batch #{n}, {age}s since intake{source}: {}", notice.1);
            self.last_wait = Some(notice);
        }
        Ok(false)
    }
    fn checked(
        &mut self,
        refs: &Refs,
        lock: &str,
        row: &Value,
        pr: &Value,
        removal: Option<&Value>,
    ) -> Result<bool> {
        let n = pr.field("number")?.number()?;
        let tip = row.field("tip")?.string()?;
        let (o, r) = self.src.repository.split_once('/').unwrap();
        let queue=self.src.query("query($o:String!,$r:String!,$b:String!){repository(owner:$o,name:$r){mergeQueue(branch:$b){entries(last:1){nodes{pullRequest{number} headCommit{oid}}}}}}",object([("o",o.into()),("r",r.into()),("b",self.src.branch.into())]))?;
        let queue = queue.field("repository")?.field("mergeQueue")?;
        if queue == &Value::Null {
            return Err(hold("The protected merge queue is unavailable"));
        }
        let predecessor = queue.field("entries")?.field("nodes")?.array()?.first();
        let predhead = predecessor
            .and_then(|p| p.get("headCommit"))
            .and_then(|p| string(p, "oid"));
        if let (Some(pred), Some(h)) = (predecessor, predhead) {
            if !self
                .preview
                .as_ref()
                .is_some_and(|(a, b, _)| a == h && b == tip)
            {
                self.fetch([h])?;
                let result = self
                    .repo
                    .output(&["merge-tree", "--write-tree", h, tip], None)?;
                let conflict = match result.status.code() {
                    Some(0) => false,
                    Some(1) => true,
                    _ => return Err("Could not preview the native queue commit".into()),
                };
                if conflict {
                    crate::merge::write_json(
                        &self
                            .repo
                            .path()
                            .join("lit-state/queue-conflicts")
                            .join(format!("{h}-{tip}.json")),
                        &object([
                            (
                                "predecessor",
                                pred.field("pullRequest")?.field("number")?.clone(),
                            ),
                            ("predecessor_head", h.into()),
                            ("batch_head", tip.into()),
                            (
                                "git_merge",
                                Value::String(String::from_utf8_lossy(&result.stdout).into_owned()),
                            ),
                        ]),
                    )?;
                }
                self.preview = Some((h.into(), tip.into(), conflict));
            }
            if self.preview.as_ref().unwrap().2 {
                return self.wait_reason(
                    n,
                    row,
                    format!(
                        "overlap with native PR #{}; keeping the checked batch together",
                        pred.field("pullRequest")?.field("number")?.number()?
                    ),
                );
            }
        }
        if let Some(removal) = removal {
            if predecessor.is_some() && predhead.is_none() {
                return self.wait_reason(
                    n,
                    row,
                    "waiting for the preceding native entry to acquire a test commit".into(),
                );
            }
            let boundary = predhead.unwrap_or(refs.get(&self.target).ok_or("Missing target")?);
            let event = removal.field("id")?;
            let retry = row.get("conflict_retry");
            if retry.and_then(|r| r.get("event")) != Some(event) {
                if retry.and_then(|r| string(r, "boundary")) == Some(boundary) {
                    println!("Native queue rejected batch #{n} twice on the same boundary; returning its sources for review.");
                    self.close_later(
                        lock,
                        row,
                        &members(row.field("members")?)?,
                        span(row, "remaining")?,
                    )?;
                } else {
                    self.change_record(
                        lock,
                        &with(
                            row,
                            "conflict_retry",
                            object([("event", event.clone()), ("boundary", boundary.into())]),
                        )?,
                    )?;
                }
                return Ok(true);
            }
        }
        self.enqueue(pr, tip)?;
        println!("Enqueued batch #{n} on its exact head.");
        Ok(false)
    }
    fn advance(&mut self, refs: &Refs, lock: &str, row: &Value) -> Result<bool> {
        let m = members(row.field("members")?)?;
        let remaining = span(row, "remaining")?;
        if row.get("individual").is_some_and(Value::truthy) {
            let (n, h) = m.first().ok_or("Missing original PR")?;
            let (pr, pin) = self.src.pull(*n)?;
            if !self.submit(*n, h, &pr, pin.as_deref(), row.field("started")?.string()?)? {
                return Ok(false);
            }
            self.finish(refs, lock, row, &m, remaining)?;
            return Ok(true);
        }
        if let Some(progress) = self.direct(refs, lock, row, &m, &remaining)? {
            return Ok(progress);
        }
        let batch = match self.batch_pr(row) {
            Ok(v) => v,
            Err(Error::Hold(s)) if s.contains("(UNKNOWN)") => return Ok(false),
            Err(Error::Hold(s)) if !s.contains("batch branch") => {
                self.finish(refs, lock, row, &[], remaining)?;
                return Ok(true);
            }
            Err(e) => return Err(e),
        };
        let n = batch.field("number")?.number()?;
        let tip = row.field("tip")?.string()?;
        if string(&batch, "state") == Some("MERGED") {
            let target = refs.get(&self.target).ok_or("Missing target")?;
            self.fetch([target.as_str()])?;
            if !self.repo.ancestor(tip, target)? {
                return Err(hold("Merged batch is not on the target branch"));
            }
            println!("Landed batch #{n} with {} source PRs.", m.len());
            self.finish(refs, lock, &split::counted(row, 0, m.len())?, &m, remaining)?;
            return Ok(true);
        }
        if let Some(plan) = row.get("closing") {
            if string(&batch, "state") == Some("OPEN") {
                self.src.client.patch(
                    &format!("repos/{}/pulls/{n}", self.src.repository),
                    object([("state", "closed".into())]),
                )?;
            }
            let reject = plan
                .get("reject")
                .map(members)
                .transpose()?
                .unwrap_or_default();
            self.finish_rejecting(
                refs,
                lock,
                row,
                &members(plan.field("remove")?)?,
                span(plan, "pending")?,
                &reject,
            )?;
            return Ok(true);
        }
        if string(&batch, "state") == Some("CLOSED") {
            println!("Batch #{n} closed; releasing its pins.");
            self.finish(refs, lock, row, &m, remaining)?;
            return Ok(true);
        }
        match self.src.verify(&m) {
            Ok(_) => (),
            Err(Error::Hold(s)) => {
                if s.contains("(UNKNOWN)") {
                    return Ok(false);
                }
                println!("Withdrawing batch #{n}: {s}");
                self.close_later(lock, row, &[], remaining)?;
                return Ok(true);
            }
            Err(e) => return Err(e),
        }
        let (pr, _) = self.src.pull(n)?;
        if string(&pr, "headRefOid") != Some(tip)
            || string(&pr, "baseRefName") != Some(self.src.branch)
        {
            return Err(hold("The batch PR was retargeted or its head was replaced"));
        }
        if pr.field("isDraft")?.boolean()? {
            if !self
                .src
                .timeline(n, "CONVERT_TO_DRAFT_EVENT", "__typename")?
                .is_empty()
            {
                return Ok(false);
            }
            let rest = self
                .src
                .client
                .get(&format!("repos/{}/pulls/{n}", self.src.repository))?;
            self.src.query("mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){clientMutationId}}",object([("id",rest.field("node_id")?.clone())]))?;
            return Ok(true);
        }
        if pr.field("isInMergeQueue")?.boolean()? {
            let entry = pr.get("mergeQueueEntry").unwrap_or(&Value::Null);
            let detail = entry
                .get("headCommit")
                .and_then(|c| string(c, "oid"))
                .map_or_else(
                    || "waiting for a protected group commit".into(),
                    |h| {
                        format!(
                            "protected group {} assigned",
                            h.chars().take(12).collect::<String>()
                        )
                    },
                );
            return self.wait_reason(
                n,
                row,
                format!(
                    "native queue {}, position {}, {detail}",
                    string(entry, "state").unwrap_or("None"),
                    entry.get("position").map_or("None".into(), repr)
                ),
            );
        }
        let merge = pr.field("mergeStateStatus")?.string()?;
        if merge == "DIRTY" {
            println!("Batch #{n} conflicts with current main; recollecting.");
            self.close_later(lock, row, &[], remaining)?;
            return Ok(true);
        }
        let removed =
            self.src
                .timeline(n, REMOVED, "... on RemovedFromMergeQueueEvent{id reason}")?;
        let trusted = self.src.trusted(&pr, tip);
        let status = trusted.and_then(|t| string(t, "state"));
        let removal = removed.last();
        let reason = removal.and_then(|r| string(r, "reason"));
        let red = matches!(status, Some("FAILURE" | "ERROR"));
        let green = status == Some("SUCCESS") && merge == "CLEAN" && approved(&pr)?;
        if reason == Some("merge_conflict") && !red {
            if green {
                return self.checked(refs, lock, row, &pr, removal);
            }
            return self.wait_reason(
                n,
                row,
                "rechecking the existing batch after a native merge conflict".into(),
            );
        }
        if removal.is_some() || red {
            let reason = reason
                .filter(|r| *r != "merge_conflict")
                .unwrap_or("failed_checks");
            println!("Batch #{n} left admission: {reason}");
            if reason == "failed_checks" && m.len() > 1 {
                let (remove, pending, why) = self.blame(n, row, &m, trusted, remaining)?;
                println!("Batch #{n}: {why}");
                self.close_rejecting(
                    lock,
                    &split::counted(row, remove.len(), remove.len())?,
                    &remove,
                    pending,
                    &remove,
                )?;
            } else {
                let next = if reason == "failed_checks" {
                    split::counted(row, m.len(), m.len())?
                } else {
                    row.clone()
                };
                let reject = if reason == "failed_checks" {
                    &m[..]
                } else {
                    &[]
                };
                self.close_rejecting(lock, &next, &m, remaining, reject)?;
            }
            return Ok(true);
        }
        if green {
            if let Some(progress) = self.direct_batch(refs, lock, row, &pr, &m, &remaining)? {
                return Ok(progress);
            }
            return self.checked(refs, lock, row, &pr, None);
        }
        self.wait_reason(
            n,
            row,
            format!(
                "batch checks {}, GitHub merge state {merge}",
                status.unwrap_or("pending")
            ),
        )
    }
    pub(super) fn tick(&mut self, label: &str) -> Result<()> {
        let rows = self.src.client.pages(&format!(
            "repos/{}/issues?state=open&labels={}&sort=created&direction=asc",
            self.src.repository,
            crate::propose::encode(label)
        ))?;
        let numbers = rows
            .iter()
            .filter(|r| r.get("pull_request").is_some())
            .map(|r| r.field("number")?.number())
            .collect::<std::result::Result<Vec<_>, String>>()?;
        if numbers.is_empty() {
            println!("No open PRs carry the {} label.", repr(&label.into()));
        } else {
            let sources = self.src.pull_many(&numbers)?;
            for n in &numbers {
                let pr = &sources[n].0;
                if !pr.get("autoMergeRequest").is_some_and(Value::truthy)
                    || pr.get("isInMergeQueue") != Some(&Value::Bool(false))
                    || string(pr, "state") != Some("OPEN")
                    || string(pr, "baseRefName") != Some(self.src.branch)
                    || pr.get("isCrossRepository") != Some(&Value::Bool(false))
                    || pr
                        .get("headRepository")
                        .and_then(|r| string(r, "nameWithOwner"))
                        != Some(self.src.repository)
                {
                    continue;
                }
                self.src.query("mutation($pr:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$pr}){clientMutationId}}",object([("pr",pr.field("id")?.clone())]))?;
                let note=format!("lit turned off auto-merge on this PR because it carries the \x60{label}\x60 label. lit will batch it with other ready PRs and merge it through the merge queue. To merge it on its own instead, remove the label before turning auto-merge back on.");
                self.src.client.post(
                    &format!("repos/{}/issues/{n}/comments", self.src.repository),
                    object([("body", Value::String(note))]),
                )?;
                println!("Took over #{n}: turned off its auto-merge so lit can batch it.");
            }
            match self.register(&numbers) {
                Ok(()) => (),
                Err(Error::Hold(reason)) => println!("Nothing new to pin: {reason}"),
                Err(e) => return Err(e),
            }
        }
        self.drain(false)
    }
    pub(super) fn drain(&mut self, wait: bool) -> Result<()> {
        for attempt in 0..4 {
            match self.pass(wait) {
                Err(Error::Operation(e)) if attempt < 3 => {
                    println!("Re-reading the durable intake after a failed operation: {e}");
                    pause(1 << attempt);
                }
                result => return result,
            }
        }
        unreachable!()
    }
    fn pass(&mut self, wait: bool) -> Result<()> {
        loop {
            let refs = self.refs()?;
            let lock = refs.get(&self.active).map_or("", String::as_str);
            let row = if lock.is_empty() {
                Value::Object(Map::new())
            } else {
                self.state(lock)?
            };
            let tip = string(&row, "tip");
            if self.originals && tip.is_some() && !row.get("individual").is_some_and(Value::truthy)
            {
                return Err(hold("An older wrapper is active. Withdraw its admission before submitting originals."));
            }
            if row.get("originals").is_some() || (self.originals && tip.is_none()) {
                match self.originals(&refs, lock, &row)? {
                    Step::Idle => return Ok(()),
                    Step::Changed => continue,
                    Step::Waiting if !wait => {
                        println!(
                            "Original PRs are still in GitHub's queue; the next pass resumes."
                        );
                        return Ok(());
                    }
                    Step::Waiting => pause(2),
                }
            } else if let Some(tip) = tip {
                if !self.advance(&refs, lock, &row)? {
                    if !wait {
                        println!(
                            "Batch {} is still being checked; the next pass resumes.",
                            &tip[..12]
                        );
                        return Ok(());
                    }
                    pause(30);
                }
            } else {
                let mut groups = span(&row, "remaining")?.into_iter();
                let first = groups.next().map(|g| members(&g)).transpose()?;
                let limit = if first.is_some() {
                    None
                } else {
                    split::limit(self.limit, row.get("red"))?
                };
                if let Some((base, tip)) = self.assemble(&refs, first.as_deref(), limit)? {
                    self.claim(&refs, &base, &tip, groups.collect(), row.get("red"))?;
                } else if first.is_some() {
                    self.finish(&refs, lock, &row, &[], groups.collect())?;
                } else {
                    println!("No eligible source requests remain.");
                    return Ok(());
                }
            }
        }
    }
}

fn pause(seconds: u64) {
    if crate::parity::epoch().is_none() {
        std::thread::sleep(Duration::from_secs(seconds));
    }
}
