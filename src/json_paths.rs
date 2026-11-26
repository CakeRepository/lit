use crate::{
    files,
    json::{self, Value},
};
use std::path::Path;
type Result<T> = std::result::Result<T, String>;

// JSON footprints can name Git paths containing arbitrary bytes via Python's
// surrogateescape. Preserve escaped surrogates even in object keys while
// working with the regular JSON parser's Unicode strings.
const MARK: char = '\u{fdd0}';
fn mark_surrogate(out: &mut String, unit: u32) {
    out.push(MARK);
    out.push(char::from_u32(0xe000 + unit - 0xd800).unwrap());
}
fn mark_char(out: &mut String, c: char) {
    if c == MARK {
        out.push(MARK);
    }
    out.push(c);
}
pub fn path_key(path: &[u8]) -> String {
    let mut s = String::new();
    for chunk in path.utf8_chunks() {
        for c in chunk.valid().chars() {
            mark_char(&mut s, c);
        }
        for b in chunk.invalid() {
            mark_surrogate(&mut s, 0xdc00 + u32::from(*b));
        }
    }
    s
}
fn unicode_escape(text: &str) -> Option<u32> {
    let h = text.strip_prefix("\\u")?.get(..4)?;
    h.bytes()
        .all(|c| c.is_ascii_hexdigit())
        .then(|| u32::from_str_radix(h, 16).ok())
        .flatten()
}
pub fn read(path: &Path) -> Result<Value> {
    parse(&files::read(path)?)
}

pub fn parse(bytes: &[u8]) -> Result<Value> {
    let mut text = std::str::from_utf8(bytes).map_err(|e| e.to_string())?;
    let mut escaped = String::new();
    while !text.is_empty() {
        if let Some(unit) = unicode_escape(text) {
            text = &text[6..];
            if (0xd800..0xdc00).contains(&unit) {
                if let Some(low @ 0xdc00..=0xdfff) = unicode_escape(text) {
                    text = &text[6..];
                    let c =
                        char::from_u32(0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00)).unwrap();
                    mark_char(&mut escaped, c);
                    continue;
                }
            }
            if (0xd800..0xe000).contains(&unit) {
                mark_surrogate(&mut escaped, unit);
            } else if unit == MARK as u32 {
                mark_char(&mut escaped, MARK);
            } else {
                use std::fmt::Write;
                write!(escaped, "\\u{unit:04x}").unwrap();
            }
        } else {
            let mut chars = text.chars();
            let c = chars.next().unwrap();
            mark_char(&mut escaped, c);
            if c == '\\' {
                if let Some(next) = chars.next() {
                    mark_char(&mut escaped, next);
                }
            }
            text = chars.as_str();
        }
    }
    json::parse(&escaped)
}
pub fn render(value: &Value) -> String {
    use std::fmt::Write;
    let encoded = json::pretty(value);
    let mut raw = String::new();
    let mut chars = encoded.chars();
    while let Some(c) = chars.next() {
        if c != MARK {
            raw.push(c);
            continue;
        }
        match chars.next() {
            Some(MARK) => raw.push(MARK),
            Some(c @ '\u{e000}'..='\u{e7ff}') => {
                write!(raw, "\\u{:04x}", 0xd800 + (c as u32 - 0xe000)).unwrap()
            }
            Some(other) => {
                raw.push(MARK);
                raw.push(other);
            }
            None => raw.push(MARK),
        }
    }
    raw
}

/// The code points Python compares when sorting a JSON key, including lone surrogates.
fn points(s: &str) -> impl Iterator<Item = u32> + '_ {
    let mut chars = s.chars().peekable();
    std::iter::from_fn(move || {
        let c = chars.next()?;
        if c != MARK {
            return Some(c as u32);
        }
        match chars.peek().copied() {
            Some(MARK) => {
                chars.next();
                Some(MARK as u32)
            }
            Some(c @ '\u{e000}'..='\u{e7ff}') => {
                chars.next();
                Some(0xd800 + (c as u32 - 0xe000))
            }
            _ => Some(MARK as u32),
        }
    })
}
pub fn utf8(s: &str) -> Result<String> {
    points(s)
        .map(|cp| char::from_u32(cp).ok_or_else(|| "Invalid Unicode unit name".to_owned()))
        .collect()
}
/// Byte-for-byte Python json.dumps(sort_keys=True), used for agreement digests
/// and committed audit books. A pretty book has two spaces per level.
pub fn sorted(value: &Value, pretty: bool) -> String {
    use std::fmt::Write;
    fn string(s: &str, out: &mut String) {
        out.push('"');
        for c in points(s) {
            match c {
                34 => out.push_str("\\\""),
                92 => out.push_str("\\\\"),
                8 => out.push_str("\\b"),
                12 => out.push_str("\\f"),
                10 => out.push_str("\\n"),
                13 => out.push_str("\\r"),
                9 => out.push_str("\\t"),
                32..=126 => out.push(c as u8 as char),
                0..=0xffff => write!(out, "\\u{c:04x}").unwrap(),
                _ => {
                    let n = c - 0x10000;
                    write!(
                        out,
                        "\\u{:04x}\\u{:04x}",
                        0xd800 + (n >> 10),
                        0xdc00 + (n & 1023)
                    )
                    .unwrap();
                }
            }
        }
        out.push('"');
    }
    fn separator(i: usize, depth: usize, pretty: bool, out: &mut String) {
        if i > 0 {
            out.push(',');
            if !pretty {
                out.push(' ');
            }
        }
        if pretty {
            out.push('\n');
            out.extend(std::iter::repeat_n(' ', depth * 2));
        }
    }
    fn item(v: &Value, d: usize, pretty: bool, out: &mut String) {
        let nonempty = match v {
            Value::Number(n) => {
                if n.contains(['.', 'e', 'E']) {
                    if let Ok(x) = n.parse::<f64>() {
                        let repr = format!("{x:?}");
                        if let Some((base, exp)) = repr.split_once('e') {
                            let exponent: i32 = exp.parse().unwrap();
                            write!(out, "{base}e{exponent:+03}").unwrap();
                        } else {
                            out.push_str(&repr);
                        }
                        return;
                    }
                }
                out.push_str(if n == "-0" { "0" } else { n });
                return;
            }
            Value::String(s) => {
                string(s, out);
                return;
            }
            Value::Object(map) => {
                out.push('{');
                let mut entries: Vec<_> = map.iter().collect();
                entries.sort_by(|a, b| points(a.0).cmp(points(b.0)));
                for (i, (k, v)) in entries.into_iter().enumerate() {
                    separator(i, d + 1, pretty, out);
                    string(k, out);
                    out.push_str(": ");
                    item(v, d + 1, pretty, out);
                }
                !map.is_empty()
            }
            Value::Array(a) => {
                out.push('[');
                for (i, v) in a.iter().enumerate() {
                    separator(i, d + 1, pretty, out);
                    item(v, d + 1, pretty, out);
                }
                !a.is_empty()
            }
            _ => {
                out.push_str(&json::canonical(v));
                return;
            }
        };
        if pretty && nonempty {
            separator(0, d, true, out);
        }
        out.push(if matches!(v, Value::Object(_)) {
            '}'
        } else {
            ']'
        });
    }
    let mut out = String::new();
    item(value, 0, pretty, &mut out);
    out
}

pub fn compare(a: &str, b: &str) -> std::cmp::Ordering {
    points(a).cmp(points(b))
}
