//! Portable signed test results, published under leased Git refs.
mod payload;
pub(crate) mod sandbox;
mod transport;
pub(crate) use transport::load_book;

use crate::args::Args;
use crate::files;
use crate::git::Repo;
use crate::json::{object, Value};
use crate::json_paths as paths;
use crate::snapshot::valid_oid;
use crate::store::job_dir;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
type Result<T> = std::result::Result<T, String>;
type Map = BTreeMap<String, Value>;

fn map(v: &Value) -> Result<&Map> {
    match v {
        Value::Object(m) => Ok(m),
        _ => Err("Expected a JSON object".into()),
    }
}
fn field<'a>(v: &'a Value, k: &str) -> Option<&'a str> {
    v.get(k)?.string().ok()
}
fn external(s: &str) -> Value {
    Value::String(paths::path_key(s.as_bytes()))
}
fn save(path: &Path, v: &Value) -> Result<()> {
    files::replace(path, format!("{}\n", paths::sorted(v, true)).as_bytes())
        .map_err(|e| files::error(path, e))
}
const HELP: &str = include_str!("../help/proof.txt");
struct Form {
    help: &'static str,
    flags: &'static [&'static str],
    required: &'static [&'static str],
    pos: &'static str,
}
fn form(s: &str) -> Option<Form> {
    let (help, flags, required, pos): (_, &[&str], &[&str], _) = match s {
        "pack" => (
            include_str!("../help/proof-pack.txt"),
            &[
                "--store",
                "--job",
                "--repository",
                "--image",
                "--workdir",
                "--out",
            ],
            &["--store", "--job", "--repository", "--image", "--out"],
            "",
        ),
        "sign" => (
            include_str!("../help/proof-sign.txt"),
            &["--key"],
            &["--key"],
            "proof",
        ),
        "push" => (
            include_str!("../help/proof-push.txt"),
            &["--remote", "--repo", "--prefix"],
            &["--remote"],
            "proof",
        ),
        "book-push" | "book-pull" => (
            if s == "book-push" {
                include_str!("../help/proof-book-push.txt")
            } else {
                include_str!("../help/proof-book-pull.txt")
            },
            &["--remote", "--repo", "--ref"],
            &["--remote"],
            "book",
        ),
        "verify" => (
            include_str!("../help/proof-verify.txt"),
            &[
                "--repository",
                "--commit",
                "--allowed-signers",
                "--signers-at",
                "--repo",
                "--image",
                "--max-age-hours",
                "--revoked",
                "--into",
            ],
            &["--repository", "--commit", "--into"],
            "proof",
        ),
        "take" => (
            include_str!("../help/proof-take.txt"),
            &[
                "--remote",
                "--prefix",
                "--repository",
                "--commit",
                "--allowed-signers",
                "--signers-at",
                "--repo",
                "--image",
                "--max-age-hours",
                "--revoked",
                "--into",
                "--quorum",
                "--audit-book",
            ],
            &["--remote", "--repository", "--commit", "--into"],
            "",
        ),
        _ => return None,
    };
    Some(Form {
        help,
        flags,
        required,
        pos,
    })
}
fn usage(help: &str) -> &str {
    help.split("\n\n").next().unwrap()
}
fn parse<'a>(sub: &str, form: &Form, argv: &'a [String]) -> Result<Args<'a>> {
    let err = |s: String| {
        format!(
            "{}\nconcurrent-merge proof {sub}: error: {s}",
            usage(form.help)
        )
    };
    let args = Args::parse(argv, form.flags).map_err(err)?;
    let mut missing = Vec::new();
    if !form.pos.is_empty() && args.positional.is_empty() {
        missing.push(form.pos);
    }
    missing.extend(form.required.iter().filter(|flag| !args.has(flag)).copied());
    if !missing.is_empty() {
        return Err(err(format!(
            "the following arguments are required: {}",
            missing.join(", ")
        )));
    }
    if matches!(sub, "verify" | "take") {
        match (args.has("--allowed-signers"), args.has("--signers-at")) {
            (false, false) => {
                return Err(err(
                    "one of the arguments --allowed-signers --signers-at is required".into(),
                ))
            }
            (true, true) => {
                return Err(err(
                    "argument --signers-at: not allowed with argument --allowed-signers".into(),
                ))
            }
            _ => (),
        }
    }
    let n = usize::from(!form.pos.is_empty());
    if args.positional.len() > n {
        return Err(format!(
            "{}\nconcurrent-merge proof: error: unrecognized arguments: {}",
            usage(HELP),
            args.positional[n..].join(" ")
        ));
    }
    Ok(args)
}
fn execute(argv: &[String]) -> Result<i32> {
    let Some((sub, opts)) = argv.split_first() else {
        return Err(format!(
            "{}\nconcurrent-merge proof: error: the following arguments are required: action",
            usage(HELP)
        ));
    };
    if matches!(sub.as_str(), "--help" | "-h") {
        print!("{HELP}");
        return Ok(0);
    }
    let form=form(sub).ok_or_else(||format!("{}\nconcurrent-merge proof: error: argument action: invalid choice: '{sub}' (choose from 'pack', 'sign', 'push', 'verify', 'take', 'book-pull', 'book-push')",usage(HELP)))?;
    if opts
        .iter()
        .take_while(|s| s.as_str() != "--")
        .any(|s| matches!(s.as_str(), "--help" | "-h"))
    {
        print!("{}", form.help);
        return Ok(0);
    }
    let args = parse(sub, &form, opts)?;
    if sub == "pack" {
        payload::pack(&args)?;
        return Ok(0);
    }
    if sub == "sign" {
        payload::sign(Path::new(args.positional[0]), args.required("--key")?)?;
        return Ok(0);
    }
    let repo = Repo::new(Path::new(args.optional("--repo").unwrap_or(".")))?;
    match sub.as_str() {
        "push" => {
            println!(
                "{}",
                transport::push(Path::new(args.positional[0]), &repo, &args)?
            );
            Ok(0)
        }
        "book-pull" | "book-push" => {
            println!("{}", transport::book(&args, &repo, sub == "book-pull")?);
            Ok(0)
        }
        _ => {
            let tmp = tempfile::tempdir().map_err(|e| e.to_string())?;
            let signers = if let Some(path) = args.optional("--allowed-signers") {
                PathBuf::from(path)
            } else {
                let rev = args.required("--signers-at")?;
                let data = repo
                    .bytes(
                        &[
                            "show",
                            "--end-of-options",
                            &format!("{rev}:.lit/allowed_signers"),
                        ],
                        None,
                    )
                    .map_err(|_| format!("{rev} has no .lit/allowed_signers"))?;
                let path = tmp.path().join("allowed");
                files::replace(&path, &data).map_err(|e| e.to_string())?;
                path
            };
            let revoked = if let Some(path) = args.optional("--revoked") {
                String::from_utf8(files::read(Path::new(path))?)
                    .map_err(|e| e.to_string())?
                    .split_whitespace()
                    .map(str::to_owned)
                    .collect()
            } else {
                BTreeSet::new()
            };
            let age = args
                .optional("--max-age-hours")
                .unwrap_or("72")
                .parse::<f64>()
                .map_err(|_| "Invalid maximum proof age")?;
            if !age.is_finite() || age.abs() >= 24e9 {
                return Err("Invalid maximum proof age".into());
            }
            let policy = payload::Policy {
                repo: args.required("--repository")?,
                commits: args.all("--commit"),
                image: args.optional("--image"),
                age,
                revoked,
                signers,
            };
            let store = Path::new(args.required("--into")?);
            if sub == "verify" {
                let p = Path::new(args.positional[0]);
                match payload::verify(p, &payload::signature(p), &policy) {
                    Ok(v) => {
                        println!(
                            "Took {} records signed by {}",
                            payload::unpack(&v.payload, store)?,
                            v.signer
                        );
                        Ok(0)
                    }
                    Err(e) => {
                        println!("Rejected: {e}");
                        Ok(1)
                    }
                }
            } else {
                transport::take(&args, &repo, &policy, store)?;
                Ok(0)
            }
        }
    }
}
pub fn command(argv: &[String]) -> i32 {
    match execute(argv) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("{e}");
            2
        }
    }
}
