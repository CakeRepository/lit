//! Python AST declarations with their complete original lines, including decorators.
use super::Node;
use encoding_rs::Encoding;
use fancy_regex::Regex;
use ruff_python_ast::{self as ast, Stmt};
use ruff_python_parser::{parse, Mode, ParseOptions};
use ruff_text_size::Ranged;
use std::borrow::Cow;
use std::sync::LazyLock;

static COOKIE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[ \t\x0c]*\#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)").unwrap());

fn decode(input: &[u8]) -> Option<Cow<'_, str>> {
    let bom = input.starts_with(b"\xef\xbb\xbf");
    let body = input.strip_prefix(b"\xef\xbb\xbf").unwrap_or(input);
    let mut name = "utf-8".to_owned();
    for line in body.split(|&b| b == b'\n').take(2) {
        let line = String::from_utf8_lossy(line);
        if let Some(capture) = COOKIE.captures(line.as_ref()).ok()? {
            name = capture
                .get(1)?
                .as_str()
                .to_ascii_lowercase()
                .replace('_', "-");
            break;
        }
        if !line.trim().is_empty() && !line.trim_start().starts_with('#') {
            break;
        }
    }
    if name == "utf8" || name == "utf-8" || name.starts_with("utf-8-") {
        return Some(Cow::Borrowed(std::str::from_utf8(body).ok()?));
    }
    if bom {
        return None;
    }
    if matches!(
        name.as_str(),
        "latin-1" | "latin1" | "iso-8859-1" | "iso8859-1" | "l1" | "cp819"
    ) {
        return Some(Cow::Owned(body.iter().map(|&b| char::from(b)).collect()));
    }
    if matches!(name.as_str(), "ascii" | "us-ascii") {
        return body
            .is_ascii()
            .then(|| Cow::Borrowed(std::str::from_utf8(body).unwrap()));
    }
    let codec = Encoding::for_label(name.as_bytes())?;
    codec.decode_without_bom_handling_and_without_replacement(body)
}

// A parser offset is in decoded UTF-8, while a receipt points into the original file.
fn starts(bytes: &[u8]) -> Vec<usize> {
    let mut result = vec![0];
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'\n' || (b == b'\r' && bytes.get(i + 1) != Some(&b'\n')) {
            result.push(i + 1);
        }
    }
    if result.last() != Some(&bytes.len()) {
        result.push(bytes.len());
    }
    result
}

struct Source<'a> {
    text: &'a str,
    lines: Vec<usize>,
    raw: Vec<usize>,
}

impl Source<'_> {
    fn line(&self, offset: usize) -> usize {
        self.lines.partition_point(|&start| start <= offset) - 1
    }

    fn declaration(
        &self,
        stmt: &Stmt,
        kind: &str,
        name: &str,
        decorators: &[ast::Decorator],
        body: &[Stmt],
    ) -> Node {
        let first = decorators
            .first()
            .map_or_else(|| stmt.start(), |d| d.expression.start())
            .to_usize();
        let last = u32::from(stmt.end()) as usize - 1;
        Node {
            key: format!("{kind} {name}").encode_utf16().collect(),
            start: self.raw[self.line(first)],
            end: self.raw[self.line(last) + 1],
            children: self.nodes(body, false),
        }
    }

    fn import(&self, stmt: &Stmt, source: &str, names: &[ast::Alias]) -> Option<Node> {
        let start = u32::from(stmt.start()) as usize;
        let first = self.line(start);
        let last = self.line(u32::from(stmt.end()) as usize - 1);
        if first != last
            || !self.text[self.lines[first]..start].trim().is_empty()
            || self.text[self.lines[first]..self.lines[first + 1]].contains(';')
        {
            return None;
        }
        let names = names
            .iter()
            .map(|a| match &a.asname {
                Some(alias) => format!("{} as {alias}", a.name),
                None => a.name.to_string(),
            })
            .collect::<Vec<_>>()
            .join(",");
        Some(Node {
            key: format!("import {source}:{names}").encode_utf16().collect(),
            start: self.raw[first],
            end: self.raw[first + 1],
            children: Vec::new(),
        })
    }

    fn nodes(&self, body: &[Stmt], top: bool) -> Vec<Node> {
        let mut out = Vec::new();
        for stmt in body {
            match stmt {
                Stmt::FunctionDef(f) => out.push(self.declaration(
                    stmt,
                    if f.is_async { "async def" } else { "def" },
                    f.name.as_str(),
                    &f.decorator_list,
                    &f.body,
                )),
                Stmt::ClassDef(c) => out.push(self.declaration(
                    stmt,
                    "class",
                    c.name.as_str(),
                    &c.decorator_list,
                    &c.body,
                )),
                Stmt::Import(i) if top => out.extend(self.import(stmt, "", &i.names)),
                Stmt::ImportFrom(i) if top => {
                    let source =
                        ".".repeat(i.level as usize) + i.module.as_ref().map_or("", |m| m.as_str());
                    out.extend(self.import(stmt, &source, &i.names));
                }
                _ if top => (),
                _ => {
                    for suite in suites(stmt) {
                        out.extend(self.nodes(suite, false));
                    }
                }
            }
        }
        out
    }
}

fn suites(stmt: &Stmt) -> Vec<&[Stmt]> {
    match stmt {
        Stmt::For(s) => vec![&s.body, &s.orelse],
        Stmt::While(s) => vec![&s.body, &s.orelse],
        Stmt::If(s) => std::iter::once(s.body.as_slice())
            .chain(s.elif_else_clauses.iter().map(|c| c.body.as_slice()))
            .collect(),
        Stmt::With(s) => vec![&s.body],
        Stmt::Match(s) => s.cases.iter().map(|c| c.body.as_slice()).collect(),
        Stmt::Try(s) => try_suites(&s.body, &s.handlers, &s.orelse, &s.finalbody),
        _ => vec![],
    }
}

fn try_suites<'a>(
    body: &'a [Stmt],
    handlers: &'a [ast::ExceptHandler],
    otherwise: &'a [Stmt],
    finally: &'a [Stmt],
) -> Vec<&'a [Stmt]> {
    let mut out = vec![body];
    for ast::ExceptHandler::ExceptHandler(handler) in handlers {
        out.push(&handler.body);
    }
    out.extend([otherwise, finally]);
    out
}

pub(super) fn outline(bytes: &[u8]) -> Option<Vec<Node>> {
    // Ruff's recursion guard assumes enough stack to reach Python's legal
    // nesting depth. Windows' default main-thread stack can be smaller.
    std::thread::scope(|scope| {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn_scoped(scope, || parse_source(bytes))
            .ok()?
            .join()
            .ok()?
    })
}

fn parse_source(bytes: &[u8]) -> Option<Vec<Node>> {
    let decoded = decode(bytes)?;
    let options = ParseOptions::from(Mode::Module).with_target_version(ast::PythonVersion::PY312);
    let parsed = parse(&decoded, options).ok()?.try_into_module()?;
    if !parsed.unsupported_syntax_errors().is_empty() {
        return None;
    }
    let module = parsed.suite();
    let lines = starts(decoded.as_bytes());
    let raw = starts(bytes);
    if lines.len() != raw.len() {
        // A file consisting only of a BOM has no declarations.
        return module.is_empty().then(Vec::new);
    }
    let source = Source {
        text: &decoded,
        lines,
        raw,
    };
    Some(source.nodes(module, true))
}
