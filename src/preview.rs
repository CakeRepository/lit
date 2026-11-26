//! Predict merge trees before queue admission, then verify the observations are still current.
use crate::args::Args;
use crate::changes;
use crate::collect;
use crate::files;
use crate::git::Repo;
use crate::github::Client;
use crate::json::{self, object, Value};
use crate::snapshot::valid_oid;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

pub fn command(args: &[String]) -> i32 {
    if args
        .iter()
        .take_while(|s| s.as_str() != "--")
        .any(|s| matches!(s.as_str(), "-h" | "--help"))
    {
        print!("{}", include_str!("help/preview.txt"));
        return 0;
    }
    match preview(args) {
        Ok(prediction) => {
            println!("{}", json::pretty(&prediction));
            0
        }
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}

fn pull_many(
    client: &Client,
    repository: &str,
    numbers: &[u64],
) -> Result<BTreeMap<u64, Value>, String> {
    let (owner, repo) = collect::repository_parts(repository)?;
    let mut pulls = BTreeMap::new();
    let vars = object([("owner", owner.into()), ("repo", repo.into())]);
    for group in numbers.chunks(25) {
        let fields=group.iter().map(|n|format!("p{n}:pullRequest(number:{n}){{number state baseRefName headRefOid isCrossRepository headRepository{{nameWithOwner}} isInMergeQueue author{{login}}}}")).collect::<Vec<_>>().join(" ");
        let query=format!("query($owner:String!,$repo:String!){{repository(owner:$owner,name:$repo){{{fields}}}}}");
        let data = client
            .graphql(&query, vars.clone())
            .map_err(|_| "GitHub could not verify intake state")?;
        let source = data.field("data")?.field("repository")?;
        for n in group {
            let pr = source.field(&format!("p{n}"))?;
            if *pr != Value::Null && pr.field("number")?.number()? != *n {
                return Err("Unexpected source PR in GitHub response".into());
            }
            pulls.insert(*n, pr.clone());
        }
    }
    Ok(pulls)
}

fn get_inputs(
    repo: &Repo,
    repository: &str,
    queue: &Value,
    sources: &[Value],
) -> Result<(), String> {
    let mut ids = BTreeSet::from([queue.field("base")?.string()?]);
    for row in sources
        .iter()
        .chain(queue.field("changes")?.array()?)
        .chain(queue.field("groups")?.array()?)
    {
        ids.insert(row.field("head")?.string()?);
    }
    if !ids.iter().all(|oid| oid.len() == 40 && valid_oid(oid)) {
        return Err("Expected full commit IDs from GitHub".into());
    }
    let input = ids.iter().map(|oid| format!("{oid}\n")).collect::<String>();
    let found = repo.text(
        &["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        Some(input.as_bytes()),
    )?;
    let mut missing = String::new();
    if found.lines().count() != ids.len() {
        return Err("Incomplete Git object response".into());
    }
    for (oid, row) in ids.iter().zip(found.lines()) {
        if row == format!("{oid} missing") {
            missing.push_str(&format!("{oid}\n"));
        } else if row != format!("{oid} commit") {
            return Err("Unexpected object in source commit set".into());
        }
    }
    if !missing.is_empty() {
        repo.text(
            &[
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--stdin",
                &format!("https://github.com/{repository}.git"),
            ],
            Some(missing.as_bytes()),
        )?;
    }
    Ok(())
}

fn assemble(repo: &Repo, base: &str, rows: &[Value]) -> Result<Value, String> {
    let mut tip = base.to_owned();
    for change in changes::ordered(repo, base, rows)? {
        if repo.ancestor(&change.head, base)? {
            return Err(format!("Anchor already contains source #{}", change.number));
        }
        let out = repo.output(&["merge-tree", "--write-tree", &tip, &change.head], None)?;
        if out.status.code() == Some(1) {
            return Ok(object([
                ("conflict_at", change.number.into()),
                ("tree", Value::Null),
            ]));
        }
        if !out.status.success() {
            return Err("Unable to reproduce native merge".into());
        }
        let tree = std::str::from_utf8(&out.stdout)
            .map_err(|e| e.to_string())?
            .lines()
            .next()
            .ok_or("Missing merge tree")?;
        tip = repo.commit(tree, &tip, &change.head, &change.message(), 0)?;
    }
    let tree = repo.text(&["rev-parse", &format!("{tip}^{{tree}}")], None)?;
    Ok(object([
        ("tip", Value::String(tip)),
        ("tree", Value::String(tree)),
    ]))
}

fn project(repo: &Repo, queue: &Value, sources: &[Value]) -> Result<Value, String> {
    let base = queue.field("base")?.string()?;
    let mut groups = BTreeMap::new();
    for group in queue.field("groups")?.array()? {
        groups.insert(group.field("number")?.number()?, group);
    }
    let mut members = BTreeMap::new();
    for row in sources {
        members.insert(
            row.field("number")?.number()?.to_string(),
            row.field("head")?.clone(),
        );
    }
    let prefix = queue.field("changes")?.array()?;
    let mut anchor = base;
    for row in prefix.iter().rev() {
        if let Some(group) = groups.get(&row.field("number")?.number()?) {
            if !matches!(
                group.field("state")?.string()?,
                "AWAITING_CHECKS" | "MERGEABLE" | "LOCKED"
            ) {
                continue;
            }
            anchor = group.field("head")?.string()?;
            if !repo.ancestor(base, anchor)?
                || !repo.ancestor(row.field("head")?.string()?, anchor)?
            {
                return Err("Invalid archived queue predecessor".into());
            }
            if group.field("tree")?.string()?
                != repo.text(&["rev-parse", &format!("{anchor}^{{tree}}")], None)?
            {
                return Err("Queue tree differs from the archived predecessor".into());
            }
            break;
        }
    }
    let mut outside = Vec::new();
    for row in prefix {
        if !repo.ancestor(row.field("head")?.string()?, anchor)? {
            outside.push(row.field("number")?.clone());
        }
    }
    let main = assemble(repo, base, sources)?;
    let from_queue = if anchor == base {
        main.clone()
    } else {
        assemble(repo, anchor, sources)?
    };
    Ok(object([
        ("main", base.into()),
        ("anchor", anchor.into()),
        ("sources", Value::Object(members)),
        ("preceding_prs_outside_anchor", Value::Array(outside)),
        ("from_main", main),
        ("from_queue", from_queue),
    ]))
}

fn preview(argv: &[String]) -> Result<Value, String> {
    let args = Args::parse(argv, &["--branch", "--out", "--repo"])?;
    let Some((repository, requested)) = args.positional.split_first() else {
        return Err("Expected a GitHub owner/repository".into());
    };
    if requested.is_empty() {
        return Err("Supply at least one source".into());
    }
    let mut seen = BTreeSet::new();
    let mut numbers = Vec::new();
    for raw in requested {
        let n = raw
            .parse::<u64>()
            .ok()
            .filter(|n| (1..(1 << 31)).contains(n))
            .ok_or("Invalid source PR number")?;
        if seen.insert(n) {
            numbers.push(n);
        }
    }
    let out = Path::new(args.required("--out")?);
    let repo = Repo::new(Path::new(args.required("--repo")?))?;
    if repo.text(&["rev-parse", "--is-bare-repository"], None)? != "true" {
        return Err("Use a dedicated bare clone".into());
    }
    let branch = args.optional("--branch").unwrap_or("main");
    let client = Client::new();
    let queue = collect::live(&client, repository, branch, None, None)?;
    let pulls = pull_many(&client, repository, &numbers)?;
    let mut rows = Vec::new();
    for n in &numbers {
        let pr = &pulls[n];
        if *pr == Value::Null
            || pr.field("state")?.string()? != "OPEN"
            || pr.field("baseRefName")?.string()? != branch
            || pr.field("isCrossRepository")?.boolean()?
            || pr
                .field("headRepository")?
                .get("nameWithOwner")
                .and_then(|v| v.string().ok())
                != Some(repository)
            || pr.field("isInMergeQueue")?.boolean()?
        {
            return Err(format!(
                "#{n} must be an open same-repository PR preceding queue admission"
            ));
        }
        rows.push(object([
            ("number", (*n).into()),
            ("head", pr.field("headRefOid")?.clone()),
            (
                "author",
                pr.field("author")?
                    .get("login")
                    .cloned()
                    .unwrap_or_else(|| "ghost".into()),
            ),
        ]));
    }
    for pr in queue.field("changes")?.array()? {
        if seen.contains(&pr.field("number")?.number()?) {
            return Err("A selected PR already entered the queue".into());
        }
    }
    get_inputs(&repo, repository, &queue, &rows)?;
    let prediction = project(&repo, &queue, &rows)?;
    let stamp = format!("{:.6}", jiff::Timestamp::now()).replace('Z', "+00:00");
    if collect::live(&client, repository, branch, None, None)? != queue {
        return Err("Queue changed while computing the prediction. Collect again.".into());
    }
    let current = pull_many(&client, repository, &numbers)?;
    for row in &rows {
        let pr = &current[&row.field("number")?.number()?];
        if *pr == Value::Null
            || pr.field("state")?.string()? != "OPEN"
            || pr.field("baseRefName")?.string()? != branch
            || pr.field("headRefOid")? != row.field("head")?
            || pr.field("isInMergeQueue")?.boolean()?
        {
            return Err("A source changed or entered the queue during prediction".into());
        }
    }
    let sources = object([
        ("repository", (*repository).into()),
        ("branch", branch.into()),
        ("base", queue.field("base")?.clone()),
        ("changes", Value::Array(rows)),
    ]);
    let result = object([
        ("computed_at", Value::String(stamp)),
        ("queue", queue),
        ("sources", sources),
        ("prediction", prediction.clone()),
    ]);
    files::replace(out, format!("{}\n", json::pretty(&result)).as_bytes())
        .map_err(|e| e.to_string())?;
    Ok(prediction)
}
