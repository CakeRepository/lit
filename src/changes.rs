//! Validate snapshot identities and order stacked pull requests before assembly.
use crate::git::Repo;
use crate::json::Value;
use crate::snapshot::{valid_author, valid_oid};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone)]
pub struct Change {
    pub number: u64,
    pub head: String,
    pub author: String,
    pub login: Option<String>,
    pub requires: BTreeSet<u64>,
}

impl Change {
    pub fn participant(&self) -> String {
        format!("PR#{}@{} ({})", self.number, self.head, self.author)
    }
    pub fn message(&self) -> String {
        let mut msg = format!(
            "Batch PR #{}\n\nPull-Request: #{}\nPR-Author: {}\n",
            self.number, self.number, self.author
        );
        if let Some(login) = &self.login {
            msg.push_str(&format!("PR-Login: {login}\n"));
        }
        msg.push_str(&format!("PR-Head: {}\n", self.head));
        msg
    }
}

pub fn ordered(repo: &Repo, base: &str, rows: &[Value]) -> Result<Vec<Change>, String> {
    if repo.text(&["rev-parse", "--is-bare-repository"], None)? != "true" {
        return Err("Use a dedicated bare clone".into());
    }
    let mut changes = Vec::new();
    let mut seen = BTreeSet::new();
    let mut by_head = BTreeMap::<String, Vec<u64>>::new();
    for row in rows {
        let number = row.field("number")?.number()?;
        let head = row.field("head")?.string()?.to_owned();
        let login = row.field("author")?.string()?;
        if number == 0 || !seen.insert(number) || !valid_author(login) {
            return Err("Invalid or duplicate PR in snapshot".into());
        }
        let agent = match row.get("agent") {
            None | Some(Value::Null) => None,
            Some(v) => Some(v.string()?),
        };
        if agent.is_some_and(|n| !valid_author(n)) {
            return Err(format!("Invalid agent for PR #{number} in snapshot"));
        }
        let author = agent.map(str::trim).unwrap_or(login);
        by_head.entry(head.clone()).or_default().push(number);
        changes.push(Change {
            number,
            head,
            author: author.into(),
            login: (author != login).then(|| login.into()),
            requires: BTreeSet::new(),
        });
    }
    let oids: BTreeSet<_> = by_head.keys().map(String::as_str).chain([base]).collect();
    if !oids.iter().all(|id| valid_oid(id)) {
        return Err("Snapshot must contain full commit IDs".into());
    }
    let input = oids.iter().map(|id| format!("{id}\n")).collect::<String>();
    let graph = repo.text(
        &["rev-list", "--no-walk=unsorted", "--parents", "--stdin"],
        Some(input.as_bytes()),
    )?;
    let mut parents = BTreeMap::new();
    for line in graph.lines() {
        let mut words = line.split_whitespace();
        parents.insert(
            words.next().ok_or("Invalid Git commit")?,
            words.collect::<Vec<_>>(),
        );
    }
    if !parents.keys().copied().eq(oids.iter().copied()) {
        return Err("Snapshot must contain locally available commit objects".into());
    }
    for change in &mut changes {
        if change.head == base || parents[change.head.as_str()] == [base] {
            continue;
        }
        let commits = repo.text(&["rev-list", &change.head, &format!("^{base}")], None)?;
        for oid in commits.lines().skip(1) {
            if let Some(numbers) = by_head.get(oid) {
                change.requires.extend(numbers);
            }
        }
    }
    let mut ordered = Vec::new();
    while !changes.is_empty() {
        let (ready, waiting): (Vec<_>, Vec<_>) = changes
            .into_iter()
            .partition(|c| c.requires.is_disjoint(&seen));
        if ready.is_empty() {
            return Err("Cyclic PR dependencies".into());
        }
        for c in &ready {
            seen.remove(&c.number);
        }
        ordered.extend(ready);
        changes = waiting;
    }
    Ok(ordered)
}
