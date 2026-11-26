use crate::args::Args;
use crate::git::Repo;
use crate::json::{self, Value};
use crate::{files, sha256};
use std::path::Path;

const USAGE: &str = "usage: concurrent-merge agree [-h] --state STATE --repo REPO
                              conflict participant file";
const HELP: &str = "
positional arguments:
  conflict
  participant
  file

options:
  -h, --help     show this help message and exit
  --state STATE
  --repo REPO";

pub fn command(arguments: &[String]) -> i32 {
    if arguments
        .iter()
        .take_while(|a| a.as_str() != "--")
        .any(|a| matches!(a.as_str(), "-h" | "--help"))
    {
        println!("{USAGE}\n{HELP}");
        return 0;
    }
    let inputs = Args::parse(arguments, &["--state", "--repo"]).and_then(|args| {
        let names = ["conflict", "participant", "file"];
        let mut missing = names[args.positional.len().min(3)..].to_vec();
        missing.extend(
            ["--state", "--repo"]
                .into_iter()
                .filter(|k| args.required(k).is_err()),
        );
        if !missing.is_empty() {
            return Err(format!(
                "the following arguments are required: {}",
                missing.join(", ")
            ));
        }
        let [key, author, file] = args.positional[..] else {
            return Err(format!(
                "unrecognized arguments: {}",
                args.positional[3..].join(" ")
            ));
        };
        Ok((
            key,
            author,
            Path::new(file),
            Path::new(args.required("--state")?),
            Path::new(args.required("--repo")?),
        ))
    });
    let (key, author, file, state, repo) = match inputs {
        Ok(value) => value,
        Err(e) => {
            eprintln!("{USAGE}\nconcurrent-merge agree: error: {e}");
            return 2;
        }
    };
    match record(key, author, file, state, repo) {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}

fn record(key: &str, author: &str, file: &Path, state: &Path, repo: &Path) -> Result<(), String> {
    let contents = files::read(file)?;
    if key.len() != 64
        || !key
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("Invalid conflict id".into());
    }
    let raw = files::read(&state.join("conflicts").join(format!("{key}.json")))?;
    let Value::Object(issue) = json::parse(std::str::from_utf8(&raw).map_err(|e| e.to_string())?)?
    else {
        return Err("Expected a conflict record".into());
    };
    let listed = matches!(issue.get("authors"), Some(Value::Array(authors)) if authors.iter().any(|a| matches!(a, Value::String(name) if name == author)));
    let regular = matches!(issue.get("mode"), Some(Value::String(mode)) if matches!(mode.as_str(), "100644" | "100755"));
    if !regular || !listed {
        return Err(
            "This vote requires a listed author and a regular file content conflict".into(),
        );
    }

    let repo = Repo::new(repo)?;
    let blob = repo.text(&["hash-object", "-w", "--stdin"], Some(&contents))?;
    repo.text(
        &[
            "update-ref",
            &format!("refs/concurrent-merge/votes/{key}/{blob}"),
            &blob,
        ],
        None,
    )?;
    let vote = Value::Object(
        [
            ("author".into(), Value::String(author.into())),
            ("blob".into(), Value::String(blob)),
        ]
        .into(),
    );
    let path = state
        .join("votes")
        .join(key)
        .join(format!("{}.json", sha256::hex(author.as_bytes())));
    files::replace(&path, format!("{}\n", json::pretty(&vote)).as_bytes())
        .map_err(|e| e.to_string())
}
