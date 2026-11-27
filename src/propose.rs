use crate::args::Args;
use crate::collect;
use crate::git::Repo;
use crate::github::Client;
use crate::json::{self, object, Value};
use crate::land::Receipt;
use crate::{files, snapshot};
use std::path::Path;
const PRIOR_QUERY: &str = r#"query($owner:String!,$name:String!,$base:String!,$head:String!){
  repository(owner:$owner,name:$name){pullRequests(baseRefName:$base,headRefName:$head,
    states:[OPEN,CLOSED,MERGED],first:100){nodes{number state headRefOid isCrossRepository url mergedAt}}}}"#;
const CREATE_QUERY: &str = r#"mutation($repo:ID!,$base:String!,$head:String!,$title:String!,$body:String!,$draft:Boolean!){
  createPullRequest(input:{repositoryId:$repo,baseRefName:$base,headRefName:$head,
    title:$title,body:$body,draft:$draft}){pullRequest{url}}}"#;

pub fn command(args: &[String]) -> i32 {
    if args
        .iter()
        .take_while(|s| s.as_str() != "--")
        .any(|s| matches!(s.as_str(), "-h" | "--help"))
    {
        print!("{}", include_str!("help/propose.txt"));
        return 0;
    }
    match propose(args) {
        Ok(url) => {
            println!("{url}");
            0
        }
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}

pub fn parse(arguments: &[String]) -> Result<Args<'_>, String> {
    let args = Args::parse(arguments, &["--repo", "--head", "--title", "--notes"])?;
    if args.positional.len() != 1 {
        return Err("Expected one batch receipt".into());
    }
    args.required("--repo")?;
    args.required("--head")?;
    Ok(args)
}

