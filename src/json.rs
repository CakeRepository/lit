//! A small JSON reader and writer: enough for lit's own files, with no dependency.
//! Numbers keep their text, so nothing is lost or reformatted between runs.

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Value>),
    Object(BTreeMap<String, Value>),
}

impl Value {
    pub fn get(&self, key: &str) -> Option<&Self> {
        match self {
            Self::Object(map) => map.get(key),
            _ => None,
        }
    }
    pub fn field(&self, key: &str) -> Result<&Self, String> {
        self.get(key)
            .ok_or_else(|| format!("Missing JSON field {key}"))
    }
    pub fn string(&self) -> Result<&str, String> {
        match self {
            Self::String(value) => Ok(value),
            _ => Err("Expected a JSON string".into()),
        }
    }
    pub fn array(&self) -> Result<&[Self], String> {
        match self {
            Self::Array(value) => Ok(value),
            _ => Err("Expected a JSON array".into()),
        }
    }
    pub fn number(&self) -> Result<u64, String> {
        match self {
            Self::Number(value) => value
                .parse()
                .map_err(|_| "Expected a positive integer".into()),
            _ => Err("Expected a JSON integer".into()),
        }
    }
    pub fn boolean(&self) -> Result<bool, String> {
        match self {
            Self::Bool(value) => Ok(*value),
            _ => Err("Expected a JSON boolean".into()),
        }
    }
}

