use crate::args::Args;
use crate::git::Repo;
use crate::github::Client;
use crate::json::{object, Value};
use crate::snapshot;
use fancy_regex::Regex;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
const PULLS_QUERY: &str = r#"query($owner:String!,$name:String!,$branch:String!,$ref:String!,$after:String){
    repository(owner:$owner,name:$name){ref(qualifiedName:$ref){target{oid}}
      pullRequests(states:OPEN,baseRefName:$branch,first:50,after:$after,orderBy:{field:CREATED_AT,direction:ASC}){
        pageInfo{hasNextPage endCursor}
        nodes{number headRefOid baseRefName isDraft author{login} authorAssociation body
          labels(first:100){nodes{name}}
          latestOpinionatedReviews(first:100){nodes{state author{login} commit{oid}}}}}}}"#;
const QUEUE_QUERY: &str = r#"query($owner:String!,$name:String!,$branch:String!,$ref:String!,$after:String){
    repository(owner:$owner,name:$name){ref(qualifiedName:$ref){target{oid}}
      mergeQueue(branch:$branch){entries(first:100,after:$after){pageInfo{hasNextPage endCursor}
        nodes{state headCommit{oid tree{oid}} pullRequest{number headRefOid baseRefName isDraft state author{login} body}}}}}}"#;

pub fn command(arguments: &[String]) -> i32 {
    if arguments
        .iter()
        .take_while(|a| a.as_str() != "--")
        .any(|a| matches!(a.as_str(), "-h" | "--help"))
    {
        print!("{}", include_str!("help/collect.txt"));
        return 0;
    }
    match execute(arguments) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}

fn execute(arguments: &[String]) -> Result<(), String> {
    let args = Args::with_options(
        arguments,
        &[
            "--branch",
            "--repo",
            "--out",
            "--label",
            "--min-reviews",
            "--author-pattern",
        ],
        &["--hold-outside"],
        &["--prs", "--allow-outside"],
    )?;
    let [repository] = args.positional[..] else {
        return Err("Expected a GitHub owner/repository".into());
    };
    let out = Path::new(args.required("--out")?);
    let repo = Path::new(args.required("--repo")?);
    let pattern = args
        .optional("--author-pattern")
        .filter(|v| !v.is_empty())
        .map(|v| {
            Regex::new(&format!("(?m){v}"))
                .map_err(|e| format!("--author-pattern is not a valid regex: {e}"))
        })
        .transpose()?;
    let branch = args.optional("--branch").unwrap_or("main");
    let mut selection = BTreeMap::new();
    for (flag, key) in [("--prs", "prs"), ("--allow-outside", "allow_outside")] {
        let mut numbers = BTreeSet::new();
        for v in args.all(flag) {
            let n = v
                .parse::<u64>()
                .ok()
                .filter(|n| *n > 0)
                .ok_or("PR numbers must be positive integers")?;
            numbers.insert(n);
        }
        if !numbers.is_empty() {
            selection.insert(
                key.into(),
                Value::Array(numbers.into_iter().map(Value::from).collect()),
            );
        }
    }
    if args.has("--hold-outside") {
        selection.insert("hold_outside".into(), Value::Bool(true));
    } else if args.has("--allow-outside") {
        return Err("--allow-outside needs --hold-outside".into());
    }
    if let Some(label) = args.optional("--label").filter(|v| !v.is_empty()) {
        selection.insert("label".into(), label.into());
    }
    if let Some(text) = args.optional("--min-reviews") {
        let n = text
            .parse::<u64>()
            .map_err(|_| "--min-reviews must be a positive integer")?;
        if n != 0 {
            selection.insert("min_reviews".into(), n.into());
        }
    }
    let selected = (!selection.is_empty()).then_some(Value::Object(selection));
    let live = live(
        &Client::new(),
        repository,
        branch,
        selected.as_ref(),
        pattern.as_ref(),
    )?;
    snapshot::fetch(&Repo::new(repo)?, &live, out)
}

fn valid_part(s: &str) -> bool {
    !s.is_empty()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}

pub fn repository_parts(repository: &str) -> Result<(&str, &str), String> {
    let (owner, name) = repository
        .split_once('/')
        .ok_or("Expected a GitHub owner/repository")?;
    if !valid_part(owner)
        || !valid_part(name)
        || [".", ".."].iter().any(|p| *p == name || *p == owner)
    {
        return Err("Expected a GitHub owner/repository".into());
    }
    Ok((owner, name))
}