fn propose(arguments: &[String]) -> Result<String, String> {
    let args = parse(arguments)?;
    let path = Path::new(args.positional[0]);
    let receipt = Receipt::read(path)?;
    let repo = Repo::new(Path::new(args.required("--repo")?))?;
    let head = args.required("--head")?;
    let snap = &receipt.snapshot;
    let repository = snap.field("repository")?.string()?;
    let branch = snap.field("branch")?.string()?;
    let base = snap.field("base")?.string()?;
    let final_oid = &receipt.final_oid;
    repo.text(&["check-ref-format", "--branch", head], None)?;
    if head == branch || head.starts_with("refs/") || head.starts_with("gh-readonly-queue/") {
        return Err("Choose a new topic branch for the batch PR".into());
    }
    let client = Client::new();
    let selection = snap.get("selection");
    let live = collect::live(&client, repository, branch, selection, None)?;
    let target = live.field("base")?.string()?;
    let destination = crate::land::destination(repository)?;
    if target != base {
        repo.text(
            &[
                "fetch",
                "--no-tags",
                &destination,
                &format!("refs/heads/{branch}"),
            ],
            None,
        )?;
    }
    receipt.verify(&repo, path)?;
    let tree = repo.text(&["rev-parse", &format!("{final_oid}^{{tree}}")], None)?;
    if target != base {
        repo.text(&["merge-base", "--is-ancestor", base, target], None)?;
        let merged = repo.output(
            &[
                "merge-tree",
                "--write-tree",
                "--messages",
                "-z",
                target,
                final_oid,
            ],
            None,
        )?;
        if !merged.status.success()
            || merged.stdout.split(|b| *b == 0).next() != Some(tree.as_bytes())
        {
            return Err(
                "The current target does not merge to the tested tree; recollect and test".into(),
            );
        }
    }
    let queued = crate::land::heads(&live)?;
    let mut remaining = Vec::new();
    let mut included = Vec::new();
    for n in &receipt.tested {
        let oid = &receipt.heads[n];
        repo.text(&["merge-base", "--is-ancestor", oid, final_oid], None)?;
        if let Some(live_head) = queued.get(n) {
            if live_head != oid {
                return Err(format!("PR #{n} changed revision; recollect and test"));
            }
            remaining.push(format!("#{n}"));
        } else if repo
            .output(&["merge-base", "--is-ancestor", oid, target], None)?
            .status
            .success()
        {
            included.push(format!("#{n}"));
        } else {
            let reason = if selection.is_some() {
                "no longer matches the collected selection"
            } else {
                "left the merge queue"
            };
            return Err(format!("PR #{n} {reason}; recollect and test"));
        }
    }
    if remaining.is_empty()
        || tree == repo.text(&["rev-parse", &format!("{target}^{{tree}}")], None)?
    {
        return Err("The target already contains the batch".into());
    }
    let reference = format!("refs/heads/{head}");
    repo.text(&["check-ref-format", &reference], None)?;
    let remote = repo.text(&["ls-remote", "--heads", &destination, &reference], None)?;
    if remote
        .split_whitespace()
        .next()
        .is_some_and(|oid| oid != final_oid)
    {
        return Err("That topic branch exists at another revision".into());
    }
    let previous = prior(&client, repository, branch, head)?;
    let mut used = false;
    for pr in previous {
        if !pr.field("isCrossRepository")?.boolean()? {
            used = true;
            if pr.field("state")?.string()? == "OPEN"
                && pr.field("headRefOid")?.string()? == final_oid
            {
                return Ok(pr.field("url")?.string()?.into());
            }
        }
    }
    if used {
        return Err("A previous batch PR already used this branch".into());
    }
    if let Some(message) = matching_group(&repo, &receipt, &live, &tree, &destination)? {
        return Ok(message);
    }
    let numbers = remaining.join(", ");
    let route = if selection.is_some() {
        "for review"
    } else {
        "for review and validation through the protected merge queue"
    };
    let mut body=format!("lit combined {numbers} {route}. Each source PR head is a parent in the candidate history.\n\nLocal validation passed on commit `{final_oid}` and tree `{tree}`.\n");
    if !included.is_empty() {
        body.push_str(&format!(
            "\nThe target already contains {}.\n",
            included.join(", ")
        ));
    }
    if let Some(notes) = args.optional("--notes") {
        let raw = files::read(Path::new(notes))?;
        let text = std::str::from_utf8(&raw)
            .map_err(|e| e.to_string())?
            .replace("\r\n", "\n")
            .replace('\r', "\n");
        body.push_str(&format!("\n{}\n", text.trim()));
    }
    if remote.is_empty() {
        repo.text(
            &[
                "push",
                &format!("--force-with-lease={reference}:"),
                &destination,
                &format!("{final_oid}:{reference}"),
            ],
            None,
        )?;
    }
    let default_title = format!("lit batch for {numbers}");
    let title = args
        .optional("--title")
        .filter(|s| !s.is_empty())
        .unwrap_or(&default_title);
    create(&client, repository, branch, head, title, &body, true)
}

fn matching_group(
    repo: &Repo,
    receipt: &Receipt,
    live: &Value,
    tree: &str,
    destination: &str,
) -> Result<Option<String>, String> {
    let repository = live.field("repository")?.string()?;
    for group in live.field("groups")?.array()? {
        let state = group.field("state")?.string()?;
        if group.field("tree")?.string()? != tree
            || !matches!(state, "AWAITING_CHECKS" | "MERGEABLE" | "LOCKED")
        {
            continue;
        }
        let head = group.field("head")?.string()?;
        if !snapshot::valid_oid(head) {
            return Err("Expected full commit IDs from GitHub".into());
        }
        if !repo
            .output(&["cat-file", "-e", &format!("{head}^{{commit}}")], None)?
            .status
            .success()
            && !repo
                .output(&["fetch", "--no-tags", destination, head], None)?
                .status
                .success()
        {
            continue;
        }
        if repo.text(&["rev-parse", &format!("{head}^{{tree}}")], None)? != tree {
            continue;
        }
        let mut covered = true;
        for oid in std::iter::once(live.field("base")?.string()?)
            .chain(receipt.tested.iter().map(|n| receipt.heads[n].as_str()))
        {
            if !repo
                .output(&["merge-base", "--is-ancestor", oid, head], None)?
                .status
                .success()
            {
                covered = false;
                break;
            }
        }
        if covered {
            let n = group.field("number")?.number()?;
            return Ok(Some(format!("GitHub already has a matching merge group for PR #{n} ({state}). https://github.com/{repository}/commit/{head}")));
        }
    }
    Ok(None)
}

pub(crate) fn encode(s: &str) -> String {
    s.bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}

