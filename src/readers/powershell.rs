//! Tokenize PowerShell without executing it. Keep offsets in the input encoding.
use super::Node;
use std::collections::HashMap;

const SINGLE: &str = "'‘’‚‛";
const DOUBLE: &str = "\"“”„";
const SPACE: &str = " \t\x0c\x0b\u{a0}\u{feff}";

#[derive(Clone, Copy)]
struct Token {
    kind: char,
    start: usize,
    end: usize,
    close: usize,
}

struct Source {
    chars: Vec<char>,
    bytes: Vec<usize>,
}

impl Source {
    fn decode(blob: &[u8]) -> Option<Self> {
        let (text, prefix, wide) =
            if blob.starts_with(&[0xff, 0xfe]) || blob.starts_with(&[0xfe, 0xff]) {
                let mut chunks = blob[2..].chunks_exact(2);
                let words: Vec<_> = chunks
                    .by_ref()
                    .map(|pair| {
                        let pair = [pair[0], pair[1]];
                        if blob[0] == 0xff {
                            u16::from_le_bytes(pair)
                        } else {
                            u16::from_be_bytes(pair)
                        }
                    })
                    .collect();
                if !chunks.remainder().is_empty() {
                    return None;
                }
                (String::from_utf16(&words).ok()?, 2, true)
            } else {
                let text = std::str::from_utf8(blob).ok()?;
                let body = text.strip_prefix('\u{feff}').unwrap_or(text);
                (body.to_owned(), text.len() - body.len(), false)
            };
        let chars: Vec<char> = text.chars().collect();
        let mut bytes = Vec::with_capacity(chars.len() + 1);
        let mut offset = prefix;
        for &c in &chars {
            if c == '\0' {
                return None;
            }
            bytes.push(offset);
            offset += if wide {
                2 * c.len_utf16()
            } else {
                c.len_utf8()
            };
        }
        bytes.push(offset);
        Some(Self { chars, bytes })
    }

    fn at(&self, i: usize) -> char {
        self.chars.get(i).copied().unwrap_or('\0')
    }

    fn braced(&self, mut i: usize) -> Option<usize> {
        while i < self.chars.len() {
            match self.at(i) {
                '`' => i += 2,
                '}' => return Some(i + 1),
                _ => i += 1,
            }
        }
        None
    }

    fn quoted(&self, start: usize, depth: usize) -> Option<usize> {
        let double = DOUBLE.contains(self.at(start));
        let marks = if double { DOUBLE } else { SINGLE };
        let mut i = start + 1;
        while i < self.chars.len() {
            let c = self.at(i);
            if marks.contains(c) {
                if !marks.contains(self.at(i + 1)) {
                    return Some(i + 1);
                }
                i += 2;
            } else if double && c == '`' {
                i += 2;
            } else if double && c == '$' && self.at(i + 1) == '(' {
                let (_, close) = self.lex(i + 2, depth + 1)?;
                i = close + 1;
            } else if double && c == '$' && self.at(i + 1) == '{' {
                i = self.braced(i + 2)?;
            } else {
                i += 1;
            }
        }
        None
    }

    fn here(&self, start: usize) -> Option<usize> {
        let quote = self.at(start + 1);
        let mut i = start + 2;
        while matches!(self.at(i), ' ' | '\t') {
            i += 1;
        }
        if self.at(i) == '\r' {
            i += 1;
        }
        if self.at(i) != '\n' {
            return None;
        }
        (i..self.chars.len())
            .find(|&j| self.at(j) == '\n' && self.at(j + 1) == quote && self.at(j + 2) == '@')
            .map(|j| j + 3)
    }

    fn word_end(&self, mut i: usize) -> Option<usize> {
        while i < self.chars.len() {
            let c = self.at(i);
            if c == '`' {
                i += 2;
            } else if c == '$' && self.at(i + 1) == '{' {
                i = self.braced(i + 2)?;
            } else if (" \t\x0c\x0b\u{a0}\r\n{}()[];,|&".contains(c))
                || SINGLE.contains(c)
                || DOUBLE.contains(c)
            {
                break;
            } else {
                i += 1;
            }
        }
        Some(i.min(self.chars.len()))
    }

