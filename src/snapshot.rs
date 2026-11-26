use crate::files;
use crate::git::Repo;
use crate::json::{self, Value};
use std::collections::BTreeSet;
use std::path::Path;

pub fn valid_author(name: &str) -> bool {
    !name.trim().is_empty()
        && name.chars().count() <= 100
        && !name.bytes().any(|b| b < 32 || b == 127)
}

pub fn valid_oid(oid: &str) -> bool {
    matches!(oid.len(), 40 | 64)
        && oid
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Pin all fetched commits in one ref transaction before publishing the corresponding snapshot.
pub fn fetch(repo: &Repo, snapshot: &Value, out: &Path) -> Result<(), String> {
    if repo.text(&["rev-parse", "--is-bare-repository"], None)? != "true" {
        return Err("Use a dedicated bare clone".into());
    }
    let repository = snapshot.field("repository")?.string()?;
    let branch = snapshot.field("branch")?.string()?;
    let reference = format!("refs/heads/{branch}");
    repo.text(&["check-ref-format", &reference], None)?;
    let mut specs = format!("{reference}\n");
    let mut oids = BTreeSet::from([snapshot.field("base")?.string()?]);
    let mut numbers = BTreeSet::new();
    for change in snapshot.field("changes")?.array()? {
        let number = change.field("number")?.number()?;
        if number == 0
            || !numbers.insert(number)
            || !valid_author(change.field("author")?.string()?)
        {
            return Err("Invalid or duplicate PR in snapshot".into());
        }
        if let Some(agent) = change.get("agent") {
            if !valid_author(agent.string()?) {
                return Err(format!("Invalid agent for PR #{number} in snapshot"));
            }
        }
        oids.insert(change.field("head")?.string()?);
        specs.push_str(&format!("refs/pull/{number}/head\n"));
    }
    if !oids.iter().all(|oid| valid_oid(oid)) {
        return Err("Snapshot must contain full commit IDs".into());
    }
    repo.text(
        &[
            "fetch",
            "--no-tags",
            "--stdin",
            &format!("https://github.com/{repository}.git"),
        ],
        Some(specs.as_bytes()),
    )?;
    let mut queries = String::new();
    let mut pins = String::new();
    for oid in &oids {
        queries.push_str(&format!("{oid}\n"));
        pins.push_str(&format!(
            "update refs/concurrent-merge/inputs/{oid} {oid}\n"
        ));
    }
    let objects = repo.text(
        &["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        Some(queries.as_bytes()),
    )?;
    if !objects
        .lines()
        .eq(oids.iter().map(|oid| format!("{oid} commit")))
    {
        return Err("Snapshot must contain locally available commit objects".into());
    }
    repo.text(&["update-ref", "--stdin"], Some(pins.as_bytes()))?;
    files::replace(out, format!("{}\n", json::pretty(snapshot)).as_bytes())
        .map_err(|e| e.to_string())
}