pub(crate) fn prior(
    client: &Client,
    repository: &str,
    branch: &str,
    head: &str,
) -> Result<Vec<Value>, String> {
    let (owner, name) = collect::repository_parts(repository)?;
    let api = std::env::var("LIT_GITHUB_API").unwrap_or_else(|_| "auto".into());
    if api != "rest" {
        match client.graphql(
            PRIOR_QUERY,
            object([
                ("owner", owner.into()),
                ("name", name.into()),
                ("base", branch.into()),
                ("head", head.into()),
            ]),
        ) {
            Ok(data) => {
                return Ok(data
                    .field("data")?
                    .field("repository")?
                    .field("pullRequests")?
                    .field("nodes")?
                    .array()?
                    .to_vec())
            }
            Err(e) if api == "graphql" => return Err(e),
            Err(_) => eprintln!("lit: GitHub GraphQL refused; reading PRs over REST"),
        }
    }
    let pulls = client.get(&format!(
        "repos/{repository}/pulls?head={}&base={}&state=all&per_page=100",
        encode(&format!("{owner}:{head}")),
        encode(branch)
    ))?;
    let mut found = Vec::new();
    for pr in pulls.array()? {
        let source = pr.field("head")?;
        if source.field("ref")?.string()? != head {
            continue;
        }
        let same = source
            .get("repo")
            .and_then(|r| r.get("full_name"))
            .and_then(|v| v.string().ok())
            == Some(repository);
        let state = if matches!(pr.get("merged_at"), Some(Value::String(_))) {
            "MERGED".into()
        } else {
            pr.field("state")?.string()?.to_uppercase()
        };
        found.push(object([
            ("number", pr.get("number").cloned().unwrap_or(Value::Null)),
            ("isCrossRepository", Value::Bool(!same)),
            (
                "mergedAt",
                pr.get("merged_at").cloned().unwrap_or(Value::Null),
            ),
            ("headRefOid", source.field("sha")?.clone()),
            ("state", Value::String(state)),
            ("url", pr.field("html_url")?.clone()),
        ]));
    }
    Ok(found)
}

pub(crate) fn create(
    client: &Client,
    repository: &str,
    branch: &str,
    head: &str,
    title: &str,
    body: &str,
    draft: bool,
) -> Result<String, String> {
    let payload = object([
        ("base", branch.into()),
        ("head", head.into()),
        ("title", title.into()),
        ("body", body.into()),
        ("draft", Value::Bool(draft)),
    ]);
    if let Some(target) = std::env::var_os("LIT_PARITY_LAND_TARGET") {
        // The parity harness models the created PR beside the private Git ref.
        let path = Path::new(&target).join("lit-draft.json");
        files::replace(&path, json::pretty(&payload).as_bytes()).map_err(|e| e.to_string())?;
        return Ok(format!("https://github.com/{repository}/pull/parity"));
    }
    let api = std::env::var("LIT_GITHUB_API").unwrap_or_else(|_| "auto".into());
    if api != "rest" {
        let (owner, name) = collect::repository_parts(repository)?;
        let result = (|| {
            let data = client.graphql(
                "query($o:String!,$n:String!){repository(owner:$o,name:$n){id}}",
                object([("o", owner.into()), ("n", name.into())]),
            )?;
            let id = data
                .field("data")?
                .field("repository")?
                .field("id")?
                .clone();
            let made = client.graphql(
                CREATE_QUERY,
                object([
                    ("repo", id),
                    ("base", branch.into()),
                    ("head", head.into()),
                    ("title", title.into()),
                    ("body", body.into()),
                    ("draft", Value::Bool(draft)),
                ]),
            )?;
            Ok::<String, String>(
                made.field("data")?
                    .field("createPullRequest")?
                    .field("pullRequest")?
                    .field("url")?
                    .string()?
                    .into(),
            )
        })();
        match result {
            Ok(url) => return Ok(url),
            Err(e) if api == "graphql" => return Err(e),
            Err(_) => eprintln!("lit: GitHub GraphQL refused; opening the PR over REST"),
        }
    }
    Ok(client
        .post(&format!("repos/{repository}/pulls"), payload)?
        .field("html_url")?
        .string()?
        .into())
}
