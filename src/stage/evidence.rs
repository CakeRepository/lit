use crate::github::Client;
use crate::json::{object, Value};
use crate::propose::encode;
use std::collections::BTreeMap;

type Result<T> = std::result::Result<T, String>;

fn equals(row: &Value, key: &str, value: &str) -> bool {
    row.get(key).and_then(|v| v.string().ok()) == Some(value)
}

fn allowed_reviewer(
    client: &Client,
    repository: &str,
    reviewers: &[Value],
    user: &Value,
) -> Result<bool> {
    let id = user.field("id")?.number()?;
    let login = user.field("login")?.string()?;
    for rule in reviewers {
        let reviewer = rule.field("reviewer")?;
        if equals(rule, "type", "User") && reviewer.field("id")?.number()? == id {
            return Ok(true);
        }
        if equals(rule, "type", "Team") {
            let org = repository.split('/').next().ok_or("Missing organization")?;
            let slug = encode(reviewer.field("slug")?.string()?);
            let path = format!(
                "orgs/{}/teams/{slug}/memberships/{}",
                encode(org),
                encode(login)
            );
            match client.get(&path) {
                Ok(member) if equals(&member, "state", "active") => return Ok(true),
                Ok(_) => (),
                Err(e) if e.starts_with("GitHub 404:") => (),
                Err(e) => return Err(e),
            }
        }
    }
    Ok(false)
}

fn approval(client: &Client, repository: &str, run: &str, name: &str) -> Result<Value> {
    let environment = client.get(&format!("repos/{repository}/environments/{}", encode(name)))?;
    let id = environment.field("id")?.number()?;
    if !equals(&environment, "name", name) {
        return Err("Wrong stage environment".into());
    }
    let rules = environment.field("protection_rules")?.array()?;
    let reviewers = rules
        .iter()
        .find(|r| equals(r, "type", "required_reviewers"))
        .and_then(|r| r.get("reviewers"))
        .and_then(|r| r.array().ok())
        .filter(|r| !r.is_empty())
        .ok_or("Environment has no required reviewers")?;
    let reviews = client.get(&format!("{run}/approvals"))?;
    let mut who = None;
    for review in reviews.array()? {
        if review.field("environments")?.array()?.iter().any(|e| {
            e.get("id").and_then(|v| v.number().ok()) == Some(id) && equals(e, "name", name)
        }) {
            let user = review.field("user")?;
            if !equals(review, "state", "approved")
                || !equals(user, "type", "User")
                || !allowed_reviewer(client, repository, reviewers, user)?
            {
                return Err("Environment review did not approve this run".into());
            }
            who = Some(user.field("login")?.string()?);
        }
    }
    Ok(object([
        ("environment", name.into()),
        ("id", id.into()),
        (
            "reviewer",
            who.filter(|s| !s.is_empty())
                .ok_or("Missing human environment approval")?
                .into(),
        ),
    ]))
}

pub(super) fn read(
    client: &Client,
    repository: &str,
    snapshot: &str,
    policy: &Value,
    id: u64,
) -> Result<Value> {
    let api = format!("repos/{repository}");
    let run = format!("{api}/actions/runs/{id}");
    let row = client.get(&run)?;
    let workflow = policy.field("workflow")?.string()?;
    if row.field("id")?.number()? != id
        || !equals(&row, "head_sha", snapshot)
        || !equals(&row, "event", "push")
        || !equals(&row, "head_branch", "main")
        || !equals(&row, "path", workflow)
        || !equals(&row, "status", "completed")
        || !equals(&row, "conclusion", "success")
        || !equals(row.field("repository")?, "full_name", repository)
        || !equals(row.field("head_repository")?, "full_name", repository)
    {
        return Err("Expected a successful main push from the configured repository and workflow on this commit".into());
    }
    let attempt = row.field("run_attempt")?.number()?;
    let workflow_id = row.field("workflow_id")?.number()?;
    let mut checks = BTreeMap::new();
    for page in 1.. {
        let result = client.get(&format!(
            "{run}/attempts/{attempt}/jobs?per_page=100&page={page}"
        ))?;
        let jobs = result.field("jobs")?.array()?;
        for job in jobs {
            let name = job.field("name")?.string()?;
            if !policy
                .field("checks")?
                .array()?
                .iter()
                .any(|v| v.string() == Ok(name))
            {
                continue;
            }
            if job.field("run_id")?.number()? != id
                || !equals(job, "head_sha", snapshot)
                || !equals(job, "status", "completed")
                || !equals(job, "conclusion", "success")
                || checks
                    .insert(name.to_owned(), job.field("id")?.clone())
                    .is_some()
            {
                return Err(format!("Missing, failed or ambiguous stage check {name}"));
            }
        }
        if jobs.len() < 100 {
            break;
        }
    }
    if checks.len() != policy.field("checks")?.array()?.len() {
        return Err("Missing required stage checks".into());
    }
    let approval = match policy.field("environment")? {
        Value::Null => Value::Null,
        environment if attempt == 1 => approval(client, repository, &run, environment.string()?)?,
        _ => return Err("Environment reviews cannot be bound to a rerun attempt".into()),
    };
    let query = format!("{api}/actions/workflows/{workflow_id}/runs?branch=main&event=push&head_sha={snapshot}&per_page=1");
    let latest = client.get(&query)?;
    let current = latest
        .field("workflow_runs")?
        .array()?
        .first()
        .ok_or("No current run")?;
    if current.field("id")?.number()? != id
        || current.field("run_attempt")?.number()? != attempt
        || !equals(current, "head_sha", snapshot)
        || !equals(current, "status", "completed")
        || !equals(current, "conclusion", "success")
    {
        return Err("Stage evidence was superseded".into());
    }
    Ok(object([
        ("run", id.into()),
        ("attempt", attempt.into()),
        ("workflow", workflow.into()),
        ("checks", Value::Object(checks)),
        ("approval", approval),
    ]))
}
