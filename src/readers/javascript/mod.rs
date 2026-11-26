//! JavaScript and TypeScript declaration boundaries, including JSX and TSX.
mod lexer;
mod patterns;
use super::Node;
use lexer::{Kind, Scanner, Token};
use std::collections::HashMap;

#[derive(PartialEq, Clone, Copy)]
enum Mode {
    Statement,
    Body,
    Type,
    Initializer,
    Field,
}
struct Unit {
    key: String,
    first: usize,
    end: usize,
    children: Vec<Node>,
    overload: bool,
    signature: bool,
}
impl Unit {
    fn new(key: impl Into<String>, first: usize, end: usize) -> Self {
        Self {
            key: key.into(),
            first,
            end,
            children: vec![],
            overload: false,
            signature: false,
        }
    }
    fn body(mut self, children: Vec<Node>) -> Self {
        self.children = children;
        self
    }
    fn method(mut self, signature: bool) -> Self {
        self.overload = true;
        self.signature = signature;
        self
    }
}
struct Parser<'a> {
    text: &'a str,
    t: Vec<Token<'a>>,
}
fn opener(s: &str) -> bool {
    matches!(s, "(" | "[" | "{")
}
fn trailing(s: &str) -> bool {
    matches!(
        s,
        "." | "?."
            | ","
            | "="
            | "=>"
            | "?"
            | ":"
            | "..."
            | "+"
            | "-"
            | "*"
            | "/"
            | "%"
            | "**"
            | "&"
            | "|"
            | "^"
            | "&&"
            | "||"
            | "??"
            | "=="
            | "!="
            | "==="
            | "!=="
            | "<="
            | ">="
            | "<<"
            | ">>"
            | ">>>"
            | "<"
            | "+="
            | "-="
            | "*="
            | "/="
            | "%="
            | "**="
            | "&="
            | "|="
            | "^="
            | "<<="
            | ">>="
            | ">>>="
            | "&&="
            | "||="
            | "??="
            | "@"
            | "typeof"
            | "keyof"
            | "extends"
            | "implements"
            | "new"
            | "in"
            | "instanceof"
            | "as"
            | "satisfies"
            | "await"
            | "yield"
            | "delete"
            | "throw"
    )
}
fn continuation(s: &str) -> bool {
    matches!(
        s,
        "." | "?."
            | "("
            | "["
            | ","
            | "="
            | "=>"
            | "?"
            | ":"
            | "+"
            | "-"
            | "*"
            | "/"
            | "%"
            | "**"
            | "&"
            | "|"
            | "^"
            | "&&"
            | "||"
            | "??"
            | "=="
            | "!="
            | "==="
            | "!=="
            | "<"
            | ">"
            | "<="
            | ">="
            | "<<"
            | ">>"
            | ">>>"
            | "+="
            | "-="
            | "*="
            | "/="
            | "%="
            | "**="
            | "&="
            | "|="
            | "^="
            | "<<="
            | ">>="
            | ">>>="
            | "&&="
            | "||="
            | "??="
            | "else"
            | "catch"
            | "finally"
            | "extends"
            | "implements"
            | "instanceof"
            | "in"
            | "as"
            | "satisfies"
    )
}
fn modifier(s: &str) -> bool {
    matches!(
        s,
        "static"
            | "public"
            | "private"
            | "protected"
            | "readonly"
            | "override"
            | "abstract"
            | "declare"
            | "async"
            | "accessor"
            | "get"
            | "set"
    )
}
impl<'a> Parser<'a> {
    fn v(&self, i: usize) -> &str {
        self.t.get(i).map_or("", |t| t.value)
    }
    fn kind(&self, i: usize) -> Option<Kind> {
        self.t.get(i).map(|t| t.kind)
    }
    fn nl(&self, i: usize) -> bool {
        self.t.get(i).is_some_and(|t| t.newline)
    }
    fn close(&self, i: usize) -> usize {
        self.t[i].mate
    }
    fn raw(&self, i: usize) -> &str {
        let t = &self.t[i];
        &self.text[t.start..t.end]
    }
    fn norm(&self, lo: usize, hi: usize) -> Option<String> {
        let start = self.t.get(lo)?.start;
        let end = self.t.get(hi.checked_sub(1)?)?.end;
        Some(
            self.text
                .get(start..end)?
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" "),
        )
    }
    fn comma(&self, i: usize, hi: usize) -> bool {
        let j = i + 1;
        if j >= hi {
            return false;
        }
        let end = if self.kind(j) == Some(Kind::Id) {
            j + 1
        } else if matches!(self.v(j), "{" | "[") {
            self.close(j) + 1
        } else {
            return false;
        };
        end >= hi || matches!(self.v(end), "=" | ":" | "," | ";" | "!") || self.nl(end)
    }
    fn angle(&self, k: usize, hi: usize) -> usize {
        let mut depth = 0i32;
        let mut j = k;
        while j < hi {
            match self.v(j) {
                "<" => depth += 1,
                ">" => depth -= 1,
                ">>" => depth -= 2,
                ">>>" => depth -= 3,
                s if opener(s) => j = self.close(j),
                ";" | "}" | ")" | "]" => return k + 1,
                _ => (),
            }
            j += 1;
            if depth <= 0 {
                return if depth == 0 { j } else { k + 1 };
            }
        }
        k + 1
    }
    fn scan(&self, start: usize, hi: usize, mode: Mode) -> Option<(usize, &'static str)> {
        let mut i = start;
        while i < hi {
            let v = self.v(i);
            match v {
                "{" if mode == Mode::Body
                    && !(i > start
                        && matches!(
                            self.v(i - 1),
                            ":" | "|" | "&" | "," | "=>" | "<" | "?" | "="
                        )) =>
                {
                    return Some((i, "{"))
                }
                s if opener(s) => i = self.close(i) + 1,
                ";" => return Some((i + 1, ";")),
                "=" if mode == Mode::Type => return Some((i, "=")),
                "," if matches!(mode, Mode::Initializer | Mode::Type) && self.comma(i, hi) => {
                    return Some((i, ","))
                }
                "<" if matches!(mode, Mode::Type | Mode::Body) => i = self.angle(i, hi),
                _ => i += 1,
            }
            if i < hi && self.nl(i) && self.v(i) != ";" && !trailing(self.v(i - 1)) {
                let tpl = self.kind(i) == Some(Kind::Template);
                if !(tpl || continuation(self.v(i)) || (mode == Mode::Body && self.v(i) == "{")) {
                    return Some((i, "asi"));
                }
                if matches!(mode, Mode::Type | Mode::Initializer | Mode::Field)
                    && self.v(i - 1) == "}"
                    && (tpl || matches!(self.v(i), "(" | "["))
                {
                    return None;
                }
            }
        }
        Some((hi, "eof"))
    }
    fn finish(&self, units: Vec<Unit>) -> Option<Vec<Node>> {
        let mut merged = Vec::<Unit>::new();
        for mut u in units {
            if merged
                .last()
                .is_some_and(|p| p.signature && p.overload && u.overload && p.key == u.key)
            {
                u.first = merged.pop()?.first;
            }
            merged.push(u);
        }
        let mut seen = HashMap::new();
        let mut out = Vec::new();
        let whitespace = |c: char| {
            matches!(
                c,
                ' ' | '\t' | '\r' | '\u{c}' | '\u{b}' | '\u{feff}' | '\u{a0}'
            )
        };
        for u in merged {
            let count = seen.entry(u.key.clone()).or_insert(0);
            *count += 1;
            let key = if *count > 1 {
                format!("{} #{}", u.key, count)
            } else {
                u.key
            };
            let mut start = self.t.get(u.first)?.start;
            let mut end = self.t.get(u.end.checked_sub(1)?)?.end;
            let line = self.text[..start].rfind('\n').map_or(0, |p| p + 1);
            if self.text[line..start].chars().all(whitespace) {
                start = line;
            }
            let nl = self.text[end..]
                .find('\n')
                .map_or(self.text.len(), |p| p + end);
            if self.text[end..nl].chars().all(whitespace) {
                end = (nl + 1).min(self.text.len());
            }
            out.push(Node {
                key: key.encode_utf16().collect(),
                start,
                end,
                children: u.children,
            });
        }
        Some(out)
    }
    fn block(&self, mut k: usize, hi: usize, depth: usize) -> Option<Vec<Node>> {
        if depth > 300 {
            return None;
        }
        let mut units = Vec::new();
        while k < hi {
            if self.v(k) == ";" {
                k += 1;
                continue;
            }
            if let Some(u) = self.unit(k, hi, depth)? {
                if u.end <= k || u.end > hi {
                    return None;
                }
                k = u.end;
                units.push(u);
            } else {
                k = self.scan(k, hi, Mode::Statement)?.0.max(k + 1);
            }
        }
        self.finish(units)
    }
    fn decorator(&self, mut j: usize) -> Option<usize> {
        j += 1;
        if self.kind(j) == Some(Kind::Id) {
            j += 1;
            while self.v(j) == "." && self.kind(j + 1) == Some(Kind::Id) {
                j += 2;
            }
        }
        (self.v(j) == "(").then(|| self.close(j) + 1)
    }
    fn braced(&self, start: usize, at: usize, hi: usize, key: String) -> Option<Unit> {
        let (pos, why) = self.scan(at, hi, Mode::Body)?;
        (why == "{").then(|| Unit::new(key, start, self.close(pos) + 1))
    }
    fn name_follows(&self, j: usize) -> bool {
        matches!(self.kind(j), Some(Kind::Id | Kind::String)) && !self.nl(j)
    }
    fn unit(&self, k: usize, hi: usize, depth: usize) -> Option<Option<Unit>> {
        let mut j = k;
        let (mut export, mut default, mut declare) = (false, false, false);
        loop {
            while self.v(j) == "@" {
                j = self.decorator(j)?;
            }
            if j >= hi {
                return None;
            }
            match self.v(j) {
                "export" if !export => {
                    export = true;
                    j += 1;
                    match self.v(j) {
                        "default" => {
                            default = true;
                            j += 1;
                        }
                        _ if matches!(self.v(j), "{" | "*")
                            || (self.v(j) == "type" && matches!(self.v(j + 1), "{" | "*")) =>
                        {
                            if self.v(j) == "type" {
                                j += 1;
                            }
                            return Some(Some(self.import_export(k, j, hi, false)?));
                        }
                        "=" => {
                            return Some(Some(Unit::new(
                                "export =",
                                k,
                                self.scan(j, hi, Mode::Statement)?.0,
                            )))
                        }
                        "as" if self.v(j + 1) == "namespace" => {
                            return Some(Some(Unit::new(
                                format!("export as namespace {}", self.v(j + 2)),
                                k,
                                self.scan(j, hi, Mode::Statement)?.0,
                            )))
                        }
                        _ => (),
                    }
                }
                "declare"
                    if !declare
                        && !self.nl(j + 1)
                        && matches!(
                            self.v(j + 1),
                            "function"
                                | "async"
                                | "class"
                                | "abstract"
                                | "interface"
                                | "type"
                                | "enum"
                                | "const"
                                | "let"
                                | "var"
                                | "namespace"
                                | "module"
                                | "global"
                        ) =>
                {
                    declare = true;
                    j += 1;
                }
                "abstract" if self.v(j + 1) == "class" => j += 1,
                _ => break,
            }
        }
        let w = self.v(j);
        let next = self.v(j + 1);
        let u = match w {
            "import" if !matches!(next, "(" | ".") => self.import_export(k, j, hi, true)?,
            "import" if export || declare => return None,
            "function" | "async" if w == "function" || (next == "function" && !self.nl(j + 1)) => {
                j += if w == "async" { 2 } else { 1 };
                if self.v(j) == "*" {
                    j += 1;
                }
                let key = if self.kind(j) == Some(Kind::Id) {
                    j += 1;
                    format!("function {}", self.v(j - 1))
                } else if default {
                    "export default".into()
                } else {
                    return None;
                };
                let (pos, why) = self.scan(j, hi, Mode::Body)?;
                Unit::new(key, k, if why == "{" { self.close(pos) + 1 } else { pos })
                    .method(why != "{")
            }
            "class" => {
                j += 1;
                let key = if self.kind(j) == Some(Kind::Id)
                    && !matches!(self.v(j), "extends" | "implements")
                {
                    j += 1;
                    format!("class {}", self.v(j - 1))
                } else if default {
                    "export default".into()
                } else {
                    return None;
                };
                let (pos, why) = self.scan(j, hi, Mode::Body)?;
                if why != "{" {
                    return None;
                }
                let close = self.close(pos);
                let mut m = pos + 1;
                let mut members = Vec::new();
                while m < close {
                    if self.v(m) == ";" {
                        m += 1;
                        continue;
                    }
                    let u = self.member(m, close)?;
                    if u.end <= m || u.end > close {
                        return None;
                    }
                    m = u.end;
                    members.push(u);
                }
                Unit::new(key, k, close + 1).body(self.finish(members)?)
            }
            "interface" if self.name_follows(j + 1) => {
                self.braced(k, j + 2, hi, format!("interface {next}"))?
            }
            "enum" | "const" if w == "enum" || next == "enum" => {
                j += usize::from(w == "const");
                if !self.name_follows(j + 1) {
                    return None;
                }
                self.braced(k, j + 2, hi, format!("enum {}", self.v(j + 1)))?
            }
            "type" if self.name_follows(j + 1) && matches!(self.v(j + 2), "=" | "<") => Unit::new(
                format!("type {next}"),
                k,
                self.scan(j + 2, hi, Mode::Statement)?.0,
            ),
            "namespace" | "module" if self.name_follows(j + 1) => {
                let mut i = j + 1;
                while i < hi && !matches!(self.v(i), "{" | ";") {
                    if !matches!(self.kind(i), Some(Kind::Id | Kind::String)) && self.v(i) != "." {
                        return None;
                    }
                    i += 1;
                }
                let key = format!("{w} {}", self.norm(j + 1, i)?);
                match self.v(i) {
                    ";" => Unit::new(key, k, i + 1),
                    "{" => Unit::new(key, k, self.close(i) + 1).body(self.block(
                        i + 1,
                        self.close(i),
                        depth + 1,
                    )?),
                    _ => return None,
                }
            }
            "global" if (export || declare) && next == "{" => Unit::new(
                "global",
                k,
                self.close(j + 1) + 1,
            )
            .body(self.block(j + 2, self.close(j + 1), depth + 1)?),
            "const" | "var" | "let"
                if w != "let"
                    || self.kind(j + 1) == Some(Kind::Id)
                    || matches!(next, "{" | "[") =>
            {
                self.variables(k, j, hi)?
            }
            _ if default => Unit::new("export default", k, self.scan(j, hi, Mode::Statement)?.0),
            _ if export || declare => return None,
            "describe" | "test" | "it" | "suite" => {
                let mut p = j + 1;
                while self.v(p) == "."
                    && matches!(
                        self.v(p + 1),
                        "only"
                            | "skip"
                            | "todo"
                            | "concurrent"
                            | "serial"
                            | "failing"
                            | "sequential"
                    )
                {
                    p += 2;
                }
                if self.v(p) != "("
                    || !matches!(self.kind(p + 1), Some(Kind::String | Kind::Template))
                {
                    return Some(None);
                }
                let raw = self.raw(p + 1);
                let name = &raw[1..raw.len() - 1];
                if self.kind(p + 1) == Some(Kind::Template) && name.contains("${") {
                    return Some(None);
                }
                Unit::new(
                    format!("test {name}"),
                    k,
                    self.scan(j, hi, Mode::Statement)?.0,
                )
            }
            _ => return Some(None),
        };
        Some(Some(u))
    }
    fn import_export(&self, k: usize, j: usize, hi: usize, import: bool) -> Option<Unit> {
        let mut i = j + usize::from(import);
        let end = self.scan(i, hi, Mode::Statement)?.0;
        let mut source = None;
        while i < end {
            if opener(self.v(i)) {
                if import
                    && self.v(i) == "("
                    && self.v(i - 1) == "require"
                    && self.kind(i + 1) == Some(Kind::String)
                {
                    source = Some(i + 1);
                    break;
                }
                i = self.close(i);
            } else if self.kind(i) == Some(Kind::String) {
                source = Some(i);
                break;
            }
            i += 1;
        }
        let key = if let Some(i) = source {
            let raw = self.raw(i);
            format!(
                "{} {}",
                if import { "import" } else { "export from" },
                &raw[1..raw.len() - 1]
            )
        } else if import {
            let mut names = self.t[j + 1..end]
                .iter()
                .filter(|t| t.kind == Kind::Id)
                .map(|t| t.value);
            let first = names.next()?;
            format!(
                "import {}",
                if first == "type" {
                    names.next().unwrap_or(first)
                } else {
                    first
                }
            )
        } else {
            format!("export {}", self.norm(j, end)?.trim_end_matches(';').trim())
        };
        Some(Unit::new(key, k, end))
    }
    fn variables(&self, k: usize, mut j: usize, hi: usize) -> Option<Unit> {
        let keyword = self.v(j);
        j += 1;
        let mut names = vec![];
        loop {
            if self.kind(j) == Some(Kind::Id) {
                names.push(self.v(j).to_owned());
                j += 1;
            } else if matches!(self.v(j), "{" | "[") {
                names.push(self.norm(j, self.close(j) + 1)?);
                j = self.close(j) + 1;
            } else {
                return None;
            }
            if self.v(j) == "!" {
                j += 1;
            }
            let (mut pos, mut why) = if self.v(j) == ":" {
                self.scan(j + 1, hi, Mode::Type)?
            } else {
                (j, "at")
            };
            if matches!(why, "at" | "=") && pos < hi && self.v(pos) == "=" {
                (pos, why) = self.scan(pos + 1, hi, Mode::Initializer)?;
            } else if why == "at" && pos < hi {
                if self.v(pos) == "," && self.comma(pos, hi) {
                    why = ",";
                } else if self.v(pos) == ";" {
                    pos += 1;
                } else if !self.nl(pos) {
                    return None;
                }
            }
            if why == "," {
                j = pos + 1;
                continue;
            }
            return Some(Unit::new(format!("{keyword} {}", names.join(",")), k, pos));
        }
    }
    fn member(&self, k: usize, hi: usize) -> Option<Unit> {
        let mut j = k;
        while self.v(j) == "@" {
            j = self.decorator(j)?;
        }
        let (mut stat, mut get, mut set) = (false, false, false);
        while modifier(self.v(j)) {
            let word = self.v(j);
            let n = j + 1;
            if word == "static" && self.v(n) == "{" {
                return Some(Unit::new("static block", k, self.close(n) + 1));
            }
            if n >= hi
                || (!(matches!(self.kind(n), Some(Kind::Id | Kind::String | Kind::Number))
                    || matches!(self.v(n), "[" | "*")))
                || (word == "async" && self.nl(n))
            {
                break;
            }
            stat |= word == "static";
            get |= word == "get";
            set |= word == "set";
            j += 1;
        }
        if self.v(j) == "*" {
            j += 1;
        }
        if j >= hi {
            return None;
        }
        let name = if matches!(self.kind(j), Some(Kind::Id | Kind::String | Kind::Number)) {
            j += 1;
            self.raw(j - 1).to_owned()
        } else if self.v(j) == "[" {
            if self.kind(j + 1) == Some(Kind::Id) && self.v(j + 2) == ":" {
                return None;
            }
            let begin = j;
            j = self.close(j) + 1;
            self.norm(begin, j)?
        } else {
            return None;
        };
        if j < hi && matches!(self.v(j), "?" | "!") {
            j += 1;
        }
        let prefix = if stat { "static " } else { "" };
        if j < hi && matches!(self.v(j), "(" | "<") {
            let (pos, why) = self.scan(j, hi, Mode::Body)?;
            let key = if name == "constructor" && !stat {
                name
            } else {
                format!(
                    "{prefix}{} {name}",
                    if get {
                        "get"
                    } else if set {
                        "set"
                    } else {
                        "method"
                    }
                )
            };
            return Some(
                Unit::new(key, k, if why == "{" { self.close(pos) + 1 } else { pos })
                    .method(why != "{"),
            );
        }
        let end = if j >= hi || (self.nl(j) && !matches!(self.v(j), ":" | "=" | ";")) {
            j
        } else if matches!(self.v(j), ":" | "=" | ";") {
            self.scan(j, hi, Mode::Field)?.0
        } else {
            return None;
        };
        Some(Unit::new(format!("{prefix}field {name}"), k, end))
    }
}
pub(super) fn outline(text: &str, suffix: &str) -> Option<Vec<Node>> {
    std::thread::scope(|scope| {
        std::thread::Builder::new()
            .stack_size(16 << 20)
            .spawn_scoped(scope, || {
                let scanner = Scanner {
                    text,
                    jsx: matches!(suffix, ".tsx" | ".jsx"),
                    tsx: suffix == ".tsx",
                };
                let (t, _) = scanner.scan(0, 0)?;
                let count = t.len();
                Parser { text, t }.block(0, count, 0)
            })
            .ok()?
            .join()
            .ok()?
    })
}
