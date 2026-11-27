//! Land a batch on the target branch itself, without the merge queue, when checks that already
//! ran cover it: either the batch PR passed the full-suite status at its exact tip on the current
//! target, or every member passed it at its exact head and no two of them, nor the commits that
//! reached the target since a member was tested, change the same file. Anything else goes
//! through the merge queue as before, and the record says why.
use super::*;
use crate::merge::path_changes;
use guard::approved;

/// A member's number with the files it changed, the files the target changed since its base,
/// and the files where its head and the batch tip differ.
pub(super) type Footprint = (u64, Vec<String>, Vec<String>, Vec<String>);

/// Why a batch can't land directly, or None when its members' own checks cover it: each
/// member's `own` files must be changed neither by the target `branch` since that member's base
/// (`drift`) nor by another member, the batch tip must keep each of them as its member left it
/// (`differ` lists the files where the member head and the tip differ), and the tip may change
/// nothing else (`extra` lists what the tip changes on the base).
pub(super) fn overlap(branch: &str, members: &[Footprint], extra: &[String]) -> Option<String> {
    let mut seen = BTreeMap::new();
    for (n, own, drift, differ) in members {
        let drift: BTreeSet<_> = drift.iter().collect();
        let differ: BTreeSet<_> = differ.iter().collect();
        for p in own {
            if drift.contains(p) {
                return Some(format!("#{n} and {branch} both changed {p}"));
            }
            if let Some(o) = seen.insert(p, n) {
                return Some(format!("#{o} and #{n} both changed {p}"));
            }
            if differ.contains(p) {
                return Some(format!("the batch does not keep #{n}'s {p}"));
            }
        }
    }
    extra
        .iter()
        .find(|p| !seen.contains_key(p))
        .map(|p| format!("the batch changes {p} outside its PRs"))
}

impl Intake<'_> {
    fn queue_instead(&self, lock: &str, row: &Value, why: String) -> Result<Option<bool>> {
        println!("Batch {} goes through the merge queue: {why}", short(row)?);
        self.change_record(lock, &with(row, "direct", Value::String(why))?)?;
        Ok(Some(true))
    }
    /// Some(progress) when the direct path handled this pass, None to use the merge queue.
    pub(super) fn direct(
        &mut self,
        refs: &Refs,
        lock: &str,
        row: &Value,
        m: &[Member],
        remaining: &[Value],
    ) -> Result<Option<bool>> {
        let Some(context) = self.direct.clone() else {
            return Ok(None);
        };
        if row.get("direct").is_some() || row.get("refused").is_some() || m.is_empty() {
            return Ok(None);
        }
        let base = row.field("base")?.string()?;
        let tip = row.field("tip")?.string()?;
        let target = refs.get(&self.target).ok_or("Missing target")?.clone();
        self.fetch([target.as_str(), base, tip])?;
        let landed = split::counted(row, 0, m.len())?;
        if self.repo.ancestor(tip, &target)? {
            println!(
                "Landed batch {} directly with {} source PRs.",
                short(row)?,
                m.len()
            );
            self.finish(refs, lock, &landed, m, remaining.to_vec())?;
            return Ok(Some(true));
        }
        if row.get("isolated_sibling").is_some_and(Value::truthy)
            && self
                .repo
                .text(&["rev-parse", &format!("{tip}^{{tree}}")], None)?
                != self
                    .repo
                    .text(&["rev-parse", &format!("{}^{{tree}}", m[0].1)], None)?
        {
            return self.queue_instead(
                lock,
                row,
                "isolated source requires its exact combined tree".into(),
            );
        }
        if target != base {
            return self.queue_instead(
                lock,
                row,
                format!("{} moved since the batch was built", self.src.branch),
            );
        }
        match self.src.verify(m) {
            Err(Error::Hold(_)) => return Ok(None),
            Err(e) => return Err(e),
            Ok(_) => {}
        }
        let numbers: Vec<u64> = m.iter().map(|(n, _)| *n).collect();
        let sources = self.src.pull_many(&numbers)?;
        for (n, h) in m {
            let pr = &sources.get(n).ok_or("Missing source PR")?.0;
            if !approved(pr)? {
                return self.queue_instead(lock, row, format!("#{n} is not approved"));
            }
            match self
                .src
                .status(pr, h, &context)
                .and_then(|s| string(s, "state"))
            {
                Some("SUCCESS") => {}
                None => {
                    return self.queue_instead(lock, row, format!("#{n} has no {context} status"))
                }
                Some(state) => {
                    return self.queue_instead(lock, row, format!("#{n}'s {context} is {state}"))
                }
            }
        }
        let names = |from: &str, to: &str| -> Result<Vec<String>> {
            Ok(path_changes(&self.repo, from, to)?
                .iter()
                .map(|b| crate::json_paths::path_key(b))
                .collect())
        };
        let mut changes = Vec::new();
        for (n, h) in m {
            let from = self.repo.text(&["merge-base", h, base], None)?;
            changes.push((*n, names(&from, h)?, names(&from, base)?, names(h, tip)?));
        }
        if let Some(why) = overlap(self.src.branch, &changes, &names(base, tip)?) {
            return self.queue_instead(lock, row, why);
        }
        self.land(refs, lock, row, m, remaining).map(Some)
    }
    /// Land a batch whose PR passed the full-suite status at its exact tip, built on the current
    /// target: the tree that was tested is the tree that lands. Some(progress) when it did.
    pub(super) fn direct_batch(
        &mut self,
        refs: &Refs,
        lock: &str,
        row: &Value,
        pr: &Value,
        m: &[Member],
        remaining: &[Value],
    ) -> Result<Option<bool>> {
        let Some(context) = self.direct.clone() else {
            return Ok(None);
        };
        let tip = row.field("tip")?.string()?;
        if row.get("refused").is_some()
            || refs.get(&self.target).map(String::as_str) != Some(row.field("base")?.string()?)
            || self
                .src
                .status(pr, tip, &context)
                .and_then(|s| string(s, "state"))
                != Some("SUCCESS")
        {
            return Ok(None);
        }
        self.land(refs, lock, row, m, remaining).map(Some)
    }
    /// Push the batch tip to the target in the same atomic push that closes the batch.
    fn land(
        &self,
        refs: &Refs,
        lock: &str,
        row: &Value,
        m: &[Member],
        remaining: &[Value],
    ) -> Result<bool> {
        let base = row.field("base")?.string()?;
        let tip = row.field("tip")?.string()?;
        let landed = split::counted(row, 0, m.len())?;
        let mut edits = self.finish_edits(refs, lock, &landed, m, remaining.to_vec(), &[])?;
        edits.push((self.target.clone(), base.into(), tip.into()));
        if let Err(e) = self.push(edits) {
            let why = format!(
                "{} refused lit's push: {}",
                self.src.branch,
                refusal(&e.to_string())
            );
            println!("Batch {} goes through the merge queue: {why}", short(row)?);
            self.change_record(lock, &with(row, "refused", Value::String(why))?)?;
            return Ok(true);
        }
        log_round(row, now().ok().as_deref(), true);
        let list = m
            .iter()
            .map(|(n, _)| format!("#{n}"))
            .collect::<Vec<_>>()
            .join(", ");
        println!(
            "Landed batch {} directly on {} with {} source PRs: {list}.",
            short(row)?,
            self.src.branch,
            m.len()
        );
        Ok(true)
    }
}

