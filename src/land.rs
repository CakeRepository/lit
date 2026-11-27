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

pub fn heads(snapshot: &Value) -> Result<BTreeMap<u64, String>, String> {
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
        .take_while(|a| !matches!(a.as_str(), "--" | "--recheck"))
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
    let split = arguments.iter().position(|arg| arg == "--recheck");
    let (path, git_dir) = inputs(&arguments[..split.unwrap_or(arguments.len())])?;
    let mut recheck = split.map(|i| &arguments[i + 1..]);
    let mut receipt = Receipt::read(path)?;
    let repo = Repo::new(git_dir)?;
    receipt.verify(&repo, path)?;
    if let Some(command) = recheck {
        let raw = files::read(path)?;
        let row = json::parse(std::str::from_utf8(&raw).map_err(|e| e.to_string())?)?;
        let expected = row
            .field("command")?
            .array()?
            .iter()
            .map(Value::string)
            .collect::<Result<Vec<_>, _>>()?;
        if command.is_empty()
            || !expected
                .iter()
                .copied()
                .eq(command.iter().map(String::as_str))
        {
            return Err("Supply the same trusted validation command used by run".into());
        }
    }
    let client = Client::new();
    let mut push_raced = false;
    loop {
        let snap = &receipt.snapshot;
        let repository = snap.field("repository")?.string()?;
        let branch = snap.field("branch")?.string()?;
        let base = snap.field("base")?.string()?;
        let live = collect::live(&client, repository, branch, snap.get("selection"), None)?;
        let ready = heads(&live)?;
        if receipt
            .tested
            .iter()
            .any(|n| ready.get(n) != receipt.heads.get(n))
        {
            return Err("Base or queue eligibility changed; collect and test again".into());
        }
        let destination = destination(repository)?;
        let fresh = live.field("base")?.string()?;
        if fresh != base {
            let cmd = recheck
                .take()
                .ok_or("Base or queue eligibility changed; collect and test again")?;
            if !valid_oid(fresh) {
                return Err("Invalid live base".into());
            }
            repo.text(&["fetch", "--no-tags", &destination, fresh], None)?;
            if !repo.ancestor(base, fresh)? {
                return Err("Landing branch was rewritten; collect and test again".into());
            }
            let updated = crate::run::recheck(&repo, path, &live, &receipt.tested, cmd)?;
            receipt = Receipt::read(&updated)?;
            receipt.verify(&repo, &updated)?;
            continue; // Re-read eligibility after validation, immediately before the next lease.
        }
        let reference = format!("refs/heads/{branch}");
        repo.text(&["check-ref-format", &reference], None)?;
        let output = repo.output(
            &[
                "push",
                "--porcelain",
                &format!("--force-with-lease={reference}:{base}"),
                &destination,
                &format!("{}:{reference}", receipt.final_oid),
            ],
            None,
        )?;
        if output.status.success() {
            return Ok(receipt.final_oid);
        }
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("stale info")
            || String::from_utf8_lossy(&output.stdout).contains("stale info")
        {
            if recheck.is_some() && !push_raced {
                push_raced = true;
                continue;
            }
            return Err(format!(
                "{reference} moved since the batch was tested on {base}; collect and test again"
            ));
        }
        return Err(format!("git push: {}", stderr.trim()));
    }
}

pub fn destination(repository: &str) -> Result<String, String> {
    Ok(match std::env::var_os("LIT_PARITY_LAND_TARGET") {
        Some(value) => {
            let target = Path::new(&value);
            if std::env::var("GIT_ALLOW_PROTOCOL").as_deref() != Ok("file")
                || !target.is_absolute()
                || !target.is_dir()
            {
                return Err("Expected an isolated local landing target".into());
            }
            target
                .to_str()
                .ok_or("Expected a UTF-8 landing target")?
                .to_owned()
        }
        None => format!("https://github.com/{repository}.git"),
    })
}
