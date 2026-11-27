use super::*;
use std::io::{Cursor, Read};
fn counts(red: Option<&Value>) -> Result<(f64, f64)> {
    let Some(v) = red.filter(|v| v.truthy()) else {
        return Ok((0., 0.));
    };
    let [r, t] = v.array()? else {
        return Err("Invalid red share".into());
    };
    Ok((number(r)?, number(t)?))
}
pub(super) fn counted(row: &Value, returned: usize, tested: usize) -> Result<Value> {
    if tested == 0 {
        return Ok(row.clone());
    }
    let (r, t) = counts(row.get("red"))?;
    let rounded =
        |v: f64| Value::Number(format!("{:?}", format!("{v:.4}").parse::<f64>().unwrap()));
    with(
        row,
        "red",
        Value::Array(vec![
            rounded(r * 0.8 + returned as f64),
            rounded(t * 0.8 + tested as f64),
        ]),
    )
}
pub(super) fn limit(limit: Limit, red: Option<&Value>) -> Result<Option<usize>> {
    Ok(match limit {
        Limit::Fixed(n) => Some(n),
        Limit::All => None,
        Limit::Auto => {
            let (r, t) = counts(red)?;
            let p = ((r + 0.1) / (t + 5.)).clamp(1. / 256., 0.5);
            Some(1usize << (((1. - p) / p).log2().floor() as u32).min(7))
        }
    })
}
type Split = (Vec<Member>, Vec<Value>, String);
pub(super) fn halves(m: &[Member], mut rest: Vec<Value>, why: String) -> Split {
    let (a, b) = m.split_at(m.len() / 2);
    let mut groups = vec![rows(a), rows(b)];
    groups.append(&mut rest);
    (vec![], groups, why)
}
fn select(v: &Value, m: &[Member]) -> Result<Vec<Member>> {
    let lookup = m
        .iter()
        .map(|(n, h)| (n.to_string(), (*n, h)))
        .collect::<BTreeMap<_, _>>();
    v.array()?
        .iter()
        .map(|s| {
            let (n, h) = lookup
                .get(s.string()?)
                .ok_or("Missing source in blame verdict")?;
            Ok((*n, (*h).clone()))
        })
        .collect()
}
fn choose(
    m: &[Member],
    rest: Vec<Value>,
    failed: Map,
    changed: BTreeMap<String, Vec<String>>,
) -> Result<Split> {
    if failed.is_empty() || changed.is_empty() {
        return Ok(halves(m, rest, "no receipts or chain: halving".into()));
    }
    let verdict = crate::reports::verdict(failed, &changed)?;
    let keep = select(verdict.field("keep")?, m)?;
    let action = verdict.field("action")?.string()?;
    if action == "eject" {
        let remove = select(verdict.field("eject")?, m)?;
        let why = format!(
            "blame names #{}",
            remove.first().ok_or("Empty eject verdict")?.0
        );
        let mut pending = Vec::new();
        if !keep.is_empty() {
            pending.push(rows(&keep));
        }
        pending.extend(rest);
        return Ok((remove, pending, why));
    }
    if action == "bisect" && !keep.is_empty() {
        let suspects = select(verdict.field("bisect")?, m)?;
        let mut groups = vec![rows(&keep)];
        if !suspects.is_empty() {
            let (a, b) = suspects.split_at((suspects.len() / 2).max(1));
            groups.push(rows(a));
            if !b.is_empty() {
                groups.push(rows(b));
            }
        }
        groups.extend(rest);
        let why = format!(
            "blame suspects {}",
            suspects
                .iter()
                .map(|(n, _)| format!("#{n}"))
                .collect::<Vec<_>>()
                .join(", ")
        );
        return Ok((vec![], groups, why));
    }
    Ok(halves(m, rest, format!("blame: {action}, halving")))
}
fn receipts(client: &Client, repository: &str, run: &str) -> Result<Map> {
    let endpoint = format!("repos/{repository}/actions/runs/{run}/artifacts");
    let mut records = BTreeMap::new();
    for page in 1.. {
        let response = client.get(&format!("{endpoint}?per_page=100&page={page}"))?;
        let artifacts = response.field("artifacts")?.array()?;
        for a in artifacts {
            let name = a.field("name")?.string()?;
            if a.get("expired").is_some_and(Value::truthy)
                || !(name.starts_with("validation-timings-") || name.starts_with("lit-"))
            {
                continue;
            }
            let bytes = client.artifact(a.field("archive_download_url")?.string()?)?;
            let mut archive =
                zip::ZipArchive::new(Cursor::new(bytes)).map_err(|e| e.to_string())?;
            let mut expanded = 0u64;
            for i in 0..archive.len() {
                let file = archive.by_index(i).map_err(|e| e.to_string())?;
                let path = file
                    .enclosed_name()
                    .ok_or("Artifact path outside its directory")?;
                if path.extension().is_none_or(|s| s != "json") {
                    continue;
                }
                expanded = expanded.saturating_add(file.size());
                if expanded > 256 * 1024 * 1024 {
                    return Err("Receipt archive exceeds the read limit".into());
                }
                let mut raw = Vec::new();
                let size = file.size();
                file.take(size.saturating_add(1))
                    .read_to_end(&mut raw)
                    .map_err(|e| e.to_string())?;
                if raw.len() as u64 != size {
                    return Err("Incomplete artifact member".into());
                }
                if let Ok(row) = crate::json_paths::parse(&raw) {
                    if row.get("job").is_some() {
                        if let Some(Value::Object(failed)) = row.get("failed_reads") {
                            records.insert((name.to_owned(), path), failed.clone());
                        }
                    }
                }
            }
        }
        if artifacts.len() < 100 {
            break;
        }
    }
    Ok(records.into_values().flatten().collect())
}
/// Ids of the completed runs of batch `n`'s newest merge group, oldest first. A batch that
/// fails in the merge queue keeps its fast PR status, and the run behind that status never
/// ran the full suites, so the receipts of the failing tests are only among these runs.
pub(super) fn queue_runs(rows: &[Value], branch: &str, n: u64) -> Vec<String> {
    let prefix = format!("gh-readonly-queue/{branch}/pr-{n}-");
    let done: Vec<(&str, u64, &str)> = rows
        .iter()
        .filter(|r| {
            string(r, "head_branch").is_some_and(|b| b.starts_with(&prefix))
                && string(r, "status") == Some("completed")
        })
        .filter_map(|r| {
            Some((
                string(r, "created_at").unwrap_or(""),
                r.get("id")?.number().ok()?,
                string(r, "head_sha")?,
            ))
        })
        .collect();
    let Some(&(_, _, newest)) = done.iter().max_by_key(|(t, id, _)| (*t, *id)) else {
        return vec![];
    };
    let mut ids: Vec<u64> = done
        .iter()
        .filter(|(_, _, h)| *h == newest)
        .map(|(_, id, _)| *id)
        .collect();
    ids.sort_unstable();
    ids.iter().map(u64::to_string).collect()
}
impl Intake<'_> {
    pub(super) fn blame(
        &self,
        n: u64,
        row: &Value,
        m: &[Member],
        trusted: Option<&Value>,
        remaining: Vec<Value>,
    ) -> Result<Split> {
        let measured = (|| -> Result<(Map, BTreeMap<String, Vec<String>>)> {
            let head = row.field("tip")?.string()?;
            let base = row.field("base")?.string()?;
            let chain = self.repo.text(
                &[
                    "rev-list",
                    "--first-parent",
                    "--reverse",
                    "--parents",
                    &format!("{base}..{head}"),
                ],
                None,
            )?;
            let steps = chain
                .lines()
                .map(|l| l.split_whitespace().collect::<Vec<_>>())
                .collect::<Vec<_>>();
            if steps.len() != m.len() || steps.iter().any(|s| s.len() < 2) {
                return Ok((Map::new(), BTreeMap::new()));
            }
            let mut changed = BTreeMap::new();
            for ((n, _), step) in m.iter().zip(steps) {
                let list = crate::merge::path_changes(&self.repo, step[1], step[0])?;
                changed.insert(
                    n.to_string(),
                    list.iter()
                        .map(|b| crate::json_paths::path_key(b))
                        .collect(),
                );
            }
            let listing = self.src.client.get(&format!(
                "repos/{}/actions/runs?event=merge_group&per_page=100",
                self.src.repository
            ));
            let mut runs = listing
                .ok()
                .and_then(|l| {
                    l.get("workflow_runs")
                        .and_then(|w| w.array().ok())
                        .map(|w| queue_runs(w, self.src.branch, n))
                })
                .unwrap_or_default();
            if runs.is_empty() {
                if let Some(id) = trusted
                    .and_then(|v| string(v, "targetUrl"))
                    .and_then(|u| guard::run_id(self.src.repository, u))
                {
                    runs.push(id.to_owned());
                }
            }
            let mut failed = Map::new();
            for id in &runs {
                failed
                    .extend(receipts(self.src.client, self.src.repository, id).unwrap_or_default());
            }
            Ok((failed, changed))
        })();
        let (failed, changed) = measured.unwrap_or_else(|e| {
            println!("Blame unavailable: {e}");
            (Map::new(), BTreeMap::new())
        });
        choose(m, remaining, failed, changed)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn receipts_come_from_the_newest_merge_group_of_the_batch() {
        let run = |id: u64, branch: &str, sha: &str, status: &str, at: &str| {
            crate::json::parse(&format!(r#"{{"id":{id},"head_branch":"{branch}","head_sha":"{sha}","status":"{status}","created_at":"{at}"}}"#)).unwrap()
        };
        let rows = vec![
            run(
                7,
                "gh-readonly-queue/main/pr-12-aaa",
                "old",
                "completed",
                "2026-01-01T10:00:00Z",
            ),
            run(
                9,
                "gh-readonly-queue/main/pr-12-bbb",
                "new",
                "completed",
                "2026-01-01T11:00:00Z",
            ),
            run(
                8,
                "gh-readonly-queue/main/pr-12-bbb",
                "new",
                "completed",
                "2026-01-01T11:00:00Z",
            ),
            run(
                10,
                "gh-readonly-queue/main/pr-12-bbb",
                "new",
                "in_progress",
                "2026-01-01T11:00:00Z",
            ),
            run(
                11,
                "gh-readonly-queue/main/pr-120-ccc",
                "x",
                "completed",
                "2026-01-01T12:00:00Z",
            ),
            run(
                12,
                "gh-readonly-queue/dev/pr-12-ddd",
                "y",
                "completed",
                "2026-01-01T12:00:00Z",
            ),
        ];
        assert_eq!(queue_runs(&rows, "main", 12), vec!["8", "9"]);
        assert!(queue_runs(&rows, "main", 13).is_empty());
    }
    #[test]
    fn fresh_size_and_decayed_returns() {
        let row = Value::Object(Map::new());
        assert_eq!(limit(Limit::Auto, None).unwrap(), Some(32));
        let row = counted(&row, 1, 5).unwrap();
        assert_eq!(limit(Limit::Auto, row.get("red")).unwrap(), Some(8));
        let row = counted(&row, 0, 20).unwrap();
        assert_eq!(
            row.get("red"),
            Some(&Value::Array(vec![
                Value::Number("0.8".into()),
                Value::Number("24.0".into())
            ]))
        );
    }
}