pub fn live(
    client: &Client,
    repository: &str,
    branch: &str,
    selection: Option<&Value>,
    pattern: Option<&Regex>,
) -> Result<Value, String> {
    let (owner, name) = repository_parts(repository)?;
    if let Some(selection) = selection {
        if ["prs", "label", "min_reviews"]
            .iter()
            .all(|k| selection.get(k).is_none())
        {
            return Err("Pick changes with --prs, --label or --min-reviews".into());
        }
    }
    let api = std::env::var("LIT_GITHUB_API").unwrap_or_else(|_| "auto".into());
    let queue = selection.is_none();
    if !queue && !matches!(api.as_str(), "auto" | "rest" | "graphql") {
        return Err("LIT_GITHUB_API must be auto, graphql or rest".into());
    }
    let (base, nodes) = if !queue && api == "rest" {
        rest_prs(client, repository, branch, selection)?
    } else {
        match graphql_pages(client,owner,name,branch,queue) {
            Ok(rows) => rows,
            Err((true,_)) if queue => return Err("Could not read the merge queue: GitHub GraphQL refused the request. Pick the changes with --prs, --label or --min-reviews, which also work over REST".into()),
            Err((true,_)) if api == "auto" => {
                eprintln!("lit: GitHub GraphQL refused; reading PRs over REST");
                rest_prs(client,repository,branch,selection)?
            }
            Err((_,e)) => return Err(e),
        }
    };
    let (rows, groups) = if let Some(rules) = selection {
        (select(&nodes, branch, rules, pattern)?, Vec::new())
    } else {
        queue_changes(&nodes, branch, pattern)?
    };
    let mut snapshot = BTreeMap::from([
        ("repository".into(), repository.into()),
        ("branch".into(), branch.into()),
        ("base".into(), Value::String(base)),
        ("changes".into(), Value::Array(rows)),
        ("groups".into(), Value::Array(groups)),
    ]);
    if let Some(s) = selection {
        snapshot.insert("selection".into(), s.clone());
    }
    Ok(Value::Object(snapshot))
}

/// Transport errors may fall back to REST. A missing branch or a changing base must stop collection.
fn graphql_pages(
    client: &Client,
    owner: &str,
    name: &str,
    branch: &str,
    queue: bool,
) -> Result<(String, Vec<Value>), (bool, String)> {
    let mut cursor = Value::Null;
    let mut seen = BTreeSet::new();
    let mut base = None;
    let mut rows = Vec::new();
    loop {
        let vars = object([
            ("owner", owner.into()),
            ("name", name.into()),
            ("branch", branch.into()),
            ("ref", Value::String(format!("refs/heads/{branch}"))),
            ("after", cursor),
        ]);
        let result = client
            .graphql(if queue { QUEUE_QUERY } else { PULLS_QUERY }, vars)
            .map_err(|e| (true, e))?;
        let page = || -> Result<(&str, &Value), String> {
            let data = result
                .get("data")
                .and_then(|d| d.get("repository"))
                .filter(|v| **v != Value::Null)
                .ok_or_else(|| format!("Repository {owner}/{name} not found"))?;
            let reference = data.field("ref")?;
            if *reference == Value::Null {
                return Err(format!("Branch {branch} not found in {owner}/{name}"));
            }
            let target = reference.field("target")?.field("oid")?.string()?;
            let list = if queue {
                let q = data.field("mergeQueue")?;
                if *q == Value::Null {
                    return Err("This branch has no GitHub merge queue. Pick the changes with --prs, --label or --min-reviews instead".into());
                }
                q.field("entries")?
            } else {
                data.field("pullRequests")?
            };
            Ok((target, list))
        };
        let (target, list) = page().map_err(|e| (false, e))?;
        if base.as_deref().is_some_and(|b| b != target) {
            return Err((
                false,
                "Target advanced during collection; collect again".into(),
            ));
        }
        base = Some(target.to_owned());
        let field = |e| (false, e);
        rows.extend_from_slice(list.field("nodes").and_then(Value::array).map_err(field)?);
        let info = list.field("pageInfo").map_err(field)?;
        if !info
            .field("hasNextPage")
            .and_then(Value::boolean)
            .map_err(field)?
        {
            return Ok((target.to_owned(), rows));
        }
        let after = info
            .field("endCursor")
            .and_then(Value::string)
            .map_err(field)?;
        if !seen.insert(after.to_owned()) {
            return Err((false, "GitHub returned a repeated page cursor".into()));
        }
        cursor = after.into();
    }
}

