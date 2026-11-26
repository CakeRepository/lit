//! Re-read reviews, source pins, and trusted checks before admitting a reconstructed merge.
use super::*;
pub(super) const PREFIX: &str = "codex/ci/lit-v1/";
pub(super) fn pin(n: u64) -> String {
    format!("refs/heads/lit/ready/{n}")
}
pub(super) fn rejected(n: u64) -> String {
    format!("refs/heads/lit/rejected/{n}")
}
pub(super) fn batch_name(base: &str, tip: &str) -> String {
    format!("{PREFIX}{base}/{tip}")
}
pub(super) fn oid(s: &str) -> bool {
    s.len() == 40 && crate::snapshot::valid_oid(s)
}
pub(super) fn run_id<'a>(repository: &str, url: &'a str) -> Option<&'a str> {
    let id = url.strip_prefix(&format!("https://github.com/{repository}/actions/runs/"))?;
    (!id.is_empty() && !id.starts_with('0') && id.bytes().all(|b| b.is_ascii_digit())).then_some(id)
}
pub(super) struct Sources<'a> {
    pub client: &'a Client,
    pub repository: &'a str,
    pub branch: &'a str,
    pub context: &'a str,
}
impl Sources<'_> {
    pub fn query(&self, q: &str, vars: Value) -> Result<Value> {
        let data = self
            .client
            .graphql(q, vars)?
            .get("data")
            .cloned()
            .filter(Value::truthy)
            .ok_or("GitHub could not verify intake state")?;
        Ok(data)
    }
    pub fn vars(&self, n: u64) -> Value {
        let (o, r) = self.repository.split_once('/').unwrap();
        object([("o", o.into()), ("r", r.into()), ("n", n.into())])
    }
    pub fn pull_many(&self, numbers: &[u64]) -> Result<BTreeMap<u64, (Value, Option<String>)>> {
        let (owner, name) = self.repository.split_once('/').unwrap();
        let mut seen = BTreeSet::new();
        let numbers = numbers
            .iter()
            .copied()
            .filter(|n| seen.insert(*n))
            .collect::<Vec<_>>();
        if numbers.iter().any(|&n| n == 0 || n >= 1 << 31) {
            return Err(hold("Invalid source PR number"));
        }
        let mut sources = BTreeMap::new();
        for group in numbers.chunks(25) {
            let fields=group.iter().map(|n|format!("p{n}:pullRequest(number:{n}){{...LitSource}} r{n}:ref(qualifiedName:\"{}\"){{target{{oid}}}}",pin(*n))).collect::<Vec<_>>().join(" ");
            let q=format!("query($owner:String!,$repo:String!){{repository(owner:$owner,name:$repo){{{fields}}}}} {}",include_str!("source.graphql").trim_end());
            let data = self.query(&q, object([("owner", owner.into()), ("repo", name.into())]))?;
            let found = data.field("repository")?;
            for &n in group {
                let mut pr = found.field(&format!("p{n}"))?.clone();
                if pr != Value::Null {
                    if pr.field("number")?.number()? != n {
                        return Err(hold("Unexpected source PR in GitHub response"));
                    }
                    let mut threads = pr.field("reviewThreads")?.clone();
                    let head = pr.field("headRefOid")?.clone();
                    let mut cursors = BTreeSet::new();
                    let unresolved = loop {
                        if threads
                            .field("nodes")?
                            .array()?
                            .iter()
                            .any(|r| r.get("isResolved") != Some(&Value::Bool(true)))
                        {
                            break true;
                        }
                        let page = threads.field("pageInfo")?;
                        if !page.field("hasNextPage")?.boolean()? {
                            break false;
                        }
                        let cursor = page.field("endCursor")?.string()?;
                        if !cursors.insert(cursor.to_owned()) {
                            return Err("Invalid GitHub review pagination".into());
                        }
                        let vars = with(&self.vars(n), "after", cursor.into())?;
                        let data=self.query("query($o:String!,$r:String!,$n:Int!,$after:String!){repository(owner:$o,name:$r){pullRequest(number:$n){headRefOid reviewThreads(first:100,after:$after){nodes{isResolved} pageInfo{hasNextPage endCursor}}}}}",vars)?;
                        let next = data.field("repository")?.field("pullRequest")?;
                        if next.get("headRefOid") != Some(&head) {
                            return Err(hold("Source changed while reading its review threads"));
                        }
                        threads = next.field("reviewThreads")?.clone();
                    };
                    let Value::Object(ref mut m) = pr else {
                        return Err("Invalid pull request".into());
                    };
                    m.remove("reviewThreads");
                    m.insert("unresolvedReview".into(), Value::Bool(unresolved));
                }
                let r = found.field(&format!("r{n}"))?;
                let pin = if r.truthy() {
                    Some(r.field("target")?.field("oid")?.string()?.to_owned())
                } else {
                    None
                };
                sources.insert(n, (pr, pin));
            }
        }
        Ok(sources)
    }
    pub fn pull(&self, n: u64) -> Result<(Value, Option<String>)> {
        self.pull_many(&[n])?
            .remove(&n)
            .ok_or_else(|| "Missing source PR".into())
    }
    pub fn trusted<'a>(&self, pr: &'a Value, head: &str) -> Option<&'a Value> {
        let commit = pr
            .get("commits")?
            .get("nodes")?
            .array()
            .ok()?
            .last()?
            .get("commit")?;
        if string(commit, "oid") != Some(head) {
            return None;
        }
        commit
            .get("status")?
            .get("contexts")?
            .array()
            .ok()?
            .iter()
            .find(|s| {
                string(s, "context") == Some(self.context)
                    && s.get("creator").and_then(|c| string(c, "login")) == Some("github-actions")
                    && string(s, "targetUrl")
                        .and_then(|u| run_id(self.repository, u))
                        .is_some()
            })
    }
    pub fn eligibility(&self, pr: &Value, head: &str) -> Result<Option<String>> {
        let reason = if !pr.truthy()
            || string(pr, "state") != Some("OPEN")
            || pr.field("isDraft")?.boolean()?
            || string(pr, "baseRefName") != Some(self.branch)
        {
            "source is closed, draft, missing, or retargeted"
        } else if pr
            .get("headRepository")
            .and_then(|v| string(v, "nameWithOwner"))
            != Some(self.repository)
            || pr.field("isCrossRepository")?.boolean()?
        {
            "source is from another repository"
        } else if pr.field("headRefName")?.string()?.starts_with(PREFIX)
            || pr.field("headRefName")?.string()?.starts_with("lit/")
            || string(pr, "headRefOid") != Some(head)
        {
            "source revision changed or is an intake branch"
        } else if pr.field("unresolvedReview")?.boolean()? {
            "source has unresolved review threads"
        } else if pr.field("isInMergeQueue")?.boolean()? || pr.field("autoMergeRequest")?.truthy() {
            "source already has an individual merge request"
        } else if !approved(pr)? || string(pr, "mergeStateStatus") != Some("CLEAN") {
            return Ok(Some(format!(
                "source review or branch requirements are incomplete ({})",
                pr.field("mergeStateStatus")?.string()?
            )));
        } else if self.trusted(pr, head).and_then(|s| string(s, "state")) != Some("SUCCESS") {
            "required status is missing or unsuccessful on the pinned commit"
        } else {
            return Ok(None);
        };
        Ok(Some(reason.into()))
    }
    pub fn verify(&self, members: &[Member]) -> Result<Value> {
        if members.is_empty() {
            return Err(hold("A batch needs at least one pinned source"));
        }
        let sources = self.pull_many(&members.iter().map(|(n, _)| *n).collect::<Vec<_>>())?;
        let mut times = Map::new();
        for (n, head) in members {
            let (pr, pin) = &sources[n];
            if pin.as_ref() != Some(head) {
                return Err(hold(format!("#{n} readiness was withdrawn or replaced")));
            }
            if let Some(reason) = self.eligibility(pr, head)? {
                return Err(hold(format!("#{n}: {reason}")));
            }
            times.insert(
                n.to_string(),
                self.trusted(pr, head).unwrap().field("createdAt")?.clone(),
            );
        }
        Ok(Value::Object(times))
    }
    pub fn timeline(&self, n: u64, kind: &str, fields: &str) -> Result<Vec<Value>> {
        let q=format!("query($o:String!,$r:String!,$n:Int!){{repository(owner:$o,name:$r){{pullRequest(number:$n){{timelineItems(last:1,itemTypes:[{kind}]){{nodes{{{fields}}}}}}}}}}}");
        Ok(self
            .query(&q, self.vars(n))?
            .field("repository")?
            .field("pullRequest")?
            .field("timelineItems")?
            .field("nodes")?
            .array()?
            .to_vec())
    }
}
pub(super) fn approved(pr: &Value) -> Result<bool> {
    Ok(matches!(pr.field("reviewDecision")?, Value::Null)
        || string(pr, "reviewDecision") == Some("APPROVED"))
}

