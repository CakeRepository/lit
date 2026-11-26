//! Conservative outline of YAML block mappings. Preserve sequence and scalar bodies as leaves.
use super::Node;
use fancy_regex::Regex;
use std::collections::BTreeSet;
use std::sync::LazyLock;

static KEY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
    r#"^(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\n]|'')*'|[^\s#'"\[\]{},&*!|>%@\x60?:\-][^\n]*?)\s*:(?=\s|$)"#
).unwrap()
});

fn comment_or_empty(s: &str) -> Option<()> {
    let s = s.trim();
    (s.is_empty() || s.starts_with('#')).then_some(())
}

fn quoted_end(s: &str) -> Option<usize> {
    let mut chars = s.char_indices().peekable();
    let (_, quote) = chars.next()?;
    while let Some((index, c)) = chars.next() {
        if c == '\\' && quote == '"' {
            chars.next()?;
        } else if c == quote {
            if c == '\'' && chars.next_if(|(_, next)| *next == '\'').is_some() {
                continue;
            }
            return Some(index + 1);
        }
    }
    None
}

fn anchor(s: &str) -> bool {
    s.strip_prefix(['&', '*'])
        .and_then(|s| s.chars().next())
        .is_some_and(|c| c.is_alphanumeric() || matches!(c, '_' | '-'))
}

fn flow(s: &str) -> Option<()> {
    let mut pos = 0;
    let mut depth = 0usize;
    let mut plain = String::new();
    while pos < s.len() {
        let c = s[pos..].chars().next()?;
        let prev = &s[..pos];
        if matches!(c, '"' | '\'') && (pos == 0 || prev.ends_with([' ', '\t', '[', '{', ',', ':']))
        {
            pos += quoted_end(&s[pos..])?;
            continue;
        }
        match c {
            '[' | '{' => depth += 1,
            ']' | '}' => {
                depth = depth.checked_sub(1)?;
                if depth == 0 {
                    comment_or_empty(&s[pos + 1..])?;
                    return (!plain
                        .split(['[', ']', '{', '}', ',', ':'])
                        .any(|s| anchor(s.trim())))
                    .then_some(());
                }
            }
            '#' if prev.ends_with([' ', '\t']) => return None,
            _ => {}
        }
        plain.push(c);
        pos += c.len_utf8();
    }
    None
}

#[derive(PartialEq)]
enum Scalar {
    Empty,
    Block,
    Other,
}

fn scalar(s: &str) -> Option<Scalar> {
    let s = s.trim_matches(' ');
    if s.is_empty() || s.starts_with('#') {
        return Some(Scalar::Empty);
    }
    if s.starts_with(['"', '\'']) {
        comment_or_empty(&s[quoted_end(s)?..])?;
    } else if s.starts_with(['[', '{']) {
        flow(s)?;
    } else {
        let comment = s
            .match_indices('#')
            .find(|(i, _)| s[..*i].ends_with(char::is_whitespace));
        let plain = s[..comment.map_or(s.len(), |(i, _)| i)].trim();
        let mut untagged = plain;
        while untagged.starts_with('!') {
            untagged = untagged
                .trim_start_matches(|c: char| !c.is_whitespace())
                .trim_start();
        }
        if anchor(untagged) {
            return None;
        }
        if let Some(flags) = plain.strip_prefix(['|', '>']) {
            if flags
                .bytes()
                .all(|b| b.is_ascii_digit() || matches!(b, b'-' | b'+'))
            {
                return (!flags.bytes().any(|b| b.is_ascii_digit())).then_some(Scalar::Block);
            }
        }
    }
    Some(Scalar::Other)
}

fn key_text(s: &str) -> Option<String> {
    if s.starts_with('"') {
        let inner = &s[1..s.len() - 1];
        (!inner.contains('\\')).then(|| inner.to_owned())
    } else if s.starts_with('\'') {
        Some(s[1..s.len() - 1].replace("''", "'"))
    } else {
        (!s.contains('#')).then(|| s.to_owned())
    }
}

#[derive(PartialEq, Default)]
enum Kind {
    #[default]
    Gap,
    Scalar,
    Text,
}

#[derive(Default)]
struct Line {
    start: usize,
    end: usize,
    indent: usize,
    kind: Kind,
    dash: bool,
    empty: bool,
    key: Option<String>,
}