fn rest_target(client: &Client, repository: &str, branch: &str) -> Result<String, String> {
    if !branch.split('/').all(valid_part) || branch.contains("..") {
        return Err("Unsupported branch name".into());
    }
    let response = client
        .get(&format!("repos/{repository}/git/ref/heads/{branch}"))
        .map_err(|_| format!("Branch {branch} not found in {repository}"))?;
    let reference = response.field("object")?;
    if reference.field("type")?.string()? != "commit" {
        return Err(format!("Branch {branch} does not point at a commit"));
    }
    Ok(reference.field("sha")?.string()?.into())
}

fn rest_prs(
    client: &Client,
    repository: &str,
    branch: &str,
    selection: Option<&Value>,
) -> Result<(String, Vec<Value>), String> {
    let base = rest_target(client, repository, branch)?;
    let reviews = selection.and_then(|s| s.get("min_reviews")).is_some();
    let pulls = format!("repos/{repository}/pulls");
    let mut rows = Vec::new();
    for pr in client.pages(&format!("{pulls}?state=open&base={branch}"))? {
        let number = pr.field("number")?.number()?;
        let mut latest = BTreeMap::new();
        if reviews {
            for review in client.pages(&format!("{pulls}/{number}/reviews"))? {
                let Some(login) = review
                    .get("user")
                    .and_then(|u| u.get("login"))
                    .and_then(|v| v.string().ok())
                else {
                    continue;
                };
                match review.field("state")?.string()? {
                    "APPROVED" | "CHANGES_REQUESTED" => {
                        latest.insert(
                            login.to_owned(),
                            object([
                                ("author", object([("login", login.into())])),
                                ("state", review.field("state")?.clone()),
                                (
                                    "commit",
                                    object([(
                                        "oid",
                                        review.get("commit_id").cloned().unwrap_or(Value::Null),
                                    )]),
                                ),
                            ]),
                        );
                    }
                    "DISMISSED" => {
                        latest.remove(login);
                    }
                    _ => {}
                }
            }
        }
        rows.push(object([
            ("number", number.into()),
            ("headRefOid", pr.field("head")?.field("sha")?.clone()),
            ("baseRefName", pr.field("base")?.field("ref")?.clone()),
            (
                "isDraft",
                pr.get("draft").cloned().unwrap_or(Value::Bool(false)),
            ),
            ("author", pr.get("user").cloned().unwrap_or(Value::Null)),
            (
                "authorAssociation",
                pr.get("author_association").cloned().unwrap_or(Value::Null),
            ),
            ("body", pr.get("body").cloned().unwrap_or(Value::Null)),
            (
                "labels",
                object([(
                    "nodes",
                    pr.get("labels")
                        .cloned()
                        .unwrap_or_else(|| Value::Array(vec![])),
                )]),
            ),
            (
                "latestOpinionatedReviews",
                object([("nodes", Value::Array(latest.into_values().collect()))]),
            ),
        ]));
    }
    if rest_target(client, repository, branch)? != base {
        return Err("Target advanced during collection; collect again".into());
    }
    Ok((base, rows))
}

fn change(pr: &Value, pattern: Option<&Regex>) -> Result<Value, String> {
    let login = pr
        .field("author")?
        .get("login")
        .map(Value::string)
        .transpose()?
        .unwrap_or("ghost");
    let mut row = BTreeMap::from([
        ("number".into(), pr.field("number")?.clone()),
        ("head".into(), pr.field("headRefOid")?.clone()),
        ("author".into(), login.into()),
    ]);
    if let (Some(re), Some(Value::String(body))) = (pattern, pr.get("body")) {
        if let Some(captures) = re.captures(body).map_err(|e| e.to_string())? {
            if let Some(m) = captures.get(usize::from(captures.len() > 1)) {
                let name = m.as_str().trim();
                if snapshot::valid_author(name) && name != login {
                    row.insert("agent".into(), name.into());
                }
            }
        }
    }
    Ok(Value::Object(row))
}

fn number_set(rules: &Value, key: &str) -> Result<BTreeSet<u64>, String> {
    rules
        .get(key)
        .map(Value::array)
        .transpose()?
        .unwrap_or(&[])
        .iter()
        .map(Value::number)
        .collect()
}

fn numbers(items: impl Iterator<Item = u64>) -> String {
    items
        .map(|n| format!("#{n}"))
        .collect::<Vec<_>>()
        .join(", ")
}

