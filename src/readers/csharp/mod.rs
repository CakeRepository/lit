//! C# declarations and overload keys. Ambiguous declarations decline the file.
mod lexer;
use super::Node;
use lexer::{Kind, Token};

fn modifier(s: &str) -> bool {
    matches!(
        s,
        "public"
            | "private"
            | "protected"
            | "internal"
            | "static"
            | "partial"
            | "sealed"
            | "abstract"
            | "readonly"
            | "file"
            | "new"
            | "unsafe"
            | "extern"
            | "virtual"
            | "override"
            | "async"
            | "volatile"
            | "required"
            | "const"
            | "ref"
    )
}
fn join<'a>(words: impl IntoIterator<Item = &'a str>) -> String {
    let mut out = String::new();
    let mut prev = false;
    let word = |c: char| c.is_alphanumeric() || matches!(c, '_' | '@');
    for w in words {
        if prev && w.starts_with(word) {
            out.push(' ');
        }
        out.push_str(w);
        prev = w.ends_with(word);
    }
    out
}
fn arity(n: usize) -> String {
    if n > 0 {
        format!("`{n}")
    } else {
        String::new()
    }
}
struct Parser<'a> {
    s: &'a str,
    t: Vec<Token<'a>>,
    base: usize,
    file_scoped: bool,
}
#[derive(PartialEq, Clone, Copy)]
enum Scope<'a> {
    File,
    Namespace,
    Type(&'a str),
}
impl<'a> Parser<'a> {
    fn v(&self, i: usize) -> &'a str {
        self.t.get(i).map_or("", |t| t.text)
    }
    fn word(&self, i: usize) -> bool {
        self.t.get(i).is_some_and(|t| t.k == Kind::Word)
    }
    fn open(&self, i: usize) -> bool {
        matches!(self.v(i), "(" | "[" | "{")
    }
    fn close(&self, i: usize) -> usize {
        self.t[i].mate
    }
    fn after(&self, i: usize) -> usize {
        if self.open(i) {
            self.close(i) + 1
        } else {
            i + 1
        }
    }
    fn join(&self, start: usize, end: usize) -> String {
        join(self.t[start..end].iter().map(|t| t.text))
    }
    fn node(&self, key: String, first: usize, last: usize, children: Vec<Node>) -> Node {
        let mut start = self.t[first].start;
        let mut end = self.t[last].end;
        let line = self.s[..start].rfind('\n').map_or(0, |i| i + 1);
        if self.s[line..start].chars().all(lexer::whitespace) {
            start = line;
        }
        if let Some(n) = self.s[end..].find('\n') {
            if self.s[end..end + n].chars().all(lexer::whitespace) {
                end += n + 1;
            }
        }
        Node {
            key: key.encode_utf16().collect(),
            start: start + self.base,
            end: end + self.base,
            children,
        }
    }
    fn attrs(&self, mut i: usize) -> usize {
        while self.v(i) == "[" {
            i = self.close(i) + 1;
        }
        i
    }
    fn mods(&self, mut i: usize) -> usize {
        while modifier(self.v(i)) {
            i += 1;
        }
        i
    }
    fn head(&self, mut i: usize, limit: usize, terms: &[&str]) -> Option<(Vec<usize>, usize)> {
        let mut ids = Vec::new();
        while i < limit {
            if terms.contains(&self.v(i)) {
                return Some((ids, i));
            }
            ids.push(i);
            i = self.after(i);
        }
        None
    }
    fn semi(&self, mut i: usize, limit: usize) -> Option<usize> {
        while i < limit {
            if self.v(i) == ";" {
                return Some(i);
            }
            i = self.after(i);
        }
        None
    }
    fn type_start(&self, i: usize) -> bool {
        match self.v(i) {
            "class" | "struct" | "interface" | "enum" => self.word(i + 1),
            "record" => self.word(i + 1 + usize::from(matches!(self.v(i + 1), "class" | "struct"))),
            "delegate" => !matches!(self.v(i + 1), "{" | ""),
            _ => false,
        }
    }
    fn types(&self, group: usize) -> String {
        let end = self.close(group);
        let mut parts = vec![vec![]];
        let mut angle = 0usize;
        let mut j = group + 1;
        while j < end {
            match self.v(j) {
                "<" => angle += 1,
                ">" => angle = angle.saturating_sub(1),
                "," if angle == 0 => {
                    parts.push(Vec::new());
                    j += 1;
                    continue;
                }
                "=" if angle == 0 => {
                    j += 1;
                    while j < end && self.v(j) != "," {
                        j = self.after(j);
                    }
                    continue;
                }
                _ => (),
            }
            parts.last_mut().unwrap().push(j);
            j = self.after(j);
        }
        let mut names = Vec::new();
        for part in &parts {
            if part.is_empty() {
                continue;
            }
            let mut items = part.as_slice();
            while let Some((&i, tail)) = items.split_first() {
                if self.v(i) != "[" {
                    break;
                }
                items = tail;
            }
            while let Some((&i, tail)) = items.split_first() {
                if !matches!(self.v(i), "this" | "params" | "scoped") {
                    break;
                }
                items = tail;
            }
            if items.len() > 1 && self.word(*items.last().unwrap()) {
                items = &items[..items.len() - 1];
            }
            names.push(join(
                items
                    .iter()
                    .flat_map(|&i| (i..self.after(i)).map(|x| self.v(x))),
            ));
        }
        names.join(",")
    }
    fn back(&self, ids: &[usize], end: usize) -> Option<(usize, usize)> {
        let mut depth = 0i32;
        let mut commas = 0;
        for q in (0..=end).rev() {
            match self.v(ids[q]) {
                ">" => depth += 1,
                "<" => {
                    depth -= 1;
                    if depth == 0 {
                        return Some((q, commas));
                    }
                }
                "," if depth == 1 => commas += 1,
                _ => (),
            }
        }
        None
    }
    fn name(&self, ids: &[usize], mut pos: usize) -> Option<(String, usize, usize)> {
        let mut n = 0;
        if self.v(*ids.get(pos)?) == ">" {
            let (q, commas) = self.back(ids, pos)?;
            n = commas + 1;
            pos = q.checked_sub(1)?;
        }
        if !self.word(ids[pos]) {
            return None;
        }
        let mut r = pos;
        while r >= 2 && self.v(ids[r - 1]) == "." {
            r -= 2;
            if self.v(ids[r]) == ">" {
                r = self.back(ids, r)?.0.checked_sub(1)?;
            }
            if !self.word(ids[r]) {
                return None;
            }
        }
        Some((self.join(ids[r], ids[pos] + 1), n, r))
    }
    fn declarators(&self, mut i: usize, end: usize) -> Option<String> {
        let mut names = Vec::new();
        let mut angle = 0usize;
        while i <= end {
            match self.v(i) {
                "<" => angle += 1,
                ">" => angle = angle.saturating_sub(1),
                s if self.word(i)
                    && angle == 0
                    && matches!(self.v(i + 1), "=" | "," | ";")
                    && (names.is_empty() || self.v(i - 1) == ",") =>
                {
                    names.push(s)
                }
                _ => (),
            }
            i = self.after(i);
        }
        (!names.is_empty()).then(|| names.join(","))
    }
    fn directive(&self, i: usize, hi: usize) -> Option<usize> {
        if self.v(i) == "extern" && self.v(i + 1) == "alias" {
            return self.semi(i, hi);
        }
        let mut j = i + usize::from(self.v(i) == "global" && self.v(i + 1) == "using");
        if self.v(j) != "using" {
            return None;
        }
        j += 1;
        if self.v(j) == "static" && self.word(j + 1) {
            j += 1;
        }
        if !self.word(j) {
            return None;
        }
        if self.v(j + 1) == "=" {
            return self.semi(j, hi);
        }
        while matches!(self.v(j + 1), "." | "::") && self.word(j + 2) {
            j += 2;
        }
        (self.v(j + 1) == ";").then_some(j + 1)
    }
    fn statement(&self, i: usize, limit: usize) -> Option<usize> {
        let control = matches!(
            self.v(i),
            "if" | "else"
                | "for"
                | "foreach"
                | "while"
                | "do"
                | "switch"
                | "try"
                | "catch"
                | "finally"
                | "using"
                | "lock"
                | "unsafe"
                | "fixed"
                | "checked"
                | "unchecked"
        );
        let mut assigned = false;
        let mut j = i;
        while j < limit {
            match self.v(j) {
                "{" => {
                    let end = self.close(j);
                    if j == i {
                        return Some(end + 1);
                    }
                    let prev = self.v(j - 1);
                    j = end + 1;
                    let next = self.v(j);
                    if matches!(next, ";" | "." | "," | ")" | "]" | "}" | "?" | ":") {
                        continue;
                    }
                    if control {
                        if matches!(next, "else" | "catch" | "finally" | "while") {
                            continue;
                        }
                        return Some(j);
                    }
                    if prev == ")" && !assigned {
                        return Some(j);
                    }
                }
                ";" => return Some(j + 1),
                "=" | "=>" => {
                    assigned = true;
                    j += 1;
                }
                _ => j = self.after(j),
            }
        }
        None
    }
    fn scope(
        &mut self,
        mut i: usize,
        hi: usize,
        scope: Scope<'a>,
        depth: usize,
    ) -> Option<Vec<Node>> {
        if depth > 320 {
            return None;
        }
        let mut nodes = Vec::new();
        while i < hi {
            if !matches!(scope, Scope::Type(_)) {
                if let Some(end) = self.directive(i, hi) {
                    nodes.push(self.node(self.join(i, end), i, end, vec![]));
                    i = end + 1;
                    continue;
                }
                if scope == Scope::File
                    && self.v(i) == "["
                    && matches!(self.v(i + 1), "assembly" | "module")
                    && self.v(i + 2) == ":"
                {
                    let end = self.close(i);
                    nodes.push(self.node(
                        format!("attribute {}", self.join(i, end + 1)),
                        i,
                        end,
                        vec![],
                    ));
                    i = end + 1;
                    continue;
                }
            }
            let j = self.attrs(i);
            let m = self.mods(j);
            let (key, end, children) =
                if !matches!(scope, Scope::Type(_)) && self.v(m) == "namespace" && self.word(m + 1)
                {
                    let (entries, term) = self.head(m + 1, hi, &["{", ";"])?;
                    if !self.word(*entries.first()?)
                        || !entries.iter().all(|&x| self.word(x) || self.v(x) == ".")
                    {
                        return None;
                    }
                    let key = format!("namespace {}", self.join(m + 1, term));
                    if self.v(term) == ";" {
                        if scope != Scope::File {
                            return None;
                        }
                        self.file_scoped = true;
                        (key, term, vec![])
                    } else {
                        let end = self.close(term);
                        let kids = self.scope(term + 1, end, Scope::Namespace, depth + 1)?;
                        (key, self.with_semi(end, hi), kids)
                    }
                } else if self.type_start(m) {
                    if self.v(m) == "delegate" {
                        let (entries, term) = self.head(m + 1, hi, &["{", ";"])?;
                        if self.v(term) == "{" {
                            if scope != Scope::File || self.file_scoped {
                                return None;
                            }
                            i = self.statement(i, hi)?;
                            continue;
                        }
                        let core = self.cut(&entries);
                        if core.len() < 3 || self.v(*core.last()?) != "(" {
                            return None;
                        }
                        let (name, n, r) = self.name(core, core.len() - 2)?;
                        if r < 1 {
                            return None;
                        }
                        (format!("delegate {name}{}", arity(n)), term, vec![])
                    } else {
                        self.type_decl(m, hi, depth)?
                    }
                } else if let Scope::Type(name) = scope {
                    let (key, end) = self.member(j, hi, name)?;
                    (key, end, vec![])
                } else {
                    if scope == Scope::Namespace || self.file_scoped {
                        return None;
                    }
                    i = self.statement(i, hi)?;
                    continue;
                };
            nodes.push(self.node(key, i, end, children));
            i = end + 1;
        }
        Some(nodes)
    }
    fn with_semi(&self, end: usize, hi: usize) -> usize {
        end + usize::from(end + 1 < hi && self.v(end + 1) == ";")
    }
    fn cut<'b>(&self, ids: &'b [usize]) -> &'b [usize] {
        let n = ids
            .iter()
            .position(|&i| matches!(self.v(i), "where" | ":"))
            .unwrap_or(ids.len());
        &ids[..n]
    }
    fn type_decl(
        &mut self,
        m: usize,
        hi: usize,
        depth: usize,
    ) -> Option<(String, usize, Vec<Node>)> {
        let word = self.v(m);
        let mut j = m + 1;
        if word == "record" && matches!(self.v(j), "class" | "struct") {
            j += 1;
        }
        let name = self.v(j);
        j += 1;
        let mut n = 0;
        if self.v(j) == "<" {
            let mut nested = 1;
            n = 1;
            j += 1;
            while nested > 0 {
                if j >= hi || !matches!(self.t[j].k, Kind::Punct | Kind::Word) {
                    return None;
                }
                match self.v(j) {
                    "<" => nested += 1,
                    ">" => nested -= 1,
                    "," if nested == 1 => n += 1,
                    _ => (),
                }
                j += 1;
            }
        }
        let (_, term) = self.head(j, hi, &["{", ";"])?;
        let key = format!("{word} {name}{}", arity(n));
        if self.v(term) == ";" {
            return (word == "record").then(|| (key, term, vec![]));
        }
        let end = self.close(term);
        let kids = if word == "enum" {
            self.enum_members(term + 1, end)?
        } else {
            self.scope(term + 1, end, Scope::Type(name), depth + 1)?
        };
        Some((key, self.with_semi(end, hi), kids))
    }
    fn enum_members(&self, mut j: usize, hi: usize) -> Option<Vec<Node>> {
        let mut nodes = Vec::new();
        while j < hi {
            let first = j;
            while j < hi && self.v(j) != "," {
                j = self.after(j);
            }
            if j == first {
                return None;
            }
            let last = j - 1;
            let at = self.attrs(first);
            if at > last || !self.word(at) || (at < last && self.v(at + 1) != "=") {
                return None;
            }
            nodes.push(self.node(format!("member {}", self.v(at)), first, last, vec![]));
            j += 1;
        }
        Some(nodes)
    }
    fn member(&self, j: usize, hi: usize, cls: &str) -> Option<(String, usize)> {
        let (entries, term) = self.head(j, hi, &["{", ";", "=>", "="])?;
        let pos = entries.iter().take_while(|&&i| modifier(self.v(i))).count();
        let rest = &entries[pos..];
        let first = self.v(*rest.first()?);
        let operator = rest.iter().position(|&i| self.v(i) == "operator");
        let event = rest.iter().any(|&i| self.v(i) == "event");
        let core = if operator.is_some() || event {
            &entries[..]
        } else {
            self.cut(&entries)
        };
        let n = core.len();
        let last = *core.last()?;
        let terminator = self.v(term);
        let mut property = false;
        let key = if first == "~" {
            if rest.len() != 3 || self.v(rest[2]) != "(" || !self.word(rest[1]) {
                return None;
            }
            format!("finalizer {}", self.v(rest[1]))
        } else if let Some(op) = operator {
            let a = pos + op + 1;
            if self.v(last) != "(" || a + 1 >= n {
                return None;
            }
            let symbol = self.join(core[a], core[n - 2] + 1);
            let conv = if rest[..op].iter().any(|&x| self.v(x) == "implicit") {
                "implicit "
            } else if rest[..op].iter().any(|&x| self.v(x) == "explicit") {
                "explicit "
            } else {
                ""
            };
            format!("operator {conv}{symbol}({})", self.types(last))
        } else if event {
            let name = if terminator == "{" {
                self.name(core, n - 1)?.0
            } else {
                self.declarators(j, self.semi(term, hi)?)?
            };
            format!("event {name}")
        } else if self.v(last) == "[" && n >= 2 && self.v(core[n - 2]) == "this" {
            let qualifier = if n >= 4 && self.v(core[n - 3]) == "." {
                let r = self.name(core, n - 4)?.2;
                format!(" {}.", self.join(core[r], core[n - 3]))
            } else {
                String::new()
            };
            format!("indexer{qualifier}({})", self.types(last))
        } else if self.v(last) == "(" {
            let (name, num, r) = self.name(core, n.checked_sub(2)?)?;
            if core[..r].iter().all(|&i| modifier(self.v(i))) {
                if name != cls || num > 0 {
                    return None;
                }
                let prefix = if core[..r].iter().any(|&i| self.v(i) == "static") {
                    "static "
                } else {
                    ""
                };
                format!("{prefix}ctor({})", self.types(last))
            } else {
                format!("method {name}{}({})", arity(num), self.types(last))
            }
        } else if matches!(terminator, "{" | "=>") {
            let (name, _, r) = self.name(core, n - 1)?;
            if core[..r].iter().all(|&i| modifier(self.v(i))) {
                return None;
            }
            property = true;
            format!("property {name}")
        } else {
            if core.iter().filter(|&&i| !modifier(self.v(i))).count() < 2 {
                return None;
            }
            format!("field {}", self.declarators(j, self.semi(term, hi)?)?)
        };
        let end = match terminator {
            ";" => term,
            "{" => {
                let end = self.close(term);
                if property && self.v(end + 1) == "=" {
                    self.semi(end + 2, hi)?
                } else {
                    end
                }
            }
            _ => self.semi(term + 1, hi)?,
        };
        Some((key, end))
    }
}
pub(super) fn outline(s: &str) -> Option<Vec<Node>> {
    let text = s.strip_prefix('\u{feff}').unwrap_or(s);
    std::thread::scope(|scope| {
        std::thread::Builder::new()
            .stack_size(16 << 20)
            .spawn_scoped(scope, || {
                let t = lexer::lex(text)?;
                let n = t.len();
                Parser {
                    s: text,
                    t,
                    base: s.len() - text.len(),
                    file_scoped: false,
                }
                .scope(0, n, Scope::File, 0)
            })
            .ok()?
            .join()
            .ok()?
    })
}