    fn lex(&self, mut i: usize, depth: usize) -> Option<(Vec<Token>, usize)> {
        if depth > 450 {
            return None;
        }
        let mut tokens: Vec<Token> = Vec::new();
        let mut groups: Vec<usize> = Vec::new();
        while i < self.chars.len() {
            let c = self.at(i);
            let start = i;
            i += 1;
            let kind = match c {
                c if SPACE.contains(c) => continue,
                '\r' if self.at(i) == '\n' => continue,
                '\r' => return None,
                '\n' => 'n',
                '`' if self.at(i) == '\n' => {
                    i += 1;
                    continue;
                }
                '`' if self.at(i) == '\r' && self.at(i + 1) == '\n' => {
                    i += 2;
                    continue;
                }
                '#' => {
                    while i < self.chars.len() && self.at(i) != '\n' {
                        i += 1;
                    }
                    'c'
                }
                '<' if self.at(i) == '#' => {
                    i += 1;
                    while i < self.chars.len() && !(self.at(i) == '#' && self.at(i + 1) == '>') {
                        i += 1;
                    }
                    if i >= self.chars.len() {
                        return None;
                    }
                    i += 2;
                    'c'
                }
                c if SINGLE.contains(c) || DOUBLE.contains(c) => {
                    i = self.quoted(start, depth)?;
                    's'
                }
                '@' if matches!(self.at(i), '\'' | '"') => {
                    i = self.here(start)?;
                    's'
                }
                '{' | '[' | '(' => {
                    groups.push(tokens.len());
                    c
                }
                ')' if depth > 0 && groups.is_empty() => return Some((tokens, start)),
                '}' | ']' | ')' => {
                    let open = groups.pop()?;
                    if !matches!((tokens[open].kind, c), ('{', '}') | ('[', ']') | ('(', ')')) {
                        return None;
                    }
                    tokens[open].close = tokens.len();
                    c
                }
                ';' => ';',
                ',' | '|' | '&' => 'o',
                _ => {
                    i = self.word_end(start)?;
                    'w'
                }
            };
            tokens.push(Token {
                kind,
                start,
                end: i,
                close: tokens.len(),
            });
        }
        (depth == 0 && groups.is_empty()).then_some((tokens, i))
    }
}

struct Outline {
    source: Source,
    tokens: Vec<Token>,
}

type Found = (Node, usize);

impl Outline {
    fn kind(&self, i: usize) -> char {
        self.tokens[i].kind
    }
    fn word(&self, i: usize) -> String {
        let t = self.tokens[i];
        self.source.chars[t.start..t.end].iter().collect()
    }
    fn skip(&self, mut i: usize, hi: usize, kinds: &str) -> usize {
        while i < hi && kinds.contains(self.kind(i)) {
            i += 1;
        }
        i
    }
    fn node(&self, key: String, first: usize, last: usize, children: Vec<Node>) -> Found {
        let mut start = self.tokens[first].start;
        let mut end = self.tokens[last].end;
        let mut p = start;
        while p > 0 && matches!(self.source.at(p - 1), ' ' | '\t') {
            p -= 1;
        }
        if p == 0 || self.source.at(p - 1) == '\n' {
            start = p;
        }
        let mut q = end;
        while matches!(self.source.at(q), ' ' | '\t' | '\r') {
            q += 1;
        }
        if self.source.at(q) == '\n' {
            end = q + 1;
        }
        (
            Node {
                key: key.encode_utf16().collect(),
                start: self.source.bytes[start],
                end: self.source.bytes[end],
                children,
            },
            last,
        )
    }
    fn statement_end(&self, mut i: usize, hi: usize) -> usize {
        let mut last = i;
        while i < hi && !matches!(self.kind(i), 'n' | ';') {
            if self.kind(i) != 'c' {
                last = self.tokens[i].close;
            }
            i = self.tokens[i].close + 1;
        }
        last
    }

