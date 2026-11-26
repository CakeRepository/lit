//! GitHub's JSON APIs, using the same credential order as the Python client.
use crate::json::{self, Value};
use std::io::Write;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use ureq::http::{Request, Uri};
use ureq::tls::{RootCerts, TlsConfig};
use ureq::Agent;

pub struct Client {
    http: Agent,
    api: String,
    graphql_url: String,
}

fn env_value(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn credential(url: &str) -> Option<String> {
    if let Some(token) = env_value("GH_TOKEN").or_else(|| env_value("GITHUB_TOKEN")) {
        return Some(token);
    }
    let uri = url.parse::<Uri>().ok()?;
    let host = uri.host()?;
    let host = host.strip_prefix("api.").unwrap_or(host);
    let mut child = Command::new("git")
        .args(["credential", "fill"])
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GCM_INTERACTIVE", "Never")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut stdin = child.stdin.take()?;
    let sent = write!(stdin, "protocol=https\nhost={host}\n\n");
    drop(stdin);
    let until = Instant::now() + Duration::from_secs(15);
    while child.try_wait().ok()?.is_none() {
        if sent.is_err() || Instant::now() >= until {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    let output = child.wait_with_output().ok()?;
    let credentials = std::str::from_utf8(&output.stdout).ok()?;
    credentials
        .lines()
        .find_map(|s| s.strip_prefix("password=").filter(|p| !p.is_empty()))
        .map(str::to_owned)
}

impl Client {
    pub fn new() -> Self {
        let api = env_value("GITHUB_API_URL")
            .unwrap_or_else(|| "https://api.github.com".into())
            .trim_end_matches('/')
            .to_owned();
        let graphql_url =
            env_value("GITHUB_GRAPHQL_URL").unwrap_or_else(|| format!("{api}/graphql"));
        let config = Agent::config_builder()
            .http_status_as_error(false)
            .timeout_global(Some(Duration::from_secs(60)))
            .tls_config(
                TlsConfig::builder()
                    .root_certs(RootCerts::PlatformVerifier)
                    .build(),
            )
            .build();
        Self {
            http: config.into(),
            api,
            graphql_url,
        }
    }

    fn request(&self, method: &str, url: &str, data: Option<String>) -> Result<Value, String> {
        let mut req = Request::builder()
            .uri(url)
            .method(method)
            .header("User-Agent", "lit")
            .header("Accept", "application/vnd.github+json")
            .header("X-GitHub-Api-Version", "2022-11-28");
        if let Some(secret) = credential(url) {
            req = req.header("Authorization", format!("Bearer {secret}"));
        }
        if data.is_some() {
            req = req.header("Content-Type", "application/json");
        }
        let req = req
            .body(data.unwrap_or_default())
            .map_err(|e| e.to_string())?;
        let mut response = self
            .http
            .run(req)
            .map_err(|e| format!("could not reach GitHub: {e}"))?;
        let status = response.status();
        let raw = response
            .body_mut()
            .with_config()
            .limit(128 * 1024 * 1024)
            .read_to_string()
            .map_err(|e| e.to_string())?;
        if !status.is_success() {
            let parsed = json::parse(&raw).ok();
            let message = parsed
                .as_ref()
                .and_then(|p| p.get("message"))
                .and_then(|p| p.string().ok())
                .or_else(|| status.canonical_reason())
                .unwrap_or("request failed");
            return Err(format!("GitHub {}: {message}", status.as_u16()));
        }
        if raw.trim().is_empty() {
            Ok(Value::Null)
        } else {
            json::parse(&raw)
        }
    }

    pub fn get(&self, path: &str) -> Result<Value, String> {
        self.request("GET", &format!("{}/{}", self.api, path), None)
    }
    pub fn artifact(&self, location: &str) -> Result<Vec<u8>, String> {
        let base = format!("{}/", self.api);
        let url = if location.starts_with(&base) {
            location.to_owned()
        } else if !location.contains("://") && !location.starts_with("//") {
            format!("{base}{}", location.trim_start_matches('/'))
        } else {
            return Err("Unexpected GitHub artifact URL".into());
        };
        let mut req = self
            .http
            .get(&url)
            .header("User-Agent", "lit")
            .header("Accept", "application/vnd.github+json");
        if let Some(token) = credential(&url) {
            req = req.header("Authorization", format!("Bearer {token}"));
        }
        // Strip credentials on every redirect, including GitHub's signed storage URL.
        let mut response = req
            .config()
            .redirect_auth_headers(ureq::config::RedirectAuthHeaders::Never)
            .build()
            .call()
            .map_err(|e| e.to_string())?;
        if !response.status().is_success() {
            return Err(format!("GitHub artifact: {}", response.status()));
        }
        response
            .body_mut()
            .with_config()
            .limit(256 * 1024 * 1024)
            .read_to_vec()
            .map_err(|e| e.to_string())
    }
    pub fn post(&self, path: &str, body: Value) -> Result<Value, String> {
        self.request(
            "POST",
            &format!("{}/{}", self.api, path),
            Some(json::pretty(&body)),
        )
    }
    pub fn patch(&self, path: &str, body: Value) -> Result<Value, String> {
        self.request(
            "PATCH",
            &format!("{}/{}", self.api, path),
            Some(json::pretty(&body)),
        )
    }

    pub fn pages(&self, path: &str) -> Result<Vec<Value>, String> {
        let sep = if path.contains('?') { '&' } else { '?' };
        let mut rows = Vec::new();
        for page in 1.. {
            let response = self.get(&format!("{path}{sep}per_page=100&page={page}"))?;
            let Value::Array(found) = response else {
                return Err("Expected a GitHub list".into());
            };
            let complete = found.len() < 100;
            rows.extend(found);
            if complete {
                break;
            }
        }
        Ok(rows)
    }

    pub fn graphql(&self, query: &str, variables: Value) -> Result<Value, String> {
        let body = json::object([("query", query.into()), ("variables", variables)]);
        let result = self.request("POST", &self.graphql_url, Some(json::pretty(&body)))?;
        if let Some(Value::Array(errors)) = result.get("errors") {
            if !errors.is_empty() {
                return Err(errors
                    .iter()
                    .map(|e| {
                        e.get("message")
                            .and_then(|v| v.string().ok())
                            .unwrap_or("GraphQL error")
                    })
                    .collect::<Vec<_>>()
                    .join("; "));
            }
        }
        Ok(result)
    }
}
