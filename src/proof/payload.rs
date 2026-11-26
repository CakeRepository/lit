//! Pack passing unit records and verify signed payloads before writing to a store.
use super::*;
use flate2::{read::MultiGzDecoder, Compression, GzBuilder};
use std::io::{Read, Seek, Write};
use std::process::{Command, Stdio};

pub fn unpacked(bytes: &[u8]) -> Result<Value> {
    let mut data = Vec::new();
    MultiGzDecoder::new(bytes)
        .read_to_end(&mut data)
        .map_err(|e| format!("Unreadable proof: {e}"))?;
    paths::parse(&data).map_err(|e| format!("Unreadable proof: {e}"))
}
fn valid_unit(unit: &Value) -> bool {
    unit.get("exit_code").and_then(|v| v.number().ok()) == Some(0)
        && !unit.get("incomplete").is_some_and(Value::truthy)
}
fn names(dir: &Path) -> Result<Vec<PathBuf>> {
    let entries = match fs::read_dir(dir) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(files::error(dir, e)),
    };
    let mut paths = Vec::new();
    for entry in entries {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.extension().is_some_and(|e| e == "json") {
            paths.push(path);
        }
    }
    paths.sort();
    Ok(paths)
}
pub fn pack(args: &Args<'_>) -> Result<()> {
    let repo = Repo::new(Path::new(args.optional("--workdir").unwrap_or(".")))?;
    let commit = repo.text(&["rev-parse", "HEAD"], None)?;
    let tree = repo.text(&["rev-parse", "HEAD^{tree}"], None)?;
    if !valid_oid(&commit) || !valid_oid(&tree) {
        return Err("pack needs full commit and tree IDs".into());
    }
    let image = args.required("--image")?;
    if image.is_empty() {
        return Err("pack needs the container image the tests ran in".into());
    }
    let mut jobs = Map::new();
    for &job in args.all("--job") {
        let dir = job_dir(Path::new(args.required("--store")?), job)?;
        let (mut units, mut symbols) = (Map::new(), Map::new());
        for p in names(&dir.join("units"))? {
            let Ok(bundle) = paths::read(&p) else {
                continue;
            };
            if field(&bundle, "kind") != Some("lit-unit-footprints")
                || field(&bundle, "commit") != Some(&commit)
            {
                continue;
            }
            for (name, unit) in map(bundle.field("units")?)? {
                if !valid_unit(unit) {
                    continue;
                }
                units.insert(name.clone(), bundle.clone());
                if let Some(Value::Object(reads)) = unit.get("symbols") {
                    for path in reads.keys() {
                        let oid = bundle
                            .get("tracked")
                            .and_then(|t| t.get(path))
                            .and_then(|v| v.array().ok())
                            .and_then(|v| v.get(1))
                            .and_then(|v| v.string().ok());
                        if let Some(oid) = oid {
                            if !valid_oid(oid) {
                                return Err("Proof holds a function table with a bad name".into());
                            }
                            let table = dir.join("symbols").join(format!("{oid}.json"));
                            if table.exists() {
                                symbols.insert(oid.to_owned(), paths::read(&table)?);
                            }
                        }
                    }
                }
            }
        }
        if !units.is_empty() {
            jobs.insert(
                job.to_owned(),
                object([
                    ("units", Value::Object(units)),
                    ("symbols", Value::Object(symbols)),
                ]),
            );
        }
    }
    if jobs.is_empty() {
        return Err("No passing records for this commit in the given jobs".into());
    }
    let now = std::env::var("LIT_PARITY_PROOF_TIME")
        .unwrap_or_else(|_| format!("{:.6}", jiff::Timestamp::now()).replace('Z', "+00:00"));
    let payload = object([
        ("kind", "lit-proof".into()),
        ("version", 1u64.into()),
        ("commit", commit.as_str().into()),
        ("tree", tree.as_str().into()),
        ("created", now.as_str().into()),
        ("image", external(image)),
        ("repository", external(args.required("--repository")?)),
        ("jobs", Value::Object(jobs)),
    ]);
    let mut compressor = GzBuilder::new()
        .mtime(0)
        .operating_system(255)
        .write(Vec::new(), Compression::best());
    compressor
        .write_all(paths::sorted(&payload, false).as_bytes())
        .map_err(|e| e.to_string())?;
    let out = Path::new(args.required("--out")?);
    files::replace(out, &compressor.finish().map_err(|e| e.to_string())?)
        .map_err(|e| files::error(out, e))?;
    println!("{}", out.display());
    Ok(())
}
fn input(bytes: &[u8]) -> Result<Stdio> {
    let mut f = tempfile::tempfile().map_err(|e| e.to_string())?;
    f.write_all(bytes)
        .and_then(|_| f.rewind())
        .map_err(|e| e.to_string())?;
    Ok(f.into())
}
pub fn signature(proof: &Path) -> PathBuf {
    let mut path = proof.as_os_str().to_owned();
    path.push(".sig");
    path.into()
}
pub fn sign(proof: &Path, key: &str) -> Result<()> {
    let sig = signature(proof);
    let out = Command::new("ssh-keygen")
        .args(["-q", "-Y", "sign", "-f", key, "-n", "lit-proof"])
        .stdin(input(&files::read(proof)?)?)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().into());
    }
    files::replace(&sig, &out.stdout).map_err(|e| files::error(&sig, e))?;
    println!("{}", sig.display());
    Ok(())
}
pub struct Verified {
    pub payload: Value,
    pub signer: String,
    pub fingerprint: String,
}
pub struct Policy<'a> {
    pub repo: &'a str,
    pub commits: &'a [&'a str],
    pub image: Option<&'a str>,
    pub age: f64,
    pub revoked: BTreeSet<String>,
    pub signers: PathBuf,
}
fn signer(bytes: &[u8], sig: &Path, allowed: &Path) -> Result<(String, String)> {
    let found = Command::new("ssh-keygen")
        .args(["-Y", "find-principals", "-s"])
        .arg(sig)
        .arg("-f")
        .arg(allowed)
        .args(["-n", "lit-proof"])
        .output()
        .map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&found.stdout);
    let principal = text
        .split_whitespace()
        .next()
        .filter(|_| found.status.success())
        .ok_or("The proof is not signed by an allowed signer")?;
    let out = Command::new("ssh-keygen")
        .args(["-Y", "verify", "-f"])
        .arg(allowed)
        .args(["-I", principal, "-n", "lit-proof", "-s"])
        .arg(sig)
        .stdin(input(bytes)?)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err("The proof's signature does not match its contents".into());
    }
    let msg = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let fp = msg
        .split_once("key SHA256:")
        .and_then(|(_, s)| s.split_whitespace().next())
        .map(|s| format!("SHA256:{s}"))
        .unwrap_or_default();
    Ok((principal.into(), fp))
}
fn moment(text: &str) -> Result<jiff::Timestamp> {
    text.parse().map_err(|_| {
        if text.contains('T')
            && !text.ends_with('Z')
            && !text.get(11..).unwrap_or("").contains(['+', '-'])
        {
            "Proof time has no time zone".to_owned()
        } else {
            format!("Invalid proof time: {text}")
        }
    })
}
pub fn verify(proof: &Path, sig: &Path, policy: &Policy<'_>) -> Result<Verified> {
    let bytes = files::read(proof)?;
    let (signer, fingerprint) = signer(&bytes, sig, &policy.signers)?;
    if !fingerprint.is_empty() && policy.revoked.contains(&fingerprint) {
        return Err(format!("{signer}'s key is revoked"));
    }
    let p = unpacked(&bytes)?;
    if field(&p, "kind") != Some("lit-proof")
        || p.get("version").and_then(|v| v.number().ok()) != Some(1)
    {
        return Err("Not a lit proof this version understands".into());
    }
    let repo = field(&p, "repository").unwrap_or("None");
    if external(policy.repo) != Value::from(repo) {
        return Err(format!("Proof is for {repo}, not {}", policy.repo));
    }
    let commit = field(&p, "commit").ok_or("Proof is for a commit this run did not ask about")?;
    if !policy.commits.contains(&commit) {
        return Err("Proof is for a commit this run did not ask about".into());
    }
    if let Some(expected) = policy.image {
        let image = field(&p, "image").unwrap_or("None");
        if external(expected) != Value::from(image) {
            return Err(format!("Proof ran in {image}, not {expected}"));
        }
    }
    let created = moment(p.field("created")?.string()?)?;
    let now = jiff::Timestamp::now();
    let age = (now.as_nanosecond() - created.as_nanosecond()) as f64 / 1e9;
    if age < -300.0 {
        return Err("Proof is dated in the future".into());
    }
    if age > policy.age * 3600.0 {
        return Err("Proof is too old".into());
    }
    for (job, part) in map(p.field("jobs")?)? {
        job_dir(Path::new("."), job)?;
        for (name, bundle) in map(part.field("units")?)? {
            let unit = bundle.get("units").and_then(|v| v.get(name));
            if field(bundle, "kind") != Some("lit-unit-footprints")
                || field(bundle, "commit") != Some(commit)
                || bundle.get("tree").or_else(|| p.get("tree")) != p.get("tree")
                || !unit.is_some_and(valid_unit)
            {
                return Err(format!(
                    "Proof record for {job} {name} is not a passing record of its commit"
                ));
            }
        }
        if let Some(tables) = part.get("symbols") {
            if map(tables)?.keys().any(|oid| !valid_oid(oid)) {
                return Err("Proof holds a function table with a bad name".into());
            }
        }
    }
    Ok(Verified {
        payload: p,
        signer,
        fingerprint,
    })
}
pub fn write_record(dir: &Path, name: &str, bundle: &Value) -> Result<()> {
    let id = crate::sha256::hex(paths::utf8(name)?.as_bytes());
    save(
        &dir.join("units").join(format!("{}.json", &id[..32])),
        bundle,
    )
}
pub fn write_symbols(dir: &Path, symbols: Option<&Value>) -> Result<()> {
    if let Some(tables) = symbols {
        for (oid, table) in map(tables)? {
            if !valid_oid(oid) {
                return Err("Proof holds a function table with a bad name".into());
            }
            save(&dir.join("symbols").join(format!("{oid}.json")), table)?;
        }
    }
    Ok(())
}
pub fn unpack(p: &Value, store: &Path) -> Result<usize> {
    let mut count = 0;
    for (job, part) in map(p.field("jobs")?)? {
        let dir = job_dir(store, job)?;
        for (name, bundle) in map(part.field("units")?)? {
            write_record(&dir, name, bundle)?;
            count += 1;
        }
        write_symbols(&dir, part.get("symbols"))?;
    }
    Ok(count)
}
pub fn time(p: &Value, key: &str) -> Result<jiff::Timestamp> {
    moment(p.field(key)?.string()?)
}