    fn scan(&self, lo: usize, hi: usize, depth: usize) -> Option<Vec<Node>> {
        if depth > 320 {
            return None;
        }
        let mut nodes = Vec::new();
        let mut fresh = true;
        let mut last = '\0';
        let mut i = lo;
        let mut done = None;
        while i < hi {
            let k = self.kind(i);
            match k {
                'c' => (),
                'n' => {
                    if last != 'o' {
                        fresh = true;
                    }
                }
                ';' => {
                    fresh = true;
                    last = k;
                }
                _ => {
                    if fresh && done.is_none_or(|d| i > d) {
                        if let Some((node, end)) = self.unit(i, hi, depth)? {
                            nodes.push(node);
                            done = Some(end);
                        }
                    }
                    fresh = false;
                    last = k;
                }
            }
            i = self.tokens[i].close + 1;
        }
        Some(nodes)
    }

    fn unit(&self, start: usize, hi: usize, depth: usize) -> Option<Option<Found>> {
        let mut j = start;
        while j < hi && self.kind(j) == '[' {
            j = self.skip(self.tokens[j].close + 1, hi, "nc");
        }
        if j == hi || self.kind(j) != 'w' {
            return Some(None);
        }
        let keyword = self.word(j).to_lowercase();
        let mut k = self.skip(j + 1, hi, "c");
        let result = match keyword.as_str() {
            "function" | "filter" | "workflow" => {
                if k == hi || self.kind(k) != 'w' {
                    return None;
                }
                let name = self.word(k).to_lowercase();
                k = self.skip(k + 1, hi, "nc");
                if k < hi && self.kind(k) == '(' {
                    k = self.skip(self.tokens[k].close + 1, hi, "nc");
                }
                if k == hi || self.kind(k) != '{' {
                    return None;
                }
                self.node(
                    format!("function {name}"),
                    start,
                    self.tokens[k].close,
                    vec![],
                )
            }
            "class" | "enum" => {
                if k == hi || self.kind(k) != 'w' {
                    return None;
                }
                let name = self.word(k).to_lowercase();
                k += 1;
                while k < hi && self.kind(k) != '{' {
                    if !"wonc[".contains(self.kind(k)) {
                        return None;
                    }
                    k = self.tokens[k].close + 1;
                }
                if k == hi {
                    return None;
                }
                let end = self.tokens[k].close;
                let children = if keyword == "class" {
                    self.members(k + 1, end, &name)?
                } else {
                    vec![]
                };
                self.node(format!("{keyword} {name}"), start, end, children)
            }
            "using" if j == start => {
                let end = self.statement_end(j, hi);
                let parts: Vec<_> = (j..=end)
                    .filter(|&i| self.kind(i) != 'c')
                    .map(|i| self.word(i))
                    .collect();
                self.node(parts.join(" ").to_lowercase(), start, end, vec![])
            }
            "param" => {
                if k == hi || self.kind(k) != '(' {
                    return None;
                }
                self.node(keyword, start, self.tokens[k].close, vec![])
            }
            "describe" | "context" | "it" if j == start => {
                return self.pester(j, hi, &keyword, depth)
            }
            _ => return Some(None),
        };
        Some(Some(result))
    }

    fn name(&self, lo: usize, hi: usize) -> Option<String> {
        let mut i = self.skip(lo, hi, "c");
        if i == hi {
            return None;
        }
        if self.kind(i) == 'w'
            && matches!(self.word(i).to_lowercase().as_str(), "-name" | "-testname")
        {
            i = self.skip(i + 1, hi, "c");
        }
        if i == hi {
            return None;
        }
        let raw = self.word(i);
        if self.kind(i) == 'w' && !raw.starts_with(['-', '@']) {
            return Some(raw);
        }
        let mut chars = raw.chars();
        let first = chars.next()?;
        if self.kind(i) != 's' || !(SINGLE.contains(first) || DOUBLE.contains(first)) {
            return None;
        }
        chars.next_back()?;
        let mut name = String::new();
        let mut chars = chars.peekable();
        while let Some(c) = chars.next() {
            name.push(c);
            if SINGLE.contains(first)
                && SINGLE.contains(c)
                && chars.peek().is_some_and(|&next| SINGLE.contains(next))
            {
                chars.next();
            }
        }
        let name = name.trim();
        (!name.is_empty()).then(|| name.to_owned())
    }

