//! Replay GitHub observations on loopback and compare mutations against two private Git remotes.
use crate::{
    collect,
    json::{self, object, Value},
    parity::copy_tree,
};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    path::Path,
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::Duration,
};
type Result<T> = std::result::Result<T, String>;
fn io(e: impl std::fmt::Display) -> String {
    e.to_string()
}

pub struct Replay {
    stop: Arc<AtomicBool>,
    worker: Option<thread::JoinHandle<Result<Value>>>,
    address: std::net::SocketAddr,
}
impl Replay {
    pub fn start(
        cmd: &mut Command,
        src: &Path,
        dir: &Path,
        argv: &[String],
        epoch: &str,
    ) -> Result<Self> {
        let remote = dir.join("intake-remote.git");
        let input = dir.join("intake-api.json");
        if !remote.join("HEAD").is_file() || !input.is_file() {
            return Err("intake parity needs a bare intake-remote.git and an intake-api.json fixture in the current folder".into());
        }
        let remote = remote.canonicalize().map_err(io)?;
        let root = dir.canonicalize().map_err(io)?;
        if !remote.starts_with(&root) {
            return Err("The intake parity remote must be inside the fixture".into());
        }
        let fixture = json::parse(&fs::read_to_string(input).map_err(io)?)?;
        let rules = fixture.array()?.to_vec();
        let home = dir.join(".intake-home");
        fs::create_dir_all(&home).map_err(io)?;
        let mut args = argv.to_vec();
        let mut repository = None;
        let mut i = 1;
        while i < args.len() {
            let (k, v) = args[i]
                .split_once('=')
                .map_or((args[i].as_str(), None), |(k, v)| (k, Some(v)));
            if k.starts_with("--")
                && [
                    "--repo-dir",
                    "--branch",
                    "--context",
                    "--label",
                    "--max-batch",
                    "--since",
                ]
                .iter()
                .any(|n| n.starts_with(k))
            {
                let repository_dir = "--repo-dir".starts_with(k);
                let inline = v.is_some();
                let argname = k.to_owned();
                let pos = if inline { i } else { i + 1 };
                let Some(val) = v.or_else(|| args.get(pos).map(String::as_str)) else {
                    break;
                };
                if repository_dir {
                    let path = src.join(val);
                    let dest = dir.join(".intake-cache.git");
                    if path.exists() {
                        copy_tree(&path.canonicalize().map_err(io)?, &dest).map_err(io)?;
                    }
                    let dest = dest.to_string_lossy();
                    args[pos] = if inline {
                        format!("{argname}={dest}")
                    } else {
                        dest.into_owned()
                    };
                }
                i = pos + 1;
                continue;
            }
            if !k.starts_with('-') && repository.is_none() {
                repository = Some(k.to_owned());
            }
            i += 1;
        }
        let repo = repository.ok_or("Missing intake repository")?;
        collect::repository_parts(&repo)?;
        let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(io)?;
        let address = listener.local_addr().map_err(io)?;
        let url = format!("http://{address}");
        let git_date = format!("@{epoch} +0000");
        let null = if cfg!(windows) { "NUL" } else { "/dev/null" };
        cmd.args(args)
            .env("GITHUB_API_URL", &url)
            .env("GITHUB_GRAPHQL_URL", format!("{url}/graphql"))
            .env("GH_TOKEN", "lit-fixture")
            .env("GITHUB_TOKEN", "lit-fixture")
            .env("NO_PROXY", "127.0.0.1")
            .env("no_proxy", "127.0.0.1")
            .env("GIT_ALLOW_PROTOCOL", "file")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", null)
            .env_remove("GIT_CONFIG_PARAMETERS")
            .env_remove("LIT_PARITY_LAND_TARGET")
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .env("GIT_CONFIG_COUNT", "2")
            .env(
                "GIT_CONFIG_KEY_0",
                format!("url.{}.insteadOf", remote.display()),
            )
            .env(
                "GIT_CONFIG_VALUE_0",
                format!("https://github.com/{repo}.git"),
            )
            .env("GIT_CONFIG_KEY_1", "core.hooksPath")
            .env("GIT_CONFIG_VALUE_1", null)
            .env("GIT_AUTHOR_DATE", &git_date)
            .env("GIT_COMMITTER_DATE", &git_date)
            .env("HOME", &home)
            .env("USERPROFILE", &home);
        let stop = Arc::new(AtomicBool::new(false));
        let halted = stop.clone();
        let worker = thread::spawn(move || {
            let mut changes = vec![];
            let mut counts = BTreeMap::new();
            let mut errors = vec![];
            for incoming in listener.incoming() {
                let stream = incoming.map_err(io)?;
                if halted.load(Ordering::Acquire) {
                    break;
                }
                if let Err(e) = request(stream, &root, &rules, &mut counts, &mut changes) {
                    errors.push(e);
                }
            }
            if !errors.is_empty() {
                return Err(errors.join("\n"));
            }
            Ok(Value::Array(changes))
        });
        Ok(Self {
            stop,
            worker: Some(worker),
            address,
        })
    }
    pub fn finish(mut self) -> Result<Value> {
        self.stop.store(true, Ordering::Release);
        let _ = TcpStream::connect(self.address);
        self.worker
            .take()
            .unwrap()
            .join()
            .map_err(|_| "Intake replay failed")?
    }
}
impl Drop for Replay {
    fn drop(&mut self) {
        if self.worker.is_some() {
            self.stop.store(true, Ordering::Release);
            let _ = TcpStream::connect(self.address);
        }
    }
}

