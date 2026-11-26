use super::*;

pub(super) struct Contents<'a> {
    repo: &'a Repo,
    trees: BTreeMap<String, BTreeMap<String, String>>,
    symbols: BTreeMap<String, Option<Value>>,
}
impl<'a> Contents<'a> {
    pub fn new(repo: &'a Repo) -> Self {
        Self {
            repo,
            trees: BTreeMap::new(),
            symbols: BTreeMap::new(),
        }
    }
    pub fn tree(&mut self, rev: &str) -> Result<&BTreeMap<String, String>> {
        if rev.starts_with('-') {
            return Err("Invalid snapshot revision".into());
        }
        if !self.trees.contains_key(rev) {
            let mut entries = BTreeMap::new();
            for row in self
                .repo
                .bytes(&["ls-tree", "-r", "-z", "--full-tree", rev], None)?
                .split(|b| *b == 0)
                .filter(|p| !p.is_empty())
            {
                let i = row
                    .iter()
                    .position(|b| *b == b'\t')
                    .ok_or("Invalid ls-tree entry")?;
                let oid = std::str::from_utf8(&row[..i])
                    .map_err(|e| e.to_string())?
                    .split_whitespace()
                    .nth(2)
                    .ok_or("Missing blob id")?;
                entries.insert(paths::path_key(&row[i + 1..]), oid.into());
            }
            self.trees.insert(rev.into(), entries);
        }
        Ok(&self.trees[rev])
    }
    pub fn digest(&mut self, rev: &str, key: &str) -> Result<Option<String>> {
        let (p, symbol) = key
            .split_once('#')
            .map_or((key, None), |(p, s)| (p, Some(s)));
        let oid = self.tree(rev)?.get(p).cloned();
        let (Some(oid), Some(symbol)) = (oid.as_deref(), symbol) else {
            return Ok(oid);
        };
        if !self.symbols.contains_key(oid) {
            let data = self.repo.bytes(&["cat-file", "blob", oid], None)?;
            let suffix = p
                .rsplit_once('.')
                .map_or_else(String::new, |(_, ext)| format!(".{}", ext.to_lowercase()));
            let table = crate::readers::symbols::table(&data, &suffix)
                .map(|t| paths::parse(crate::json::pretty(&t).as_bytes()))
                .transpose()?
                .map(|t| super::super::symbols::digests(&t))
                .transpose()?;
            self.symbols.insert(oid.into(), table);
        }
        Ok(match &self.symbols[oid] {
            Some(table) => store::value(table.get(symbol)),
            None => Some(format!("unreadable:{oid}")),
        })
    }
    pub fn writes(
        &mut self,
        base: &str,
        head: &str,
        files: &BTreeMap<String, Vec<String>>,
    ) -> Result<Inputs> {
        if base.starts_with('-') || head.starts_with('-') {
            return Err("Invalid snapshot revision".into());
        }
        let fork = self.repo.text(&["merge-base", base, head], None)?;
        let changed = self.repo.bytes(
            &[
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--name-only",
                "-z",
                &fork,
                head,
                "--",
            ],
            None,
        )?;
        let mut writes = Inputs::new();
        for path in changed.split(|b| *b == 0).filter(|p| !p.is_empty()) {
            let path = paths::path_key(path);
            let keys = files
                .get(&path)
                .map(Vec::as_slice)
                .unwrap_or(std::slice::from_ref(&path));
            for k in keys {
                let before = self.digest(&fork, k)?;
                let mut after = self.digest(head, k)?;
                if before == after {
                    continue;
                }
                if self.digest(base, k)? != before {
                    after = Some(format!("merged:{base}:{head}:{k}"));
                }
                writes.insert(k.clone(), after);
            }
        }
        Ok(writes)
    }
    pub fn discovered(&mut self, rev: &str, patterns: &[Pattern], strip: &str) -> Result<Units> {
        if patterns.is_empty() {
            return Ok(Units::new());
        }
        Ok(self
            .tree(rev)?
            .keys()
            .filter(|p| patterns.iter().any(|pat| pat.matches(p)))
            .filter_map(|p| p.strip_prefix(strip).map(str::to_owned))
            .collect())
    }
}
