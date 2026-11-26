use crate::args::Args;
use crate::collect;
use crate::files;
use crate::git::Repo;
use crate::github::Client;
use crate::json::{self, Value};
use crate::snapshot::valid_oid;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

pub fn inputs(arguments: &[String]) -> Result<(&Path, &Path), String> {
    let args = Args::parse(arguments, &["--repo"])?;
    let [receipt] = args.positional[..] else {
        return Err("Expected one batch receipt".into());
    };
    Ok((Path::new(receipt), Path::new(args.required("--repo")?)))
}

pub struct Receipt {
    pub snapshot: Value,
    pub final_oid: String,
    pub tested: BTreeSet<u64>,
    pub heads: BTreeMap<u64, String>,
}

impl Receipt {
    pub fn read(path: &Path) -> Result<Self, String> {
        let raw = files::read(path)?;
        let row = json::parse(std::str::from_utf8(&raw).map_err(|e| e.to_string())?)?;
        let snapshot = row.field("snapshot")?.clone();
        let final_oid = row.field("final")?.string()?.to_owned();
        let tested: BTreeSet<_> = row
            .field("tested")?
            .array()?
            .iter()
            .map(Value::number)
            .collect::<Result<_, _>>()?;
        let heads = heads(&snapshot)?;
        if tested.is_empty()
            || !tested.iter().all(|n| heads.contains_key(n))
            || !valid_oid(&final_oid)
            || !valid_oid(snapshot.field("base")?.string()?)
        {
            return Err("No matching tested batch ref".into());
        }
        Ok(Self {
            snapshot,
            final_oid,
            tested,
            heads,
        })
    }

    pub fn verify(&self, repo: &Repo, path: &Path) -> Result<(), String> {
        let batch = path
            .parent()
            .and_then(Path::file_name)
            .and_then(|s| s.to_str())
            .unwrap_or("");
        let reference = format!("refs/concurrent-merge/tested/{batch}");
        if repo.text(&["rev-parse", &reference], None)? != self.final_oid {
            return Err("No matching tested batch ref".into());
        }
        repo.text(
            &[
                "merge-base",
                "--is-ancestor",
                self.snapshot.field("base")?.string()?,
                &self.final_oid,
            ],
            None,
        )?;
        Ok(())
    }
}

fn heads(snapshot: &Value) -> Result<BTreeMap<u64, String>, String> {
    let mut heads = BTreeMap::new();
    for row in snapshot.field("changes")?.array()? {
        let n = row.field("number")?.number()?;
        let head = row.field("head")?.string()?;
        if n == 0 || !valid_oid(head) || heads.insert(n, head.to_owned()).is_some() {
            return Err("Invalid or duplicate PR in snapshot".into());
        }
    }
    Ok(heads)
}

pub fn command(args: &[String]) -> i32 {
    if args
        .iter()
        .take_while(|a| a.as_str() != "--")
        .any(|s| matches!(s.as_str(), "-h" | "--help"))
    {
        print!("{}", include_str!("help/land.txt"));
        return 0;
    }
    match land(args) {
        Ok(oid) => {
            println!("{oid}");
            0
        }
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}

fn land(arguments: &[String]) -> Result<String, String> {
    let (path, git_dir) = inputs(arguments)?;
    let receipt = Receipt::read(path)?;
    let repo = Repo::new(git_dir)?;
    receipt.verify(&repo, path)?;
    let snap = &receipt.snapshot;
    let repository = snap.field("repository")?.string()?;
    let branch = snap.field("branch")?.string()?;
    let base = snap.field("base")?.string()?;
    let live = collect::live(
        &Client::new(),
        repository,
        branch,
        snap.get("selection"),
        None,
    )?;
    let ready = heads(&live)?;
    if live.field("base")?.string()? != base
        || receipt
            .tested
            .iter()
            .any(|n| ready.get(n) != receipt.heads.get(n))
    {
        return Err("Base or queue eligibility changed; collect and test again".into());
    }
    let reference = format!("refs/heads/{branch}");
    repo.text(&["check-ref-format", &reference], None)?;
    let output = repo.output(
        &[
            "push",
            "--porcelain",
            &format!("--force-with-lease={reference}:{base}"),
            &format!("https://github.com/{repository}.git"),
            &format!("{}:{reference}", receipt.final_oid),
        ],
        None,
    )?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("stale info")
            || String::from_utf8_lossy(&output.stdout).contains("stale info")
        {
            return Err(format!(
                "{reference} moved since the batch was tested on {base}; collect and test again"
            ));
        }
        return Err(format!("git push: {}", stderr.trim()));
    }
    Ok(receipt.final_oid)
}
