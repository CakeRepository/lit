//! Advance release-stage refs and their audit histories in an atomic Git ref transaction.
use crate::args::Args;
use crate::git::Repo;
use crate::github::Client;
use crate::json::{self, object, Value};
use crate::snapshot::valid_oid;
use std::collections::BTreeSet;
use std::path::Path;
mod evidence;

type Result<T> = std::result::Result<T, String>;
const MAIN: &str = "refs/heads/main";

struct Stage {
    reference: String,
    journal: String,
    tip: String,
    history: Vec<Value>,
}

impl Stage {
    fn read(repo: &Repo, name: &str, zero: &str) -> Result<Self> {
        let reference = format!("refs/lit/stages/{name}");
        let journal = format!("refs/lit/stage-history/{name}");
        let out = repo.output(&["rev-parse", "--verify", "--quiet", &journal], None)?;
        let (tip, history) = match out.status.code() {
            Some(0) => {
                let tip = String::from_utf8(out.stdout)
                    .map_err(|e| e.to_string())?
                    .trim()
                    .to_owned();
                let row = json::parse(&repo.text(&["show", "-s", "--format=%B", &tip], None)?)?;
                let history = row.field("history")?.array()?.to_vec();
                if history.is_empty() || !history.iter().all(|v| v.string().is_ok_and(valid_oid)) {
                    return Err("Invalid stage journal".into());
                }
                (tip, history)
            }
            Some(1) => (zero.into(), vec![]),
            _ => return Err("Cannot read stage journal".into()),
        };
        Ok(Self {
            reference,
            journal,
            tip,
            history,
        })
    }
    fn current<'a>(&'a self, zero: &'a str) -> &'a str {
        self.history
            .last()
            .and_then(|v| v.string().ok())
            .unwrap_or(zero)
    }
}

fn policies(config: &Value) -> Result<&[Value]> {
    let mut names = BTreeSet::new();
    let stages = config.field("stages")?.array()?;
    for stage in stages {
        let name = stage.field("name")?.string()?;
        let workflow = stage.field("workflow")?.string()?;
        let checks = stage.field("checks")?.array()?;
        let env = stage.field("environment")?;
        let mut unique = BTreeSet::new();
        if name.is_empty()
            || name == "main"
            || !name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c))
            || !names.insert(name.to_ascii_lowercase())
            || stage.object()?.len() != 4
            || !workflow
                .strip_prefix(".github/workflows/")
                .is_some_and(|p| !p.contains('/') && (p.ends_with(".yml") || p.ends_with(".yaml")))
            || checks.is_empty()
            || !checks
                .iter()
                .all(|v| v.string().is_ok_and(|s| !s.is_empty() && unique.insert(s)))
            || (*env != Value::Null && !env.string().is_ok_and(|s| !s.is_empty()))
        {
            return Err("Invalid or duplicate stage policy".into());
        }
    }
    Ok(stages)
}

fn transition(argv: &[String]) -> Result<String> {
    let args = Args::parse(argv, &["--repo", "--expected", "--run"])?;
    let repo = Repo::new(Path::new(args.required("--repo")?))?;
    if repo.text(&["rev-parse", "--is-bare-repository"], None)? != "true" {
        return Err("Use a trusted bare stage repository".into());
    }
    let main = repo.text(&["rev-parse", "--verify", MAIN], None)?;
    let zero = "0".repeat(main.len());
    let config = json::parse(&repo.text(&["show", &format!("{main}:.lit/stages.json")], None)?)?;
    let repository = config.field("repository")?.string()?;
    crate::collect::repository_parts(repository)?;
    let stages = policies(&config)?;
    let (verb, name, snapshot) = match args.positional.as_slice() {
        ["promote", name, sha] if valid_oid(sha) => ("promote", *name, Some(*sha)),
        ["rollback", name] if !args.has("--run") => ("rollback", *name, None),
        _ => return Err("Expected stage promote NAME FULL_SHA or stage rollback NAME".into()),
    };
    let index = stages
        .iter()
        .position(|s| s.get("name").and_then(|v| v.string().ok()) == Some(name))
        .ok_or("Unknown stage")?;
    let expected = match args.required("--expected")? {
        "empty" => zero.as_str(),
        sha if valid_oid(sha) => sha,
        _ => return Err("Expected a full stage commit or empty".into()),
    };
    let mut stage = Stage::read(&repo, name, &zero)?;
    if stage.current(&zero) != expected {
        return Err("Stage moved".into());
    }
    let client = Client::new();
    let remote = client.get(&format!("repos/{repository}/git/ref/heads/main"))?;
    if remote.field("object")?.field("sha")?.string()? != main {
        return Err("Refresh the trusted main ref".into());
    }
    let mut transaction = format!("verify {MAIN} {main}\n");
    let (next, source, proof) = if let Some(sha) = snapshot {
        let source = if index == 0 {
            // Side parents of a landed merge never held the main ref.
            if !repo
                .text(&["rev-list", "--first-parent", &main], None)?
                .lines()
                .any(|oid| oid == sha)
            {
                return Err("Snapshot never held main".into());
            }
            "main"
        } else {
            let source = stages[index - 1].field("name")?.string()?;
            let prev = Stage::read(&repo, source, &zero)?;
            if !prev.history.iter().any(|v| v.string() == Ok(sha)) {
                return Err("Snapshot has not reached its source stage".into());
            }
            transaction.push_str(&format!(
                "verify {} {}\nverify {} {}\n",
                prev.reference,
                prev.current(&zero),
                prev.journal,
                prev.tip
            ));
            source
        };
        if stage.current(&zero) == sha {
            return Err("Stage already holds this snapshot".into());
        }
        let id = args
            .required("--run")?
            .parse::<u64>()
            .map_err(|_| "Invalid workflow run ID")?;
        let proof = evidence::read(&client, repository, sha, &stages[index], id)?;
        stage.history.push(sha.into());
        (sha, source, proof)
    } else {
        if stage.history.len() < 2 {
            return Err("No previous stage snapshot".into());
        }
        stage.history.pop();
        (stage.current(&zero), name, Value::Null)
    };
    let record = object([
        ("operation", verb.into()),
        ("stage", name.into()),
        ("source", source.into()),
        ("from", expected.into()),
        ("snapshot", next.into()),
        ("policy_main", main.as_str().into()),
        ("evidence", proof),
        ("history", Value::Array(stage.history.clone())),
    ]);
    let empty_tree = repo.text(&["mktree"], Some(b""))?;
    let mut commit = vec![
        "-c",
        "user.name=lit",
        "-c",
        "user.email=stage@localhost",
        "commit-tree",
        &empty_tree,
    ];
    if stage.tip != zero {
        commit.extend(["-p", stage.tip.as_str()]);
    }
    let journal = repo.text(&commit, Some(json::pretty(&record).as_bytes()))?;
    transaction.push_str(&format!(
        "update {} {next} {expected}\nupdate {} {journal} {}\n",
        stage.reference, stage.journal, stage.tip
    ));
    repo.text(
        &["update-ref", "--no-deref", "--stdin"],
        Some(transaction.as_bytes()),
    )?;
    Ok(next.into())
}

pub fn command(argv: &[String]) -> i32 {
    if argv.iter().any(|s| matches!(s.as_str(), "-h" | "--help")) {
        print!("{}", include_str!("help/stage.txt"));
        return 0;
    }
    match transition(argv) {
        Ok(sha) => {
            println!("{sha}");
            0
        }
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}