impl From<&str> for Value {
    fn from(value: &str) -> Self {
        Self::String(value.into())
    }
}
impl From<u64> for Value {
    fn from(value: u64) -> Self {
        Self::Number(value.to_string())
    }
}
pub fn object<const N: usize>(entries: [(&str, Value); N]) -> Value {
    Value::Object(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

pub fn parse(text: &str) -> Result<Value, String> {
    let mut p = Parser {
        s: text.as_bytes(),
        i: 0,
    };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    if p.i != p.s.len() {
        return Err(format!("trailing data at byte {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn err<T>(&self, what: &str) -> Result<T, String> {
        Err(format!("{what} at byte {}", self.i))
    }

    fn eat(&mut self, word: &str) -> bool {
        if self.s[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self, depth: usize) -> Result<Value, String> {
        if depth > 512 {
            return self.err("nested too deeply");
        }
        match self.s.get(self.i) {
            None => self.err("unexpected end"),
            Some(b'{') => {
                self.i += 1;
                let mut map = BTreeMap::new();
                self.ws();
                if self.eat("}") {
                    return Ok(Value::Object(map));
                }
                loop {
                    self.ws();
                    if self.s.get(self.i) != Some(&b'"') {
                        return self.err("expected a key");
                    }
                    let key = self.string()?;
                    self.ws();
                    if !self.eat(":") {
                        return self.err("expected ':'");
                    }
                    self.ws();
                    let v = self.value(depth + 1)?;
                    map.insert(key, v);
                    self.ws();
                    if self.eat(",") {
                        continue;
                    }
                    if self.eat("}") {
                        return Ok(Value::Object(map));
                    }
                    return self.err("expected ',' or '}'");
                }
            }
            Some(b'[') => {
                self.i += 1;
                let mut items = Vec::new();
                self.ws();
                if self.eat("]") {
                    return Ok(Value::Array(items));
                }
                loop {
                    self.ws();
                    items.push(self.value(depth + 1)?);
                    self.ws();
                    if self.eat(",") {
                        continue;
                    }
                    if self.eat("]") {
                        return Ok(Value::Array(items));
                    }
                    return self.err("expected ',' or ']'");
                }
            }
            Some(b'"') => Ok(Value::String(self.string()?)),
            Some(b't') if self.eat("true") => Ok(Value::Bool(true)),
            Some(b'f') if self.eat("false") => Ok(Value::Bool(false)),
            Some(b'n') if self.eat("null") => Ok(Value::Null),
            Some(c) if *c == b'-' || c.is_ascii_digit() => {
                let start = self.i;
                self.i += 1;
                while self.i < self.s.len()
                    && matches!(
                        self.s[self.i],
                        b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-'
                    )
                {
                    self.i += 1;
                }
                let text = std::str::from_utf8(&self.s[start..self.i]).unwrap();
                if text.parse::<f64>().is_err() {
                    return self.err("bad number");
                }
                Ok(Value::Number(text.to_string()))
            }
            Some(_) => self.err("unexpected character"),
        }
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let digits = self.s.get(self.i..self.i + 4).ok_or("short \\u escape")?;
        let n = u32::from_str_radix(std::str::from_utf8(digits).map_err(|e| e.to_string())?, 16)
            .map_err(|_| "bad \\u escape".to_string())?;
        self.i += 4;
        Ok(n)
    }

    fn string(&mut self) -> Result<String, String> {
        self.i += 1; // opening quote
        let mut out = String::new();
        loop {
            let start = self.i;
            while self.i < self.s.len() && self.s[self.i] != b'"' && self.s[self.i] != b'\\' {
                if self.s[self.i] < 0x20 {
                    return self.err("control character in string");
                }
                self.i += 1;
            }
            out.push_str(std::str::from_utf8(&self.s[start..self.i]).map_err(|e| e.to_string())?);
            match self.s.get(self.i) {
                None => return self.err("unterminated string"),
                Some(b'"') => {
                    self.i += 1;
                    return Ok(out);
                }
                _ => {
                    self.i += 1;
                    let c = *self.s.get(self.i).ok_or("unterminated escape")?;
                    self.i += 1;
                    match c {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let mut n = self.hex4()?;
                            if (0xd800..0xdc00).contains(&n) && self.eat("\\u") {
                                let low = self.hex4()?;
                                if (0xdc00..0xe000).contains(&low) {
                                    n = 0x10000 + ((n - 0xd800) << 10) + (low - 0xdc00);
                                }
                            }
                            out.push(char::from_u32(n).unwrap_or('\u{fffd}'));
                        }
                        _ => return self.err("bad escape"),
                    }
                }
            }
        }
    }
}

/// Writes v with keys sorted and one space of indent per level.
pub fn pretty(v: &Value) -> String {
    let mut out = String::new();
    write(v, 0, &mut out);
    out
}

fn write(v: &Value, depth: usize, out: &mut String) {
    let pad = |n: usize, out: &mut String| out.extend(std::iter::repeat_n(' ', n));
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(n),
        Value::String(s) => quote(s, out),
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Object(map) if map.is_empty() => out.push_str("{}"),
        Value::Array(items) => {
            out.push_str("[\n");
            for (i, item) in items.iter().enumerate() {
                pad(depth + 1, out);
                write(item, depth + 1, out);
                out.push_str(if i + 1 < items.len() { ",\n" } else { "\n" });
            }
            pad(depth, out);
            out.push(']');
        }
        Value::Object(map) => {
            out.push_str("{\n");
            for (i, (k, item)) in map.iter().enumerate() {
                pad(depth + 1, out);
                quote(k, out);
                out.push_str(": ");
                write(item, depth + 1, out);
                out.push_str(if i + 1 < map.len() { ",\n" } else { "\n" });
            }
            pad(depth, out);
            out.push('}');
        }
    }
}

fn quote(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_and_writes_lit_receipts() {
        let v = parse(r#"{"b": [1, 2.5e3, -0.1], "a": {"x": null, "y": true}, "s": "q\"\\é😀\n"}"#)
            .unwrap();
        assert_eq!(
            pretty(&v),
            "{\n \"a\": {\n  \"x\": null,\n  \"y\": true\n },\n \"b\": [\n  1,\n  2.5e3,\n  -0.1\n ],\n \"s\": \"q\\\"\\\\é😀\\n\"\n}"
        );
        assert_eq!(parse(&pretty(&v)).unwrap(), v);
    }

    #[test]
    fn rejects_what_is_not_json() {
        for bad in [
            "",
            "{",
            "[1,]",
            "{\"a\" 1}",
            "tru",
            "\"a",
            "1 2",
            "{\"a\":1,}",
            "01x",
        ] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
    }
}