pub(super) fn manifest(repo: &Repo, name: &str, head: &str, target: &str) -> Result<Vec<Member>> {
    let (base, tip) = name
        .strip_prefix(PREFIX)
        .and_then(|s| s.split_once('/'))
        .filter(|(b, t)| oid(b) && oid(t) && *t == head)
        .ok_or_else(|| hold("Batch name does not identify its exact commit"))?;
    if !oid(target) || !repo.ancestor(base, target)? {
        return Err(hold("Batch base is not an ancestor of target"));
    }
    let history = repo.text(
        &[
            "rev-list",
            "--first-parent",
            "--reverse",
            "--parents",
            &format!("{base}..{tip}"),
        ],
        None,
    )?;
    let mut parent = base;
    let mut chain = Vec::new();
    for row in history.lines() {
        let f: Vec<_> = row.split_whitespace().collect();
        let [id, left, right] = f.as_slice() else {
            return Err(hold(
                "Batch history is not a continuous chain of source merges",
            ));
        };
        if *left != parent {
            return Err(hold(
                "Batch history is not a continuous chain of source merges",
            ));
        }
        parent = id;
        chain.push((*id, *left, *right));
    }
    if chain.is_empty() || parent != head {
        return Err(hold("Empty or incomplete batch"));
    }
    let mut members = Vec::new();
    let mut seen = BTreeSet::new();
    for group in chain.chunks(128) {
        let ids = group
            .iter()
            .map(|(id, _, _)| id.to_string())
            .collect::<Vec<_>>();
        let commits = crate::merge_tree::objects(repo, &ids, "commit")?;
        let pairs = group
            .iter()
            .map(|(_, a, b)| format!("{a} {b}\n"))
            .collect::<String>();
        let data = repo.bytes(
            &[
                "merge-tree",
                "--stdin",
                "-z",
                "--no-messages",
                "--name-only",
            ],
            Some(pairs.as_bytes()),
        )?;
        let mut rest = data.as_slice();
        for ((_, _, source), commit) in group.iter().zip(commits) {
            let end = rest
                .windows(2)
                .position(|s| s == b"\0\0")
                .ok_or_else(|| hold("Incomplete Git merge verification"))?;
            let merged = &rest[..end];
            rest = &rest[end + 2..];
            let split = commit
                .windows(2)
                .position(|s| s == b"\n\n")
                .ok_or_else(|| hold("Invalid batch commit"))?;
            let tree = commit[..split]
                .split(|b| *b == b'\n')
                .next()
                .unwrap_or_default()
                .strip_prefix(b"tree ")
                .and_then(|b| std::str::from_utf8(b).ok())
                .filter(|s| oid(s))
                .ok_or_else(|| hold("Invalid batch commit"))?;
            if commit.contains(&0) {
                return Err(hold("Invalid batch commit"));
            }
            let msg = std::str::from_utf8(&commit[split + 2..])
                .map_err(|_| hold("Invalid batch commit"))?
                .trim_end_matches('\n');
            let mut lines = msg.split('\n');
            let id = lines
                .next()
                .and_then(|s| s.strip_prefix("Batch PR #"))
                .unwrap_or("");
            let n = id
                .parse::<u64>()
                .ok()
                .filter(|_| id.bytes().all(|b| b.is_ascii_digit()));
            if n.is_none()
                || lines.next() != Some("")
                || lines.next() != Some(&format!("Pull-Request: #{id}"))
                || !lines
                    .next()
                    .and_then(|s| s.strip_prefix("PR-Author: "))
                    .is_some_and(|s| !s.is_empty())
                || lines.next() != Some(&format!("PR-Head: {source}"))
                || lines.next().is_some()
                || !seen.insert(n.unwrap())
            {
                return Err(hold("Batch commit does not identify a unique source PR"));
            }
            let n = n.unwrap();
            if merged != format!("1\0{tree}").as_bytes() {
                return Err(Error::Resolution(n));
            }
            members.push((n, source.to_string()));
        }
        if !rest.is_empty() {
            return Err(hold("Incomplete Git merge verification"));
        }
    }
    Ok(members)
}
