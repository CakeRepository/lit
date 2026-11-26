//! Compare exactly the code a covered function could run, including module loading.
mod functions;
mod module;
mod purity;
use super::{continuation, opener, trailing, Kind, Mode, Parser, Scanner};
use crate::{
    json::{self, object, Value},
    sha256,
};
use fancy_regex::{Captures, Regex};
use functions::Cut;
use module::Module;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Read,
    sync::LazyLock,
};
fn reserved(v: &str) -> bool {
    matches!(
        v,
        "break"
            | "case"
            | "catch"
            | "class"
            | "const"
            | "continue"
            | "debugger"
            | "default"
            | "delete"
            | "do"
            | "else"
            | "enum"
            | "export"
            | "extends"
            | "false"
            | "finally"
            | "for"
            | "function"
            | "if"
            | "import"
            | "in"
            | "instanceof"
            | "new"
            | "null"
            | "return"
            | "super"
            | "switch"
            | "this"
            | "throw"
            | "true"
            | "try"
            | "typeof"
            | "var"
            | "void"
            | "while"
            | "with"
            | "yield"
            | "let"
            | "static"
            | "await"
            | "async"
    )
}
fn digest(s: &str) -> Value {
    Value::String(sha256::hex(s.as_bytes())[..32].to_owned())
}
fn array(names: BTreeSet<String>) -> Value {
    Value::Array(names.into_iter().map(Value::String).collect())
}
static COMMENTS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"//[^\n\x{2028}\x{2029}]*|/\*[\s\S]*?\*/").unwrap());
static IDENT: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"#?(?:[\p{L}\p{Nl}\p{No}_$])[\p{L}\p{N}_$]*").unwrap());
fn clean_gap(s: &str, first: bool, last: bool) -> String {
    if first && s.starts_with("#!") {
        return s.split_once('\n').map_or_else(
            || s.to_owned(),
            |(line, rest)| format!("{line}\n{}", clean_gap(rest, true, last)),
        );
    }
    if !s.contains('/') {
        return s.into();
    }
    let marked = COMMENTS.replace_all(s, |c: &Captures<'_, str>| {
        if c.get(0).unwrap().as_str().contains('\n') {
            "\x02"
        } else {
            "\x01"
        }
    });
    let lines = marked.split('\n').collect::<Vec<_>>();
    let keep = lines
        .iter()
        .enumerate()
        .filter_map(|(i, &line)| {
            let inner = (i > 0 || first) && (i + 1 < lines.len() || last);
            (!(inner
                && line.contains(['\x01', '\x02'])
                && line
                    .chars()
                    .all(|c| c.is_whitespace() || matches!(c, '\x01' | '\x02'))))
            .then_some(line)
        })
        .collect::<Vec<_>>();
    let out = keep.join("\n").replace('\x01', "").replace('\x02', "\n");
    if out.is_empty() && !s.is_empty() && !first && !last {
        " ".into()
    } else {
        out
    }
}
struct Body {
    code: String,
    start: usize,
    end: usize,
    ids: BTreeSet<String>,
}
fn tree(cuts: &mut [Cut]) -> Vec<usize> {
    cuts.sort_by_key(|c| (c.lo, std::cmp::Reverse(c.hi)));
    let mut roots = Vec::new();
    let mut stack = Vec::<usize>::new();
    for i in 0..cuts.len() {
        while stack.last().is_some_and(|&p| cuts[p].hi <= cuts[i].lo) {
            stack.pop();
        }
        if let Some(&parent) = stack.last() {
            if cuts[i].hi > cuts[parent].hi
                || (cuts[i].lo == cuts[parent].lo && cuts[i].hi == cuts[parent].hi)
            {
                continue;
            }
            cuts[parent].kids.push(i);
        } else {
            roots.push(i);
        }
        stack.push(i);
    }
    roots
}
struct Writer<'a> {
    parser: &'a Parser<'a>,
    gaps: Vec<String>,
    lines: Vec<usize>,
    cuts: Vec<Cut>,
}
impl Writer<'_> {
    fn render(
        &self,
        lo: usize,
        hi: usize,
        kids: &[usize],
        root: bool,
    ) -> Option<(String, BTreeSet<String>)> {
        let p = self.parser;
        let mut out = String::new();
        let mut ids = BTreeSet::new();
        let mut line_break = false;
        let mut emit = |piece: &str, settle: bool| {
            let piece = if settle {
                if line_break || !piece.contains('\n') {
                    return;
                }
                "\n"
            } else {
                piece
            };
            if piece.is_empty() {
                return;
            }
            out.push_str(piece);
            let stripped = piece.trim_end_matches([
                ' ', '\t', '\n', '\r', '\x0c', '\x0b', '\u{00a0}', '\u{feff}',
            ]);
            let newline = piece[stripped.len()..].contains('\n');
            line_break = newline || (stripped.is_empty() && line_break);
        };
        let mut i = lo;
        let mut ki = 0;
        let mut skip = false;
        while i < hi {
            if i > lo || root {
                emit(&self.gaps[i], skip);
            }
            skip = false;
            if let Some(kid) = kids.get(ki).map(|&a| &self.cuts[a]).filter(|c| c.lo == i) {
                ki += 1;
                if kid.name.is_none() {
                    emit("ƒ", false);
                } else {
                    skip = true;
                }
                i = kid.hi;
                continue;
            }
            emit(p.raw(i), false);
            match p.kind(i) {
                Some(Kind::Id) => {
                    ids.insert(p.v(i).into());
                }
                Some(Kind::Template | Kind::Jsx) => {
                    for m in IDENT.find_iter(p.raw(i)) {
                        ids.insert(m.ok()?.as_str().into());
                    }
                }
                _ => {}
            }
            i += 1;
        }
        if root && !skip {
            emit(&self.gaps[p.t.len()], false);
        }
        Some((out, ids))
    }
    fn symbols(&self, roots: &[usize], top: &[usize]) -> Option<(Value, Value)> {
        let n = self.parser.t.len();
        let (code, ids) = self.render(0, n, top, true)?;
        let mut symbols = BTreeMap::from([(
            String::new(),
            Body {
                code,
                ids,
                start: 1,
                end: self.lines.len() + 1,
            },
        )]);
        let mut todo = Vec::new();
        let children = |kids: &[usize], prefix: &str| {
            let mut anon = 0;
            kids.iter()
                .map(|&i| {
                    let name = self.cuts[i].name.clone().unwrap_or_else(|| {
                        anon += 1;
                        format!("@{}", anon - 1)
                    });
                    (
                        i,
                        if prefix.is_empty() {
                            name
                        } else {
                            format!("{prefix}/{name}")
                        },
                    )
                })
                .collect::<Vec<_>>()
        };
        todo.extend(children(roots, "").into_iter().rev());
        while let Some((i, name)) = todo.pop() {
            let c = &self.cuts[i];
            let (code, ids) = self.render(c.lo, c.hi, &c.kids, false)?;
            let line = |byte: usize| 1 + self.lines.partition_point(|&n| n < byte);
            let start = line(self.parser.t[c.fs].start);
            let end = line(self.parser.t[c.fe - 1].end - 1);
            todo.extend(children(&c.kids, &name).into_iter().rev());
            match symbols.entry(name) {
                std::collections::btree_map::Entry::Occupied(mut entry) => {
                    let prior = entry.get_mut();
                    prior.code.push_str("\n\0\n");
                    prior.code.push_str(&code);
                    prior.ids.extend(ids);
                    prior.start = prior.start.min(start);
                    prior.end = prior.end.max(end);
                }
                std::collections::btree_map::Entry::Vacant(entry) => {
                    entry.insert(Body {
                        code,
                        ids,
                        start,
                        end,
                    });
                }
            }
        }
        let data = symbols
            .into_iter()
            .map(|(key, b)| {
                (
                    key,
                    object([
                        ("digest", digest(&b.code)),
                        ("start", (b.start as u64).into()),
                        ("end", (b.end as u64).into()),
                        ("ids", array(b.ids)),
                    ]),
                )
            })
            .collect();
        let legacy = digest(&self.render(0, n, roots, true)?.0);
        Some((Value::Object(data), legacy))
    }
}
fn analyze(text: &str, suffix: &str) -> Option<Value> {
    if text.contains(['\u{2028}', '\u{2029}'])
        || text
            .match_indices('\r')
            .any(|(i, _)| !text[i + 1..].starts_with('\n'))
    {
        return None;
    }
    let scanner = Scanner {
        text,
        jsx: !matches!(suffix, ".ts" | ".mts" | ".cts"),
        tsx: suffix == ".tsx",
    };
    let (t, _) = scanner.scan(0, 0)?;
    let p = Parser { text, t };
    let mut module = p.module()?;
    let pure = p.pure(&module);
    let mut cuts = p.candidates();
    let roots = tree(&mut cuts);
    let mut top = roots.clone();
    for c in module.cuts.drain(..) {
        if roots
            .iter()
            .any(|&i| cuts[i].lo < c.hi && c.lo < cuts[i].hi)
        {
            return None;
        }
        top.push(cuts.len());
        cuts.push(c);
    }
    top.sort_by_key(|&i| cuts[i].lo);
    let gaps = (0..=p.t.len())
        .map(|i| {
            let start = if i == 0 { 0 } else { p.t[i - 1].end };
            let end = p.t.get(i).map_or(text.len(), |t| t.start);
            clean_gap(&text[start..end], i == 0, i == p.t.len())
        })
        .collect();
    let writer = Writer {
        parser: &p,
        gaps,
        lines: text.match_indices('\n').map(|(i, _)| i).collect(),
        cuts,
    };
    let (symbols, legacy) = writer.symbols(&roots, &top)?;
    let imports = module
        .imports
        .into_iter()
        .map(|(source, bindings)| {
            let mut compact = String::from("{");
            for (i, (name, value)) in bindings.iter().enumerate() {
                if i > 0 {
                    compact.push(',');
                }
                json::quote(name, &mut compact);
                compact.push(':');
                json::quote(value, &mut compact);
            }
            compact.push('}');
            let b = bindings
                .into_iter()
                .map(|(k, v)| (k, Value::String(v)))
                .collect();
            (
                source,
                object([("digest", digest(&compact)), ("bindings", Value::Object(b))]),
            )
        })
        .collect();
    Some(object([
        ("symbols", symbols),
        ("imports", Value::Object(imports)),
        ("legacy", legacy),
        ("pure", Value::Bool(pure)),
        ("deps", array(module.deps)),
        ("exports", array(module.exports)),
        ("star", Value::Bool(module.star)),
    ]))
}
pub fn table(blob: &[u8], suffix: &str) -> Option<Value> {
    let text = std::str::from_utf8(blob).ok()?;
    std::thread::scope(|scope| {
        std::thread::Builder::new()
            .stack_size(16 << 20)
            .spawn_scoped(scope, || analyze(text, suffix))
            .ok()?
            .join()
            .ok()?
    })
}
pub fn command(suffix: &str) -> i32 {
    let mut bytes = Vec::new();
    if let Err(e) = std::io::stdin().read_to_end(&mut bytes) {
        eprintln!("{e}");
        return 2;
    }
    println!(
        "{}",
        json::pretty(&table(&bytes, suffix).unwrap_or(Value::Null))
    );
    0
}
