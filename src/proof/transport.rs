//! Store signed proofs and audit books in Git, using leases on published refs.
use super::*;
const PREFIX: &str = "refs/heads/lit/proof/";
const BOOK_REF: &str = "refs/heads/lit/audits";

fn envelope(
    repo: &Repo,
    tree: &str,
    msg: &str,
    parent: Option<&str>,
    date: Option<&str>,
) -> Result<String> {
    let mut cmd = vec!["commit-tree", tree, "-m", msg];
    if let Some(p) = parent {
        cmd.extend(["-p", p]);
    }
    let mut env = vec![
        ("GIT_AUTHOR_NAME", "lit"),
        ("GIT_AUTHOR_EMAIL", "lit@localhost"),
        ("GIT_COMMITTER_NAME", "lit"),
        ("GIT_COMMITTER_EMAIL", "lit@localhost"),
    ];
    if let Some(d) = date {
        env.extend([("GIT_AUTHOR_DATE", d), ("GIT_COMMITTER_DATE", d)]);
    }
    let out = repo.output_env(&cmd, None, &env)?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().into());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().into())
}
pub fn push(proof: &Path, repo: &Repo, args: &Args<'_>) -> Result<String> {
    let content = files::read(proof)?;
    let p = payload::unpacked(&content)?;
    let commit = p.field("commit")?.string()?;
    if !valid_oid(commit) {
        return Err("Invalid proof commit".into());
    }
    let sig = files::read(&payload::signature(proof))?;
    let id = crate::sha256::hex(&sig);
    let prefix = args.optional("--prefix").unwrap_or(PREFIX);
    let reference = format!("{prefix}{commit}/{}", &id[..16]);
    repo.text(&["check-ref-format", &reference], None)?;
    let blob = repo.text(&["hash-object", "-w", "--stdin"], Some(&content))?;
    let sig_id = repo.text(&["hash-object", "-w", "--stdin"], Some(&sig))?;
    let records =
        format!("100644 blob {blob}\tproof.json.gz\n100644 blob {sig_id}\tproof.json.gz.sig\n");
    let tree = repo.text(&["mktree"], Some(records.as_bytes()))?;
    let date = format!("@{} +0000", payload::time(&p, "created")?.as_second());
    let made = envelope(
        repo,
        &tree,
        &format!("lit proof for {commit}"),
        None,
        Some(&date),
    )?;
    let remote = args.required("--remote")?;
    if let Err(failure) = repo.text(
        &[
            "push",
            "--quiet",
            &format!("--force-with-lease={reference}:"),
            "--",
            remote,
            &format!("{made}:{reference}"),
        ],
        None,
    ) {
        let listed = repo.text(&["ls-remote", "--refs", "--", remote, &reference], None)?;
        let v: Vec<_> = listed.split_whitespace().collect();
        if v.len() != 2 || v[1] != reference || !valid_oid(v[0]) {
            return Err(failure);
        }
        repo.text(
            &[
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--",
                remote,
                &reference,
            ],
            None,
        )?;
        if repo.text(
            &["rev-parse", "--verify", &format!("{}^{{tree}}", v[0])],
            None,
        )? != tree
        {
            return Err(failure);
        }
    }
    Ok(reference)
}
fn load_book(path: &Path) -> Value {
    if let Ok(book) = paths::read(path) {
        if field(&book, "kind") == Some("lit-audit-book") {
            return book;
        }
    }
    object([
        ("kind", "lit-audit-book".into()),
        ("version", 1u64.into()),
        ("signers", Value::Object(Map::new())),
        ("parent", Value::Null),
    ])
}
pub fn book(args: &Args<'_>, repo: &Repo, pull: bool) -> Result<String> {
    let path = Path::new(args.positional[0]);
    let remote = args.required("--remote")?;
    let reference = args.optional("--ref").unwrap_or(BOOK_REF);
    repo.text(&["check-ref-format", reference], None)?;
    if pull {
        let listing = repo.text(&["ls-remote", "--", remote, reference], None)?;
        let (signers, parent) = if let Some(oid) = listing.split_whitespace().next() {
            if !valid_oid(oid) {
                return Err("Invalid audit book ref".into());
            }
            repo.text(
                &[
                    "fetch",
                    "-q",
                    "--",
                    remote,
                    &format!("+{reference}:refs/lit-book/fetched"),
                ],
                None,
            )?;
            let book = paths::parse(
                &repo.bytes(&["cat-file", "blob", &format!("{oid}:book.json")], None)?,
            )?;
            (
                book.get("signers")
                    .cloned()
                    .unwrap_or_else(|| Value::Object(Map::new())),
                oid.into(),
            )
        } else {
            (Value::Object(Map::new()), Value::Null)
        };
        save(
            path,
            &object([
                ("kind", "lit-audit-book".into()),
                ("version", 1u64.into()),
                ("signers", signers),
                ("parent", parent),
            ]),
        )?;
        return Ok(path.display().to_string());
    }
    let book = load_book(path);
    let parent = field(&book, "parent");
    if parent.is_some_and(|p| !valid_oid(p)) {
        return Err("Invalid audit book parent".into());
    }
    let signers = book
        .get("signers")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let book = object([
        ("kind", "lit-audit-book".into()),
        ("version", 1u64.into()),
        ("signers", signers),
    ]);
    let id = repo.text(
        &["hash-object", "-w", "--stdin"],
        Some(paths::sorted(&book, true).as_bytes()),
    )?;
    let tree = repo.text(
        &["mktree"],
        Some(format!("100644 blob {id}\tbook.json\n").as_bytes()),
    )?;
    let date = std::env::var("LIT_PARITY_BOOK_DATE").ok();
    let made = envelope(repo, &tree, "lit audit book", parent, date.as_deref())?;
    repo.text(
        &[
            "push",
            "-q",
            &format!("--force-with-lease={reference}:{}", parent.unwrap_or("")),
            "--",
            remote,
            &format!("{made}:{reference}"),
        ],
        None,
    )?;
    let Value::Object(mut book) = book else {
        unreachable!()
    };
    book.insert("parent".into(), made.as_str().into());
    save(path, &Value::Object(book))?;
    Ok(made)
}
fn agreement(bundle: &Value, name: &str) -> Result<String> {
    let unit = bundle.field("units")?.field(name)?;
    let inputs = |names: &str, table: &str| -> Result<Value> {
        let mut map = Map::new();
        if let Some(paths) = unit.get(names) {
            for path in paths.array()? {
                let p = path.string()?;
                map.insert(
                    p.to_owned(),
                    bundle
                        .get(table)
                        .and_then(|m| m.get(p))
                        .cloned()
                        .unwrap_or(Value::Null),
                );
            }
        }
        Ok(Value::Object(map))
    };
    let mut absent = Vec::new();
    if let Some(Value::Array(a)) = unit.get("absent") {
        for v in a {
            absent.push(v.string()?.to_owned());
        }
    }
    absent.sort_by(|a, b| paths::compare(a, b));
    let reads = object([
        ("tracked", inputs("tracked", "tracked")?),
        ("listings", inputs("listings", "listing_children")?),
        (
            "absent",
            Value::Array(absent.into_iter().map(Value::String).collect()),
        ),
        ("exit_code", unit.field("exit_code")?.clone()),
    ]);
    Ok(crate::sha256::hex(paths::sorted(&reads, false).as_bytes()))
}
struct Vote {
    signers: BTreeSet<String>,
    bundle: Value,
    symbols: Option<Value>,
}
pub fn take(
    args: &Args<'_>,
    repo: &Repo,
    policy: &payload::Policy<'_>,
    store: &Path,
) -> Result<()> {
    let quorum = args
        .optional("--quorum")
        .unwrap_or("1")
        .parse::<i64>()
        .map_err(|_| "Invalid proof quorum")?;
    let book = args
        .optional("--audit-book")
        .map(|p| load_book(Path::new(p)));
    let remote = args.required("--remote")?;
    let prefix = args.optional("--prefix").unwrap_or(PREFIX);
    let patterns: Vec<_> = policy
        .commits
        .iter()
        .filter(|c| valid_oid(c))
        .map(|c| format!("{prefix}{c}/*"))
        .collect();
    let mut query = vec!["ls-remote", "--", remote];
    query.extend(patterns.iter().map(String::as_str));
    let listing = if patterns.is_empty() {
        String::new()
    } else {
        repo.text(&query, None)?
    };
    let scratch = tempfile::tempdir().map_err(|e| e.to_string())?;
    let mut accepted = Vec::new();
    let mut votes = BTreeMap::<(String, String, String), Vote>::new();
    for line in listing.lines() {
        let mut words = line.split_whitespace();
        let (Some(oid), Some(reference)) = (words.next(), words.next()) else {
            continue;
        };
        let Some((commit, tag)) = reference
            .strip_prefix(prefix)
            .and_then(|r| r.split_once('/'))
        else {
            continue;
        };
        if !valid_oid(oid)
            || !valid_oid(commit)
            || !policy.commits.contains(&commit)
            || tag.len() != 16
            || !tag
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            continue;
        }
        repo.text(
            &["fetch", "--quiet", "--no-tags", "--", remote, reference],
            None,
        )?;
        let p = scratch.path().join("proof.json.gz");
        let sig = payload::signature(&p);
        let mut complete = true;
        for (name, path) in [("proof.json.gz", &p), ("proof.json.gz.sig", &sig)] {
            let Ok(bytes) = repo.bytes(&["cat-file", "blob", &format!("{oid}:{name}")], None)
            else {
                complete = false;
                break;
            };
            files::replace(path, &bytes).map_err(|e| files::error(path, e))?;
        }
        if !complete {
            continue;
        }
        let verify = payload::verify(&p, &sig, policy).and_then(|proof| {
            if let Some(row) = book
                .as_ref()
                .and_then(|b| b.get("signers"))
                .and_then(|s| s.get(&paths::path_key(proof.signer.as_bytes())))
            {
                if row.get("reset").is_some_and(Value::truthy)
                    && payload::time(&proof.payload, "created")? <= payload::time(row, "reset")?
                {
                    return Err(format!("{} failed an audit after making it", proof.signer));
                }
            }
            Ok(proof)
        });
        let proof = match verify {
            Ok(v) => v,
            Err(reason) => {
                println!("Skipped a proof for {}: {reason}", &commit[..12]);
                continue;
            }
        };
        accepted.push(object([
            ("commit", proof.payload.field("commit")?.clone()),
            ("signer", external(&proof.signer)),
            ("fingerprint", external(&proof.fingerprint)),
        ]));
        for (job, part) in map(proof.payload.field("jobs")?)? {
            for (name, bundle) in map(part.field("units")?)? {
                let key = (job.clone(), name.clone(), agreement(bundle, name)?);
                votes
                    .entry(key)
                    .or_insert_with(|| Vote {
                        signers: BTreeSet::new(),
                        bundle: bundle.clone(),
                        symbols: part.get("symbols").cloned(),
                    })
                    .signers
                    .insert(proof.signer.clone());
            }
        }
    }
    let mut agreed: BTreeMap<String, BTreeMap<String, Option<Vote>>> = BTreeMap::new();
    for ((job, name, _), vote) in votes {
        if (vote.signers.len() as i64) < quorum {
            continue;
        }
        agreed
            .entry(job)
            .or_default()
            .entry(name)
            .and_modify(|v| *v = None)
            .or_insert(Some(vote));
    }
    let mut totals = Vec::new();
    for (job, units) in agreed {
        let dir = job_dir(store, &job)?;
        let mut index = Map::new();
        for (name, slot) in units {
            if let Some(vote) = slot {
                payload::write_record(&dir, &name, &vote.bundle)?;
                payload::write_symbols(&dir, vote.symbols.as_ref())?;
                index.insert(
                    name,
                    Value::Array(vote.signers.iter().map(|s| external(s)).collect()),
                );
            } else {
                println!("Signers disagree about {job} {name}; it runs");
            }
        }
        if !index.is_empty() {
            totals.push((job, index.len()));
        }
        save(&dir.join("proof-signers.json"), &Value::Object(index))?;
    }
    for (job, n) in totals {
        println!("Took {n} {job} files that {quorum} or more signers agree on");
    }
    // take may print status lines before its final JSON. Preserve the public key order.
    let items = accepted
        .iter()
        .map(|v| {
            Ok(format!(
                "  {{\n    \"commit\": {},\n    \"signer\": {},\n    \"fingerprint\": {}\n  }}",
                paths::sorted(v.field("commit")?, false),
                paths::sorted(v.field("signer")?, false),
                paths::sorted(v.field("fingerprint")?, false)
            ))
        })
        .collect::<Result<Vec<_>>>()?;
    if items.is_empty() {
        println!("[]");
    } else {
        println!("[\n{}\n]", items.join(",\n"));
    }
    Ok(())
}
