use super::*;
#[derive(Default)]
pub(super) struct Module {
    pub imports: BTreeMap<String, BTreeMap<String, String>>,
    pub known: BTreeMap<usize, usize>,
    pub deps: BTreeSet<String>,
    pub exports: BTreeSet<String>,
    pub star: bool,
    declared: BTreeSet<String>,
    locals: Vec<(String, String)>,
    pub cuts: Vec<Cut>,
}
impl Parser<'_> {
    fn boundary(&self, k: usize) -> bool {
        k == 0 || matches!(self.before(k, 1), ";" | "}") || self.nl(k)
    }
    fn statement_end(&self, k: usize) -> Option<usize> {
        if self.v(k) == ";" {
            Some(k + 1)
        } else if k >= self.t.len() || self.nl(k) {
            Some(k)
        } else {
            None
        }
    }
    fn attributes(&self, j: usize) -> usize {
        if matches!(self.v(j), "with" | "assert") && self.v(j + 1) == "{" {
            self.close(j + 1) + 1
        } else {
            j
        }
    }
    fn spec_name(&self, i: usize) -> Option<&str> {
        match self.kind(i)? {
            Kind::Id => Some(self.v(i)),
            Kind::String => {
                let s = self.raw(i);
                s.get(1..s.len() - 1)
            }
            _ => None,
        }
    }
    fn specs(&self, k: usize) -> Option<Vec<(String, String, bool)>> {
        let mut out = Vec::new();
        for (s, e) in self.items(k + 1, self.close(k)) {
            let w = (s..e)
                .map(|i| self.spec_name(i))
                .collect::<Option<Vec<_>>>()?;
            let (a, b, ty) = if self.v(s) == "type" && w.len() > 1 {
                match w.as_slice() {
                    [_, b] if *b != "as" => (*b, *b, true),
                    [_, a, "as", b] if *a != "as" => (*a, *b, true),
                    _ => return None,
                }
            } else {
                match w.as_slice() {
                    [a] => (*a, *a, false),
                    [a, "as", b] if self.kind(s + 1) == Some(Kind::Id) => (*a, *b, false),
                    _ => return None,
                }
            };
            out.push((a.into(), b.into(), ty));
        }
        Some(out)
    }
    fn read_import(&self, k: usize, m: &mut Module) -> Option<usize> {
        if !self.boundary(k) {
            return None;
        }
        let mut j = k + 1;
        let mut erased = false;
        let mut bindings = BTreeMap::new();
        let source = if self.kind(j) == Some(Kind::String) {
            j += 1;
            self.spec_name(j - 1)?
        } else {
            if self.v(j) == "type" {
                if matches!(self.v(j + 1), "{" | "*")
                    || (self.kind(j + 1) == Some(Kind::Id) && self.v(j + 1) != "from")
                {
                    erased = true;
                    j += 1;
                } else if self.v(j + 1) == "from" {
                    if self.kind(j + 2) != Some(Kind::String) {
                        if self.v(j + 2) != "from" || self.kind(j + 3) != Some(Kind::String) {
                            return None;
                        }
                        erased = true;
                        j += 1;
                    }
                } else {
                    return None;
                }
            }
            if self.plain(j) && matches!(self.v(j + 1), "," | "from") {
                bindings.insert(self.v(j).into(), "default".into());
                j += 1 + usize::from(self.v(j + 1) == ",");
            }
            if self.v(j) == "*" {
                if self.v(j + 1) != "as" || self.kind(j + 2) != Some(Kind::Id) {
                    return None;
                }
                bindings.insert(self.v(j + 2).into(), "*".into());
                j += 3;
            } else if self.v(j) == "{" {
                for (imported, local, ty) in self.specs(j)? {
                    if !ty {
                        bindings.insert(local, imported);
                    }
                }
                j = self.close(j) + 1;
            }
            if self.v(j) != "from" || self.kind(j + 1) != Some(Kind::String) {
                return None;
            }
            j += 2;
            self.spec_name(j - 1)?
        };
        let end = self.statement_end(self.attributes(j))?;
        m.cuts.push(Cut::new(k, end, Some("import".into())));
        m.known.insert(k, end);
        if !erased {
            m.imports.entry(source.into()).or_default().extend(bindings);
            m.deps.insert(source.into());
        }
        Some(end)
    }
    fn read_export(&self, k: usize, m: &mut Module) -> Option<usize> {
        if !self.boundary(k) {
            return None;
        }
        let mut j = k + 1;
        let word = self.v(j);
        match word {
            "import" => return None,
            "default" => {
                if self.v(j + 1) != "interface" {
                    m.exports.insert("default".into());
                }
                return Some(k + 1);
            }
            "=" => {
                m.star = true;
                return Some(k + 1);
            }
            _ => {}
        }
        let ty = word == "type" && matches!(self.v(j + 1), "{" | "*");
        j += usize::from(ty);
        if self.v(j) == "*" {
            j += 1;
            let name = if self.v(j) == "as" {
                let n = self.spec_name(j + 1)?;
                j += 2;
                Some(n)
            } else {
                None
            };
            if self.v(j) != "from" || self.kind(j + 1) != Some(Kind::String) {
                return None;
            }
            let source = self.spec_name(j + 1)?;
            let end = self.statement_end(self.attributes(j + 2))?;
            if !ty {
                m.deps.insert(source.into());
                if let Some(n) = name {
                    m.exports.insert(n.into());
                } else {
                    m.star = true;
                }
            }
            m.known.insert(k, end);
            return Some(end);
        }
        if self.v(j) == "{" {
            let specs = self.specs(j)?;
            j = self.close(j) + 1;
            let source = if self.v(j) == "from" {
                if self.kind(j + 1) != Some(Kind::String) {
                    return None;
                }
                j += 2;
                Some(self.spec_name(j - 1)?)
            } else {
                None
            };
            let end = self.statement_end(self.attributes(j))?;
            if !ty {
                if let Some(s) = source {
                    m.deps.insert(s.into());
                } else {
                    m.locals.extend(
                        specs
                            .into_iter()
                            .filter(|(_, _, ty)| !*ty)
                            .map(|(a, b, _)| (a, b)),
                    );
                }
            }
            m.known.insert(k, end);
            return Some(end);
        }
        if (word == "async" && self.v(j + 1) == "function")
            || (word == "abstract" && self.v(j + 1) == "class")
        {
            j += 1;
        }
        if matches!(
            self.v(j),
            "function" | "class" | "enum" | "const" | "let" | "var"
        ) {
            j += 1 + usize::from(self.v(j) == "function" && self.v(j + 1) == "*");
            if self.plain(j) {
                m.exports.insert(self.v(j).into());
            }
        }
        Some(k + 1)
    }
    pub(super) fn module(&self) -> Option<Module> {
        let mut m = Module::default();
        let mut k = 0;
        while k < self.t.len() {
            let v = self.v(k);
            let before = self.before(k, 1);
            if v == "import" && !matches!(before, "." | "?.") && !matches!(self.v(k + 1), "(" | ".")
            {
                k = self.read_import(k, &mut m)?;
            } else if v == "export" && !matches!(before, "." | "?.") {
                k = self.read_export(k, &mut m)?;
            } else {
                if matches!(v, "function" | "class" | "const" | "let" | "var" | "enum")
                    && (k == 0
                        || matches!(
                            before,
                            ";" | "}" | "export" | "default" | "async" | "abstract"
                        )
                        || self.nl(k))
                    && before != "declare"
                    && !(v == "enum" && before == "const")
                {
                    let j = k + 1 + usize::from(self.v(k + 1) == "*");
                    if self.plain(j) {
                        m.declared.insert(self.v(j).into());
                    }
                }
                k = if opener(v) { self.close(k) + 1 } else { k + 1 };
            }
        }
        for (local, name) in &m.locals {
            if m.declared.contains(local) {
                m.exports.insert(name.clone());
            }
        }
        Some(m)
    }
}