fn select(
    nodes: &[Value],
    branch: &str,
    rules: &Value,
    pattern: Option<&Regex>,
) -> Result<Vec<Value>, String> {
    let wanted = number_set(rules, "prs")?;
    let allowed = number_set(rules, "allow_outside")?;
    let label = rules.get("label").map(Value::string).transpose()?;
    let minimum = rules
        .get("min_reviews")
        .map(Value::number)
        .transpose()?
        .unwrap_or(0);
    let hold = rules.get("hold_outside") == Some(&Value::Bool(true));
    let mut seen = BTreeSet::new();
    let mut outside = BTreeSet::new();
    let mut selected = BTreeMap::new();
    for pr in nodes {
        let n = pr.field("number")?.number()?;
        if !wanted.is_empty() && !wanted.contains(&n) {
            continue;
        }
        seen.insert(n);
        if pr.field("isDraft")?.boolean()? || pr.field("baseRefName")?.string()? != branch {
            continue;
        }
        if let Some(label) = label {
            let labels = pr.field("labels")?.field("nodes")?.array()?;
            if !labels
                .iter()
                .any(|l| l.get("name").and_then(|v| v.string().ok()) == Some(label))
            {
                continue;
            }
        }
        if minimum > 0 {
            let head = pr.field("headRefOid")?;
            let mut approved = BTreeSet::new();
            for r in pr
                .field("latestOpinionatedReviews")?
                .field("nodes")?
                .array()?
            {
                if r.field("state")?.string()? == "APPROVED"
                    && r.field("commit")?.get("oid") == Some(head)
                {
                    if let Some(login) = r.field("author")?.get("login") {
                        approved.insert(login.string()?);
                    }
                }
            }
            if (approved.len() as u64) < minimum {
                continue;
            }
        }
        if hold
            && !allowed.contains(&n)
            && !matches!(
                pr.get("authorAssociation").and_then(|v| v.string().ok()),
                Some("OWNER" | "MEMBER" | "COLLABORATOR")
            )
        {
            outside.insert(n);
            continue;
        }
        let row = change(pr, pattern)?;
        if selected.get(&n).is_some_and(|old| old != &row) {
            return Err("PR changed during collection; collect again".into());
        }
        selected.insert(n, row);
    }
    let missing = numbers(wanted.difference(&seen).copied());
    if !missing.is_empty() {
        return Err(format!("Not open against {branch}: {missing}"));
    }
    if !outside.is_empty() {
        eprintln!("lit: held for review, from outside contributors: {} (after review, collect again with --allow-outside)",numbers(outside.iter().copied()));
    }
    let skipped = numbers(
        wanted
            .iter()
            .copied()
            .filter(|n| !selected.contains_key(n) && !outside.contains(n)),
    );
    if !skipped.is_empty() {
        return Err(format!("Draft or not matching the other rules: {skipped}"));
    }
    Ok(selected.into_values().collect())
}

fn queue_changes(
    nodes: &[Value],
    branch: &str,
    pattern: Option<&Regex>,
) -> Result<(Vec<Value>, Vec<Value>), String> {
    let mut changes = Vec::new();
    let mut positions = BTreeMap::<u64, usize>::new();
    let mut groups = Vec::new();
    let mut group_positions = BTreeMap::new();
    for entry in nodes {
        let pr = entry.field("pullRequest")?;
        if pr.field("state")?.string()? != "OPEN"
            || pr.field("isDraft")?.boolean()?
            || pr.field("baseRefName")?.string()? != branch
        {
            continue;
        }
        let n = pr.field("number")?.number()?;
        let row = change(pr, pattern)?;
        if let Some(i) = positions.get(&n) {
            if changes[*i] != row {
                return Err("PR changed during collection; collect again".into());
            }
        } else {
            positions.insert(n, changes.len());
            changes.push(row);
        }
        if let Some(commit) = entry.get("headCommit").filter(|v| **v != Value::Null) {
            let group = object([
                ("number", n.into()),
                ("head", commit.field("oid")?.clone()),
                ("tree", commit.field("tree")?.field("oid")?.clone()),
                ("state", entry.field("state")?.clone()),
            ]);
            if let Some(i) = group_positions.get(&n) {
                groups[*i] = group;
            } else {
                group_positions.insert(n, groups.len());
                groups.push(group);
            }
        }
    }
    Ok((changes, groups))
}
