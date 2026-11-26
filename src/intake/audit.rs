//! Account for protected attempts, queue visits and trusted source latency.
use super::*;
use crate::propose::encode;
fn integer(n: i64) -> Value {
    Value::Number(n.to_string())
}
fn project(row: &Value, fields: &[(&str, &str)]) -> Result<Value> {
    Ok(Value::Object(
        fields
            .iter()
            .map(|(out, from)| Ok(((*out).into(), row.field(from)?.clone())))
            .collect::<Result<Map>>()?,
    ))
}
fn workflow(client: &Client, endpoint: &str, filters: &str) -> Result<Vec<Value>> {
    let mut rows = vec![];
    for page in 1..=10 {
        let r = client.get(&format!("{endpoint}?{filters}&per_page=100&page={page}"))?;
        if r.field("total_count")?.number()? > 1000 {
            return Err(hold("Window exceeds GitHub's 1,000-run search limit"));
        }
        let found = r.field("workflow_runs")?.array()?;
        rows.extend_from_slice(found);
        if found.len() < 100 {
            break;
        }
    }
    Ok(rows)
}
fn queue_pr(branch: &str) -> Option<u64> {
    let (prefix, group) = branch
        .strip_prefix("gh-readonly-queue/")?
        .rsplit_once("/pr-")?;
    let (id, hash) = group.split_once('-')?;
    if prefix.is_empty() || !guard::oid(hash) || !id.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    id.parse().ok()
}
impl Intake<'_> {
    fn latency(&self, n: u64, head: &str, landed: bool) -> Result<Value> {
        let repo = self.src.repository;
        let pr = self.src.client.get(&format!("repos/{repo}/pulls/{n}"))?;
        let merged = if landed && string(pr.field("head")?, "sha") == Some(head) {
            string(&pr, "merged_at")
        } else {
            None
        };
        let mut green = None;
        if let Some(end) = merged {
            'pages: for page in 1.. {
                let rows = self.src.client.get(&format!(
                    "repos/{repo}/commits/{head}/statuses?per_page=100&page={page}"
                ))?;
                let rows = rows.array()?;
                for row in rows {
                    let stamp = row.field("created_at")?.string()?;
                    if string(row, "context") != Some(self.src.context) || stamp > end {
                        continue;
                    }
                    if string(row, "state") != Some("success")
                        || row.get("creator").and_then(|v| string(v, "login"))
                            != Some("github-actions[bot]")
                        || string(row, "target_url")
                            .and_then(|u| guard::run_id(repo, u))
                            .is_none()
                    {
                        break 'pages;
                    }
                    green = Some(stamp.to_owned());
                }
                if rows.len() < 100 {
                    break;
                }
            }
        }
        let elapsed = green
            .as_ref()
            .zip(merged)
            .map(|(g, m)| seconds(g, m))
            .transpose()?;
        Ok(object([
            ("number", n.into()),
            ("head", head.into()),
            ("trusted_green_at", green.map_or(Value::Null, Value::String)),
            ("merged_at", merged.map_or(Value::Null, Value::from)),
            (
                "trusted_green_to_merge_seconds",
                elapsed.map_or(Value::Null, integer),
            ),
            (
                "within_120_seconds",
                elapsed.map_or(Value::Null, |t| Value::Bool(t <= 120)),
            ),
        ]))
    }
    fn visits(&self, n: u64, merged: Option<&str>, measured: &str) -> Result<Value> {
        let mut visits = Vec::<(String, Option<String>, Option<String>)>::new();
        let mut after = Value::Null;
        let mut cursors = BTreeSet::new();
        let queued = loop {
            let res=self.src.query("query($o:String!,$r:String!,$n:Int!,$after:String){repository(owner:$o,name:$r){pullRequest(number:$n){isInMergeQueue timelineItems(first:100,after:$after,itemTypes:[ADDED_TO_MERGE_QUEUE_EVENT,REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{__typename ... on AddedToMergeQueueEvent{createdAt} ... on RemovedFromMergeQueueEvent{createdAt reason}} pageInfo{hasNextPage endCursor}}}}}",with(&self.src.vars(n),"after",after)?)?;
            let pr = res.field("repository")?.field("pullRequest")?;
            let timeline = pr.field("timelineItems")?;
            for event in timeline.field("nodes")?.array()? {
                let at = event.field("createdAt")?.string()?.to_owned();
                if string(event, "__typename") == Some("AddedToMergeQueueEvent") {
                    visits.push((at, None, None));
                } else if let Some(last) = visits.last_mut().filter(|(_, exit, _)| exit.is_none()) {
                    last.1 = Some(at);
                    last.2 = string(event, "reason").map(str::to_owned);
                }
            }
            let page = timeline.field("pageInfo")?;
            if !page.field("hasNextPage")?.boolean()? {
                break pr.field("isInMergeQueue")?.boolean()?;
            }
            after = page.field("endCursor")?.clone();
            if !cursors.insert(after.string()?.to_owned()) {
                return Err("Invalid GitHub queue pagination".into());
            }
        };
        if let Some((_, end, reason)) = visits.last_mut().filter(|(_, e, _)| e.is_none()) {
            *end = merged.map(str::to_owned);
            *reason = Some(
                if merged.is_some() {
                    "merged"
                } else if queued {
                    "observed_open"
                } else {
                    "unknown"
                }
                .into(),
            );
        }
        Ok(Value::Array(
            visits
                .into_iter()
                .map(|(start, exit, reason)| {
                    let end = exit.as_deref().or_else(|| {
                        (reason.as_deref() == Some("observed_open")).then_some(measured)
                    });
                    let duration = end.map(|e| seconds(&start, e)).transpose()?;
                    Ok(object([
                        ("entered_at", Value::String(start)),
                        ("exited_at", exit.map_or(Value::Null, Value::String)),
                        ("reason", reason.map_or(Value::Null, Value::String)),
                        ("seconds", duration.map_or(Value::Null, integer)),
                    ]))
                })
                .collect::<Result<_>>()?,
        ))
    }
    pub(super) fn report(&self, n: u64, since: Option<&str>) -> Result<()> {
        let repo = self.src.repository;
        let found = self.src.client.get(&format!("repos/{repo}/pulls/{n}"))?;
        if string(found.field("base")?, "ref") != Some(self.src.branch) {
            return Err(Error::Usage("Batch targets a different branch".into()));
        }
        let refs = self.refs()?;
        let target = refs.get(&self.target).ok_or("Missing target")?;
        self.repo.bytes(
            &[
                "fetch",
                "--quiet",
                "--no-tags",
                &self.url,
                target,
                &format!("refs/pull/{n}/head"),
            ],
            None,
        )?;
        let head = found.field("head")?;
        let members = guard::manifest(
            &self.repo,
            head.field("ref")?.string()?,
            head.field("sha")?.string()?,
            target,
        )?;
        let since = since.unwrap_or(found.field("created_at")?.string()?);
        let start =
            date(since).map_err(|_| hold("The admission start time must include a timezone"))?;
        let created = encode(&format!(">={start:.0}"));
        let actions = format!("repos/{repo}/actions");
        let runs = workflow(
            self.src.client,
            &format!("{actions}/workflows/trusted-pr-validation.yml/runs"),
            &format!("event=merge_group&created={created}"),
        )?;
        let known: BTreeSet<_> = members.iter().map(|(n, _)| *n).chain([n]).collect();
        let mut selected = vec![];
        let mut count = 0u64;
        for r in runs {
            if let Some(pr) = string(&r, "head_branch")
                .and_then(queue_pr)
                .filter(|id| known.contains(id))
            {
                count += r.field("run_attempt")?.number()?;
                selected.push(with(
                    &project(
                        &r,
                        &[
                            ("run", "id"),
                            ("attempts", "run_attempt"),
                            ("head", "head_sha"),
                            ("conclusion", "conclusion"),
                            ("url", "html_url"),
                        ],
                    )?,
                    "number",
                    pr.into(),
                )?);
            }
        }
        let (owner, name) = repo.split_once('/').unwrap();
        let response = self.src.query(
            include_str!("report.graphql"),
            object([
                ("owner", owner.into()),
                ("name", name.into()),
                ("number", n.into()),
            ]),
        )?;
        let pr = response.field("repository")?.field("pullRequest")?;
        let merged = pr.get("mergeCommit").and_then(|v| string(v, "oid"));
        let landed = string(pr, "state") == Some("MERGED")
            && selected.iter().any(|r| {
                r.get("number") == Some(&n.into())
                    && string(r, "head") == merged
                    && string(r, "conclusion") == Some("success")
            });
        let mut overhead = BTreeMap::new();
        for r in workflow(
            self.src.client,
            &format!("{actions}/runs"),
            &format!(
                "branch={}&created={created}",
                encode(pr.field("headRefName")?.string()?)
            ),
        )? {
            overhead.insert(r.field("id")?.number()?, r);
        }
        let checks = pr
            .field("commits")?
            .field("nodes")?
            .array()?
            .first()
            .and_then(|v| v.get("commit"))
            .and_then(|v| v.get("statusCheckRollup"))
            .and_then(|v| v.get("contexts"))
            .and_then(|v| v.get("nodes"))
            .map(Value::array)
            .transpose()?
            .unwrap_or_default();
        for check in checks {
            let url = string(check, "detailsUrl")
                .or_else(|| string(check, "targetUrl"))
                .unwrap_or("");
            if let Some(id) = url
                .split_once("/actions/runs/")
                .and_then(|(_, v)| v.split(|c: char| !c.is_ascii_digit()).next())
                .and_then(|s| s.parse::<u64>().ok())
            {
                if let std::collections::btree_map::Entry::Vacant(e) = overhead.entry(id) {
                    e.insert(self.src.client.get(&format!("{actions}/runs/{id}"))?);
                }
            }
        }
        let mut extra = vec![];
        let mut attempts = 0u64;
        for r in overhead.into_values().filter(|r| {
            matches!(
                string(r, "event"),
                Some("pull_request" | "workflow_run" | "push")
            )
        }) {
            attempts += r.field("run_attempt")?.number()?;
            extra.push(project(
                &r,
                &[
                    ("run", "id"),
                    ("name", "name"),
                    ("event", "event"),
                    ("attempts", "run_attempt"),
                    ("conclusion", "conclusion"),
                ],
            )?);
        }
        let measured = now()?;
        let row = object([
            ("batch_pr", pr.field("url")?.clone()),
            (
                "sources",
                Value::Array(members.iter().map(|(n, _)| (*n).into()).collect()),
            ),
            ("since", since.into()),
            ("landed", Value::Bool(landed)),
            ("separate_entry_baseline", (members.len() as u64).into()),
            ("observed_protected_attempts", count.into()),
            (
                "protected_attempts_avoided",
                if landed {
                    integer(members.len() as i64 - count as i64)
                } else {
                    Value::Null
                },
            ),
            ("observed_runs", Value::Array(selected)),
            ("wrapper_pr_check_runs", Value::Array(extra)),
            ("wrapper_pr_workflow_attempts", attempts.into()),
            (
                "source_check_to_merge",
                Value::Array(
                    members
                        .iter()
                        .map(|(n, h)| self.latency(*n, h, landed))
                        .collect::<Result<_>>()?,
                ),
            ),
            (
                "native_queue_visits",
                self.visits(n, string(pr, "mergedAt"), &measured)?,
            ),
            ("measured_at", Value::String(measured)),
        ]);
        println!("{}", json::pretty(&row));
        Ok(())
    }
}
