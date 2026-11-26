use super::*;

#[derive(Clone)]
pub(super) struct Cut {
    pub lo: usize,
    pub hi: usize,
    pub fs: usize,
    pub fe: usize,
    pub name: Option<String>,
    pub kids: Vec<usize>,
}
impl Cut {
    pub fn new(lo: usize, hi: usize, name: Option<String>) -> Self {
        Self {
            lo,
            hi,
            fs: lo,
            fe: hi,
            name,
            kids: Vec::new(),
        }
    }
}
impl Parser<'_> {
    pub(super) fn before(&self, k: usize, n: usize) -> &str {
        self.v(k.wrapping_sub(n))
    }
    pub(super) fn plain(&self, k: usize) -> bool {
        self.kind(k) == Some(Kind::Id) && !reserved(self.v(k))
    }
    pub(super) fn angled(&self, i: usize, hi: usize) -> Option<usize> {
        let end = self.angle(i, hi);
        (end != i + 1).then_some(end)
    }
    pub(super) fn continues(&self, i: usize) -> bool {
        self.kind(i) == Some(Kind::Template) || continuation(self.v(i))
    }
    fn statement_start(&self, i: usize) -> bool {
        i == 0
            || matches!(self.before(i, 1), ";" | "{" | "}")
            || (self.nl(i) && !trailing(self.before(i, 1)) && !self.continues(i))
    }
    pub(super) fn items(&self, lo: usize, hi: usize) -> Vec<(usize, usize)> {
        let mut out = Vec::new();
        let mut start = lo;
        let mut i = lo;
        while i < hi {
            if opener(self.v(i)) {
                i = self.close(i) + 1;
                continue;
            }
            if self.v(i) == "," {
                out.push((start, i));
                start = i + 1;
            }
            i += 1;
        }
        if start < hi {
            out.push((start, hi));
        }
        out
    }
    pub(super) fn after_params(&self, mut j: usize, hi: usize) -> Option<(usize, &'static str)> {
        if self.v(j) == "<" {
            j = self.angled(j, hi)?;
        }
        if self.v(j) != "(" {
            return None;
        }
        let pe = self.close(j) + 1;
        let (pos, why) = self.scan(pe, hi, Mode::Body)?;
        if why == "{" && pos > pe {
            let k = pos - 1;
            if !(matches!(
                self.kind(k),
                Some(Kind::String | Kind::Number | Kind::Template)
            ) || (self.kind(k) == Some(Kind::Id)
                && !matches!(
                    self.v(k),
                    "is" | "extends" | "keyof" | "infer" | "asserts" | "readonly"
                ))
                || matches!(self.v(k), ")" | "]" | "}" | ">" | ">>" | ">>>"))
            {
                return None;
            }
        }
        matches!(why, "{" | ";").then_some((pos, why))
    }
    fn arrow_from(&self, i: usize) -> Option<(usize, BTreeSet<usize>)> {
        let n = self.t.len();
        let mut claims = BTreeSet::new();
        let mut j = i;
        if self.v(j) == "async"
            && j + 1 < n
            && !self.nl(j + 1)
            && (matches!(self.v(j + 1), "(" | "<") || (self.plain(j + 1) && self.v(j + 2) == "=>"))
        {
            j += 1;
            claims.insert(j);
        }
        if self.v(j) == "<" {
            j = self.angled(j, n)?;
            if self.v(j) != "(" {
                return None;
            }
            claims.insert(j);
        }
        let arrow = if self.v(j) == "(" {
            let pe = self.close(j) + 1;
            match self.v(pe) {
                "=>" => pe,
                ":" => {
                    if i > 0
                        && !matches!(
                            self.before(i, 1),
                            "=" | "("
                                | ","
                                | "["
                                | ";"
                                | "||"
                                | "&&"
                                | "??"
                                | "return"
                                | "default"
                                | "yield"
                                | "await"
                        )
                    {
                        return None;
                    }
                    let mut k = pe + 1;
                    loop {
                        if k >= n {
                            return None;
                        }
                        claims.insert(k);
                        match self.v(k) {
                            "(" | "[" | "{" => k = self.close(k) + 1,
                            ")" | "]" | "}" | ";" => return None,
                            "=>" => break k,
                            "<" => k = self.angle(k, n),
                            _ => k += 1,
                        }
                    }
                }
                _ => return None,
            }
        } else if self.plain(j)
            && self.v(j + 1) == "=>"
            && !(self.before(j, 1) == ":" && self.before(j, 2) == ")")
        {
            j + 1
        } else {
            return None;
        };
        (self.v(arrow + 1) == "{").then(|| (self.close(arrow + 1) + 1, claims))
    }
    fn function_at(&self, k: usize) -> Option<Cut> {
        let s = if self.before(k, 1) == "async"
            && !self.nl(k)
            && !matches!(self.before(k, 2), "." | "?.")
        {
            k - 1
        } else {
            k
        };
        let mut j = k + 1 + usize::from(self.v(k + 1) == "*");
        let name = if self.kind(j) == Some(Kind::Id) {
            j += 1;
            Some(self.v(j - 1))
        } else {
            None
        };
        let (pos, "{") = self.after_params(j, self.t.len())? else {
            return None;
        };
        let end = self.close(pos) + 1;
        let default = self.before(s, 1) == "default" && self.before(s, 2) == "export";
        let start = if default {
            s - 2
        } else if self.before(s, 1) == "export" {
            s - 1
        } else {
            s
        };
        let mut c = Cut::new(s, end, None);
        if (name.is_some() || default) && self.statement_start(start) {
            c.lo = start;
            c.name = if default {
                Some("default".into())
            } else {
                name.map(str::to_owned)
            };
        }
        Some(c)
    }
    fn class_candidates(&self, k: usize) -> Option<Vec<Cut>> {
        let mut j = k + 1;
        if self.kind(j) == Some(Kind::Id) && !matches!(self.v(j), "extends" | "implements") {
            j += 1;
        } else if self.kind(j) == Some(Kind::Punct) && !matches!(self.v(j), "{" | "<") {
            return None;
        }
        if self.v(j) == "<" {
            j = self.angled(j, self.t.len())?;
        }
        let (pos, "{") = self.scan(j, self.t.len(), Mode::Body)? else {
            return None;
        };
        let mut cuts = Vec::new();
        for u in self.merge_overloads(self.members(pos)?) {
            if u.signature || self.v(u.end - 1) != "}" {
                continue;
            }
            let (stat, key) = u
                .key
                .strip_prefix("static ")
                .map_or((false, u.key.as_str()), |s| (true, s));
            let name = if key == "constructor" && !stat {
                key.to_owned()
            } else {
                let Some((word, base)) = key.split_once(' ') else {
                    continue;
                };
                if !matches!(word, "method" | "get" | "set")
                    || base.is_empty()
                    || base.starts_with('[')
                {
                    continue;
                }
                format!(
                    "{}{}{base}",
                    if word == "method" {
                        ""
                    } else if word == "get" {
                        "get "
                    } else {
                        "set "
                    },
                    if stat { "static " } else { "" }
                )
            };
            cuts.push(Cut::new(u.first, u.end, Some(name)));
        }
        Some(cuts)
    }
    pub(super) fn method_at(&self, s: usize, e: usize) -> Option<String> {
        let mut j = s;
        let mut prefix = "";
        while j + 1 < e && matches!(self.v(j), "async" | "get" | "set") {
            let next = j + 1;
            if !matches!(
                self.kind(next),
                Some(Kind::Id | Kind::String | Kind::Number)
            ) && self.v(next) != "*"
            {
                break;
            }
            if self.v(j) == "get" {
                prefix = "get ";
            } else if self.v(j) == "set" {
                prefix = "set ";
            }
            j += 1;
        }
        j += usize::from(self.v(j) == "*");
        if j >= e || !matches!(self.kind(j), Some(Kind::Id | Kind::String | Kind::Number)) {
            return None;
        }
        let name = self.raw(j);
        j += 1;
        let (brace, "{") = self.after_params(j, e)? else {
            return None;
        };
        (self.close(brace) + 1 == e && !self.nl(brace)).then(|| format!("{prefix}{name}"))
    }
    fn named_variables(&self, cuts: &mut [Cut], by_lo: &BTreeMap<usize, usize>) {
        for k in 0..self.t.len() {
            if self.kind(k) != Some(Kind::Id) || matches!(self.before(k, 1), "." | "?.") {
                continue;
            }
            let (start, j, name) = if self.v(k) == "const" && self.plain(k + 1) {
                let start = k - usize::from(self.before(k, 1) == "export");
                let mut j = k + 2;
                if self.v(j) == ":" {
                    let Some((end, "=")) = self.scan(j + 1, self.t.len(), Mode::Type) else {
                        continue;
                    };
                    j = end;
                }
                if self.v(j) != "=" {
                    continue;
                }
                (start, j, self.v(k + 1))
            } else if self.v(k) == "default" && self.before(k, 1) == "export" {
                (k - 1, k, "default")
            } else {
                continue;
            };
            if !self.statement_start(start) {
                continue;
            }
            let Some(index) = by_lo.get(&(j + 1)) else {
                continue;
            };
            let c = &mut cuts[*index];
            if c.name.is_some() || c.lo != c.fs {
                continue;
            }
            let mut end = c.hi;
            if end < self.t.len() {
                if self.v(end) == ";" {
                    end += 1;
                } else if self.v(end) != "}" && (!self.nl(end) || self.continues(end)) {
                    continue;
                }
            }
            c.lo = start;
            c.hi = end;
            c.name = Some(name.into());
        }
    }
    pub(super) fn candidates(&self) -> Vec<Cut> {
        let mut found = Vec::new();
        let mut lookup = BTreeMap::new();
        let mut claimed = BTreeSet::new();
        for k in 0..self.t.len() {
            if !matches!(self.before(k, 1), "." | "?.") {
                if self.v(k) == "function" {
                    if let Some(c) = self.function_at(k) {
                        if c.name.is_none() {
                            lookup.insert(c.lo, found.len());
                        }
                        found.push(c);
                    }
                } else if self.v(k) == "class" {
                    found.extend(self.class_candidates(k).into_iter().flatten());
                }
            }
            if !claimed.contains(&k)
                && ((self.kind(k) == Some(Kind::Id)
                    && (self.v(k) == "async" || self.v(k + 1) == "=>"))
                    || matches!(self.v(k), "(" | "<"))
            {
                if let Some((end, claims)) = self.arrow_from(k) {
                    lookup.insert(k, found.len());
                    found.push(Cut::new(k, end, None));
                    claimed.extend(claims);
                }
            }
            if self.v(k) == "{"
                && matches!(
                    self.before(k, 1),
                    "=" | "("
                        | ","
                        | ":"
                        | "["
                        | "?"
                        | "||"
                        | "&&"
                        | "??"
                        | "..."
                        | "||="
                        | "&&="
                        | "??="
                        | "return"
                        | "default"
                        | "yield"
                        | "await"
                        | "typeof"
                )
            {
                let close = self.close(k);
                for (s, e) in self.items(k + 1, close) {
                    if let Some(name) = self.method_at(s, e) {
                        let mut c = Cut::new(s, e, Some(name));
                        c.hi += usize::from(e < close && self.v(e) == ",");
                        found.push(c);
                    }
                }
            }
        }
        self.named_variables(&mut found, &lookup);
        found
    }
}