fn request(
    mut stream: TcpStream,
    root: &Path,
    rules: &[Value],
    counts: &mut BTreeMap<usize, usize>,
    changes: &mut Vec<Value>,
) -> Result<()> {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(io)?;
    let mut reader = BufReader::new(&stream);
    let mut line = String::new();
    reader.read_line(&mut line).map_err(io)?;
    let mut parts = line.split_whitespace();
    let method = parts.next().ok_or("Missing request")?.to_owned();
    let path = parts.next().ok_or("Missing HTTP path")?.to_owned();
    let mut len = 0usize;
    loop {
        line.clear();
        if reader.read_line(&mut line).map_err(io)? == 0 || line.trim().is_empty() {
            break;
        }
        if let Some((key, val)) = line.trim().split_once(':') {
            if key.eq_ignore_ascii_case("content-length") {
                len = val.trim().parse().map_err(io)?;
            }
        }
    }
    if len > 8 * 1024 * 1024 {
        return Err("Intake fixture request exceeds the read limit".into());
    }
    let mut raw = vec![0; len];
    reader.read_exact(&mut raw).map_err(io)?;
    drop(reader);
    let body = if raw.is_empty() {
        Value::Null
    } else {
        json::parse(std::str::from_utf8(&raw).map_err(io)?)?
    };
    let query = body
        .get("query")
        .and_then(|v| v.string().ok())
        .unwrap_or("");
    let query = query.split_whitespace().collect::<String>();
    let rule = rules.iter().enumerate().find(|(_, r)| {
        r.get("method").and_then(|v| v.string().ok()) == Some(&method)
            && r.get("path").and_then(|v| v.string().ok()) == Some(&path)
            && r.get("query_contains")
                .and_then(|v| v.string().ok())
                .is_none_or(|s| query.contains(&s.split_whitespace().collect::<String>()))
            && r.get("variables")
                .is_none_or(|v| body.get("variables") == Some(v))
    });
    let mut error = None;
    if let Some(path) = rule
        .and_then(|(_, r)| r.get("file"))
        .and_then(|v| v.string().ok())
    {
        let source = root.join(path).canonicalize().map_err(io)?;
        if !source.starts_with(root) || method != "GET" {
            return Err("Invalid intake fixture file".into());
        }
        let bytes = fs::read(source).map_err(io)?;
        write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/zip\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",bytes.len()).map_err(io)?;
        return stream.write_all(&bytes).map_err(io);
    }
    let (status, response) = if let Some((i, r)) = rule {
        let uses = counts.entry(i).or_default();
        *uses += 1;
        let response = if *uses > 256 {
            None
        } else if let Some(list) = r.get("replies") {
            list.array()?.get(*uses - 1)
        } else {
            r.get("reply")
        };
        if let Some(value) = response {
            if (method != "GET" && path != "/graphql") || query.starts_with("mutation") {
                let payload = if path == "/graphql" {
                    object([
                        ("query", query.as_str().into()),
                        ("variables", body.field("variables")?.clone()),
                    ])
                } else {
                    body
                };
                changes.push(object([
                    ("method", method.as_str().into()),
                    ("path", path.as_str().into()),
                    ("body", payload),
                ]));
            }
            (
                r.get("status")
                    .map(Value::number)
                    .transpose()?
                    .unwrap_or(200),
                value.clone(),
            )
        } else {
            error = Some("Intake fixture exhausted its recorded replies".to_string());
            (500, Value::Null)
        }
    } else {
        error = Some(format!(
            "Intake fixture has no reply for {method} {path} {query}"
        ));
        (404, Value::Null)
    };
    let json = json::canonical(&response);
    write!(stream,"HTTP/1.1 {status} Reply\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{json}",json.len()).map_err(io)?;
    error.map_or(Ok(()), Err)
}