/// The line of a refused push that says why: what the remote said, else git's rejection line.
pub(super) fn refusal(said: &str) -> String {
    let said = said.strip_prefix("git push:").unwrap_or(said);
    let lines: Vec<&str> = said.lines().map(str::trim).collect();
    let remote = lines.iter().find_map(|l| {
        l.strip_prefix("remote:")
            .map(str::trim)
            .filter(|r| !r.is_empty())
    });
    let line = remote
        .or_else(|| lines.iter().copied().find(|l| l.contains("rejected]")))
        .or_else(|| lines.iter().rev().copied().find(|l| !l.is_empty()))
        .unwrap_or("");
    line.chars().take(200).collect()
}

fn short(row: &Value) -> Result<String> {
    Ok(row.field("tip")?.string()?.chars().take(12).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|p| (*p).to_owned()).collect()
    }
    #[test]
    fn a_refusal_names_what_the_remote_said() {
        let said = "git push: remote: \nremote: error: GH013: Repository rule violations found for refs/heads/main.\n ! [remote rejected] main -> main (push declined due to repository rule violations)\nerror: failed to push some refs to 'x'";
        assert_eq!(
            refusal(said),
            "error: GH013: Repository rule violations found for refs/heads/main."
        );
        assert_eq!(
            refusal("git push: ! [rejected] main -> main (stale info)\nerror: failed"),
            "! [rejected] main -> main (stale info)"
        );
    }
    #[test]
    fn disjoint_members_land_and_any_shared_file_goes_to_the_queue() {
        let clean = vec![
            (
                1,
                s(&["web/a.ts"]),
                s(&["docs/x.md"]),
                s(&["docs/x.md", "psa/b.sql"]),
            ),
            (2, s(&["psa/b.sql"]), s(&[]), s(&["web/a.ts"])),
        ];
        assert_eq!(
            overlap("main", &clean, &s(&["web/a.ts", "psa/b.sql"])),
            None
        );
        let mut shared = clean.clone();
        shared[1].1.push("web/a.ts".into());
        assert_eq!(
            overlap("main", &shared, &s(&[])).unwrap(),
            "#1 and #2 both changed web/a.ts"
        );
        let mut drift = clean.clone();
        drift[0].2.push("web/a.ts".into());
        assert_eq!(
            overlap("main", &drift, &s(&[])).unwrap(),
            "#1 and main both changed web/a.ts"
        );
        let mut lost = clean.clone();
        lost[1].3.push("psa/b.sql".into());
        assert_eq!(
            overlap("main", &lost, &s(&[])).unwrap(),
            "the batch does not keep #2's psa/b.sql"
        );
        assert_eq!(
            overlap("main", &clean, &s(&["web/a.ts", "ci.yml"])).unwrap(),
            "the batch changes ci.yml outside its PRs"
        );
    }
}
