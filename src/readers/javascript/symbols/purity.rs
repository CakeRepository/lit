use super::*;
impl Parser<'_> {
    pub(super) fn pure(&self, m: &Module) -> bool {
        let mut k = 0;
        let mut prologue = true;
        let n = self.t.len();
        while k < n {
            if self.v(k) == ";" {
                k += 1;
                continue;
            }
            if prologue && self.kind(k) == Some(Kind::String) {
                k += 1;
                if self.v(k) == ";" {
                    k += 1;
                } else if k < n && (!self.nl(k) || self.continues(k)) {
                    return false;
                }
                continue;
            }
            prologue = false;
            let Some(end) = m.known.get(&k).copied().or_else(|| self.pure_statement(k)) else {
                return false;
            };
            if end <= k {
                return false;
            }
            k = end;
        }
        true
    }
    fn pure_statement(&self, k: usize) -> Option<usize> {
        let n = self.t.len();
        let mut j = k;
        let mut default = false;
        if self.v(j) == "export" {
            j += 1;
            if self.v(j) == "default" {
                default = true;
                j += 1;
            }
        }
        let declare =
            self.v(j) == "declare" && self.kind(j + 1) == Some(Kind::Id) && !self.nl(j + 1);
        j += usize::from(declare);
        let word = self.v(j);
        let next = self.v(j + 1);
        if word == "function" || (word == "async" && next == "function" && !self.nl(j + 1)) {
            let mut a = j + 1 + usize::from(word == "async");
            a += usize::from(self.v(a) == "*");
            a += usize::from(self.kind(a) == Some(Kind::Id));
            let (p, w) = self.after_params(a, n)?;
            return Some(if w == "{" { self.close(p) + 1 } else { p });
        }
        if word == "class" || (word == "abstract" && next == "class") {
            return self.class_end(j + usize::from(word == "abstract"), 0);
        }
        if default && word != "interface" {
            let (end, w) = self.scan(j, n, Mode::Field)?;
            return self.inert(j, end - usize::from(w == ";"), 0).then_some(end);
        }
        if word == "interface" && self.kind(j + 1) == Some(Kind::Id) && !self.nl(j + 1) {
            let (pos, "{") = self.scan(j + 2, n, Mode::Body)? else {
                return None;
            };
            return Some(self.close(pos) + 1);
        }
        if word == "type"
            && self.kind(j + 1) == Some(Kind::Id)
            && !self.nl(j + 1)
            && matches!(self.v(j + 2), "=" | "<")
        {
            let end = self.scan(j + 2, n, Mode::Statement)?.0;
            let mut q = j + 3;
            while q < end {
                if self.nl(q)
                    && (matches!(self.v(q), "(" | "[") || self.kind(q) == Some(Kind::Template))
                    && !trailing(self.before(q, 1))
                {
                    return None;
                }
                q = if opener(self.v(q)) {
                    self.close(q) + 1
                } else {
                    q + 1
                };
            }
            return Some(end);
        }
        if word == "enum" || (word == "const" && next == "enum") {
            let e = j + usize::from(word == "const");
            if self.kind(e + 1) != Some(Kind::Id) || self.v(e + 2) != "{" {
                return None;
            }
            let end = self.close(e + 2);
            for (a, b) in self.items(e + 3, end) {
                if !matches!(self.kind(a), Some(Kind::Id | Kind::String))
                    || (b > a + 1 && !(self.v(a + 1) == "=" && self.inert(a + 2, b, 0)))
                {
                    return None;
                }
            }
            return Some(end + 1);
        }
        if declare && matches!(word, "namespace" | "module") {
            let mut i = j + 1;
            while i < n
                && !matches!(self.v(i), "{" | ";")
                && (matches!(self.kind(i), Some(Kind::Id | Kind::String)) || self.v(i) == ".")
            {
                i += 1;
            }
            return match self.v(i) {
                "{" => Some(self.close(i) + 1),
                ";" => Some(i + 1),
                _ => None,
            };
        }
        if declare && word == "global" && next == "{" {
            return Some(self.close(j + 1) + 1);
        }
        if matches!(word, "const" | "let") {
            return self.variables_end(j);
        }
        None
    }
    fn class_end(&self, k: usize, depth: usize) -> Option<usize> {
        if depth > 300 {
            return None;
        }
        let n = self.t.len();
        let mut j = k + 1;
        if self.kind(j) == Some(Kind::Id) && !matches!(self.v(j), "extends" | "implements") {
            j += 1;
        }
        if self.v(j) == "<" {
            j = self.angled(j, n)?;
        }
        let (pos, "{") = self.scan(j, n, Mode::Body)? else {
            return None;
        };
        let mut i = j;
        if self.v(i) == "extends" {
            if !self.plain(i + 1) {
                return None;
            }
            i += 2;
            if self.v(i) == "<" {
                i = self.angled(i, pos)?;
            }
        }
        if self.v(i) == "implements" {
            i += 1;
            while i < pos {
                if self.v(i) == "<" {
                    i = self.angled(i, pos)?;
                    continue;
                }
                if self.kind(i) != Some(Kind::Id) && !matches!(self.v(i), "." | ",") {
                    return None;
                }
                i += 1;
            }
        }
        if i != pos {
            return None;
        }
        for u in self.merge_overloads(self.members(pos)?) {
            if self.v(u.first) == "@" || u.key.starts_with("static block") {
                return None;
            }
            let rest = u.key.strip_prefix("static ").unwrap_or(&u.key);
            if rest != "constructor" {
                let (_, base) = rest.split_once(' ')?;
                if base.is_empty() || base.starts_with('[') {
                    return None;
                }
            }
            if u.key.starts_with("static field ") {
                let end = u.end - usize::from(self.v(u.end - 1) == ";");
                let mut q = u.first;
                while q < end {
                    if self.v(q) == "=" {
                        if !self.inert(q + 1, end, depth + 1) {
                            return None;
                        }
                        break;
                    }
                    q = if opener(self.v(q)) {
                        self.close(q) + 1
                    } else {
                        q + 1
                    };
                }
            }
        }
        Some(self.close(pos) + 1)
    }
    fn variables_end(&self, j: usize) -> Option<usize> {
        let mut k = j + 1;
        loop {
            if self.kind(k) != Some(Kind::Id) {
                return None;
            }
            k += 1;
            k += usize::from(self.v(k) == "!");
            let (mut pos, mut why) = if self.v(k) == ":" {
                self.scan(k + 1, self.t.len(), Mode::Type)?
            } else {
                (k, "at")
            };
            if matches!(why, "at" | "=") && self.v(pos) == "=" {
                let start = pos + 1;
                (pos, why) = self.scan(start, self.t.len(), Mode::Initializer)?;
                if !self.inert(start, pos - usize::from(why == ";"), 0) {
                    return None;
                }
            } else if why == "at" {
                if self.v(pos) == "," && self.comma(pos, self.t.len()) {
                    why = ",";
                } else if self.v(pos) == ";" {
                    pos += 1;
                } else if pos < self.t.len() && !self.nl(pos) {
                    return None;
                }
            }
            if why != "," {
                return Some(pos);
            }
            k = pos + 1;
        }
    }
    fn arrow_value(&self, a: usize, b: usize) -> bool {
        let mut j = a;
        if self.v(j) == "async" && self.v(j + 1) != "=>" && !self.nl(j + 1) {
            j += 1;
        }
        if self.v(j) == "<" {
            let Some(i) = self.angled(j, b) else {
                return false;
            };
            j = i;
        }
        if self.v(j) == "(" {
            let pe = self.close(j) + 1;
            if self.v(pe) == "=>" {
                return pe + 1 < b;
            }
            if self.v(pe) == ":" {
                let mut k = pe + 1;
                while k < b {
                    if self.v(k) == "=>" {
                        return k + 1 < b;
                    }
                    k = if opener(self.v(k)) {
                        self.close(k) + 1
                    } else {
                        k + 1
                    };
                }
            }
            return false;
        }
        self.plain(j) && self.v(j + 1) == "=>" && j + 2 < b
    }
    fn type_only(&self, a: usize, b: usize) -> bool {
        a < b
            && (a..b).all(|i| match self.kind(i) {
                Some(Kind::Id) => !matches!(
                    self.v(i),
                    "instanceof"
                        | "in"
                        | "new"
                        | "delete"
                        | "void"
                        | "await"
                        | "yield"
                        | "function"
                        | "class"
                        | "async"
                ),
                Some(Kind::String | Kind::Number) => true,
                Some(Kind::Punct) => matches!(
                    self.v(i),
                    "." | "|" | "&" | "," | "<" | ">" | ">>" | ">>>" | "[" | "]" | "?" | ":"
                ),
                _ => false,
            })
    }
    fn inert(&self, a: usize, b: usize, depth: usize) -> bool {
        if a >= b || depth > 300 {
            return false;
        }
        if self.arrow_value(a, b) {
            return true;
        }
        if b > a + 1 && self.v(b - 1) == "!" {
            return self.inert(a, b - 1, depth + 1);
        }
        let mut i = a + 1;
        while i < b {
            if matches!(self.v(i), "as" | "satisfies") && !matches!(self.before(i, 1), "." | "?.") {
                return self.inert(a, i, depth + 1) && self.type_only(i + 1, b);
            }
            i = if opener(self.v(i)) {
                self.close(i) + 1
            } else {
                i + 1
            };
        }
        let one = b == a + 1;
        let word = self.v(a);
        match self.kind(a) {
            Some(Kind::String | Kind::Number | Kind::Regex) => one,
            Some(Kind::Template) => one && !self.raw(a).contains("${"),
            Some(Kind::Id) => match word {
                "true" | "false" | "null" => one,
                _ if word == "function" || (word == "async" && self.v(a + 1) == "function") => {
                    let mut j = a + 1 + usize::from(word == "async");
                    j += usize::from(self.v(j) == "*");
                    j += usize::from(self.kind(j) == Some(Kind::Id));
                    self.after_params(j, b)
                        .is_some_and(|(pos, w)| w == "{" && self.close(pos) + 1 == b)
                }
                "class" => self.class_end(a, depth + 1) == Some(b),
                "new" if matches!(self.v(a + 1), "Map" | "Set" | "WeakMap" | "WeakSet") => {
                    let mut j = a + 2;
                    if self.v(j) == "<" {
                        let Some(k) = self.angled(j, b) else {
                            return false;
                        };
                        j = k;
                    }
                    self.v(j) == "(" && self.close(j) == j + 1 && j + 2 == b
                }
                _ => one && !reserved(word),
            },
            Some(Kind::Punct) => match word {
                "[" if self.close(a) + 1 == b => self
                    .items(a + 1, b - 1)
                    .into_iter()
                    .all(|(s, e)| s < e && self.v(s) != "..." && self.inert(s, e, depth + 1)),
                "{" if self.close(a) + 1 == b => {
                    self.items(a + 1, b - 1).into_iter().all(|(s, e)| {
                        if s >= e || matches!(self.v(s), "..." | "[") {
                            return false;
                        }
                        if matches!(self.kind(s), Some(Kind::Id | Kind::String | Kind::Number))
                            && self.v(s + 1) == ":"
                        {
                            self.inert(s + 2, e, depth + 1)
                        } else {
                            (e == s + 1 && self.plain(s)) || self.method_at(s, e).is_some()
                        }
                    })
                }
                "-" => self.inert(a + 1, b, depth + 1),
                _ => false,
            },
            _ => false,
        }
    }
}