    fn pester(&self, j: usize, hi: usize, keyword: &str, depth: usize) -> Option<Option<Found>> {
        let end = self.statement_end(j, hi);
        let mut k = j + 1;
        while k < end && self.tokens[k].close != end {
            k = self.tokens[k].close + 1;
        }
        if k >= end || self.kind(k) != '{' {
            return Some(None);
        }
        let before = k - 1;
        if before > j
            && self.kind(before) == 'w'
            && self.tokens[before].end == self.tokens[k].start
            && self.word(before).ends_with('@')
        {
            return Some(None);
        }
        let Some(name) = self.name(j + 1, k) else {
            return Some(None);
        };
        let children = if keyword == "it" {
            vec![]
        } else {
            self.scan(k + 1, end, depth + 1)?
        };
        Some(Some(self.node(format!("test {name}"), j, end, children)))
    }
    fn members(&self, lo: usize, hi: usize, cls: &str) -> Option<Vec<Node>> {
        let mut nodes = Vec::new();
        let mut counts = HashMap::new();
        let mut methods = Vec::new();
        let mut i = lo;
        while i < hi {
            let start = self.skip(i, hi, "nc;");
            if start == hi {
                break;
            }
            let mut j = start;
            while j < hi
                && (self.kind(j) == '['
                    || (self.kind(j) == 'w'
                        && matches!(self.word(j).to_lowercase().as_str(), "hidden" | "static")))
            {
                j = self.skip(self.tokens[j].close + 1, hi, "nc");
            }
            if j == hi || self.kind(j) != 'w' {
                return None;
            }
            let token = self.word(j);
            let (node, end) = if let Some(raw) = token.strip_prefix('$') {
                let name = if let Some(raw) = raw.strip_prefix('{') {
                    raw.split_once('}')?.0.to_lowercase()
                } else {
                    raw.chars()
                        .take_while(|c| c.is_alphanumeric() || *c == '_')
                        .collect::<String>()
                        .to_lowercase()
                };
                if name.is_empty() {
                    return None;
                }
                let end = self.statement_end(j, hi);
                if self.kind(end) == 'o' || matches!(self.word(end).as_str(), "=" | "+" | "-") {
                    return None;
                }
                self.node(format!("property ${name}"), start, end, vec![])
            } else {
                let name = token.to_lowercase();
                let parens = self.skip(j + 1, hi, "nc");
                if parens == hi || self.kind(parens) != '(' {
                    return None;
                }
                let mut k = self.skip(self.tokens[parens].close + 1, hi, "nc");
                while k < hi && self.kind(k) != '{' {
                    if !"wonc(".contains(self.kind(k)) {
                        return None;
                    }
                    k = self.tokens[k].close + 1;
                }
                if k == hi {
                    return None;
                }
                let label = if name == cls { "constructor" } else { "method" };
                let key = format!("{label} {name}");
                let mut arity = 0;
                let mut arg = parens + 1;
                while arg < self.tokens[parens].close {
                    if self.kind(arg) == 'w' && self.word(arg).starts_with('$') {
                        arity += 1;
                    }
                    arg = self.tokens[arg].close + 1;
                }
                *counts.entry(key.clone()).or_insert(0) += 1;
                methods.push((nodes.len(), key.clone(), arity));
                self.node(key, start, self.tokens[k].close, vec![])
            };
            nodes.push(node);
            i = end + 1;
        }
        for (idx, key, arity) in methods {
            if counts[&key] > 1 {
                nodes[idx].key.extend(format!("({arity})").encode_utf16());
            }
        }
        Some(nodes)
    }
}

pub(super) fn outline(blob: &[u8]) -> Option<Vec<Node>> {
    let source = Source::decode(blob)?;
    // Nested Pester blocks and interpolations recurse. Keep the same conservative
    // refusal on excessive depth on platforms with small default thread stacks.
    std::thread::scope(|scope| {
        std::thread::Builder::new()
            .stack_size(16 << 20)
            .spawn_scoped(scope, move || {
                let (tokens, _) = source.lex(0, 0)?;
                let count = tokens.len();
                Outline { source, tokens }.scan(0, count, 0)
            })
            .ok()?
            .join()
            .ok()?
    })
}