fn classify(text: &str) -> Option<Vec<Line>> {
    let body = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut position = text.len() - body.len();
    let mut lines = Vec::new();
    let mut block: Option<(usize, Option<usize>)> = None;
    let mut marker = false;
    let mut content = false;
    for raw in body.split_inclusive('\n') {
        let start = position;
        position += raw.len();
        let s = raw.trim_end_matches(['\r', '\n']);
        let rest = s.trim_start_matches(' ');
        let indent = s.len() - rest.len();
        let mut line = Line {
            start,
            end: position,
            indent,
            ..Line::default()
        };
        if s.trim_matches([' ', '\t', '\r']).is_empty() {
            lines.push(line);
            continue;
        }
        if let Some((owner, first)) = &mut block {
            if indent > *owner {
                if indent < *first.get_or_insert(indent) {
                    return None;
                }
                line.kind = Kind::Scalar;
                lines.push(line);
                continue;
            }
            block = None;
        }
        if rest.starts_with('\t') {
            return None;
        }
        if rest.starts_with('#') {
            lines.push(line);
            continue;
        }
        if indent == 0 && (s.starts_with("---") || s.starts_with("...") || s.starts_with('%')) {
            let tail = s.strip_prefix("---")?;
            if content || marker || !(tail.is_empty() || tail.starts_with([' ', '\t'])) {
                return None;
            }
            comment_or_empty(tail)?;
            marker = true;
            lines.push(line);
            continue;
        }
        content = true;
        line.kind = Kind::Text;
        let mut rest = rest;
        let mut dash_column = indent;
        while rest == "-" || rest.starts_with("- ") {
            dash_column = s.len() - rest.len();
            line.dash = true;
            rest = rest[1..].trim_start_matches(' ');
        }
        if rest.starts_with('\t') {
            return None;
        }
        let mut owner = dash_column;
        if let Some(found) = KEY.find(rest).ok()? {
            let key = key_text(rest[..found.end() - 1].trim_end())?;
            if key == "<<" {
                return None;
            }
            line.key = Some(key);
            owner = s.len() - rest.len();
            rest = &rest[found.end()..];
        }
        match scalar(rest)? {
            Scalar::Block => block = Some((owner, None)),
            Scalar::Empty => line.empty = true,
            Scalar::Other => {}
        }
        lines.push(line);
    }
    Some(lines)
}

struct Level<'a> {
    i: usize,
    limit: usize,
    indent: usize,
    seen: BTreeSet<&'a str>,
    nodes: Vec<Node>,
}

impl Level<'_> {
    fn new(i: usize, limit: usize, indent: usize) -> Self {
        Self {
            i,
            limit,
            indent,
            seen: BTreeSet::new(),
            nodes: Vec::new(),
        }
    }
}

pub(super) fn outline(text: &str) -> Option<Vec<Node>> {
    let lines = classify(text)?;
    let Some(first) = lines.iter().position(|line| line.kind == Kind::Text) else {
        return Some(Vec::new());
    };
    let mut levels = vec![Level::new(first, lines.len(), lines[first].indent)];
    loop {
        let level = levels.last_mut()?;
        let i = level.i;
        if i == level.limit {
            let nodes = levels.pop()?.nodes;
            if let Some(parent) = levels.last_mut() {
                parent.nodes.last_mut()?.children = nodes;
                continue;
            }
            return Some(nodes);
        }
        let line = &lines[i];
        if line.kind == Kind::Gap {
            level.i += 1;
            continue;
        }
        let key = line.key.as_deref()?;
        if line.kind != Kind::Text
            || line.dash
            || line.indent != level.indent
            || !level.seen.insert(key)
        {
            return None;
        }
        let mut end = i + 1;
        let mut child = None;
        for (j, next) in lines.iter().enumerate().take(level.limit).skip(end) {
            if next.kind == Kind::Gap {
                continue;
            }
            if !(next.kind == Kind::Scalar
                || next.indent > level.indent
                || (line.empty && next.dash && next.indent == level.indent))
            {
                break;
            }
            end = j + 1;
            if next.kind == Kind::Text && child.is_none() {
                child = Some(j);
            }
        }
        level.nodes.push(Node {
            key: key.encode_utf16().collect(),
            start: line.start,
            end: lines[end - 1].end,
            children: Vec::new(),
        });
        level.i = end;
        if let Some(j) = child.filter(|&j| {
            line.empty && !lines[j].dash && lines[j].key.is_some() && lines[j].indent > line.indent
        }) {
            // The reference refuses source nested beyond its recursion limit.
            if levels.len() >= 1000 {
                return None;
            }
            levels.push(Level::new(j, end, lines[j].indent));
        }
    }
}
