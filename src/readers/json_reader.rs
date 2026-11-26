//! JSON object members are units. Keep commas and inter-member whitespace in the gaps.
use super::Node;
use std::collections::BTreeSet;

struct Parser<'a> {
    text: &'a str,
    pos: usize,
}

impl Parser<'_> {
    fn byte(&self) -> Option<u8> {
        self.text.as_bytes().get(self.pos).copied()
    }

    fn take(&mut self, value: u8) -> bool {
        if self.byte() == Some(value) {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    fn space(&mut self) {
        while matches!(self.byte(), Some(b' ' | b'\t' | b'\r' | b'\n')) {
            self.pos += 1;
        }
    }

    fn string(&mut self) -> Option<Vec<u16>> {
        self.take(b'"').then_some(())?;
        let mut units = Vec::new();
        loop {
            let start = self.pos;
            while self
                .byte()
                .is_some_and(|c| c >= 0x20 && c != b'"' && c != b'\\')
            {
                self.pos += 1;
            }
            units.extend(self.text[start..self.pos].encode_utf16());
            if self.take(b'"') {
                return Some(units);
            }
            self.take(b'\\').then_some(())?;
            let byte = self.byte()?;
            self.pos += 1;
            let unit = match byte {
                b'"' | b'\\' | b'/' => byte as u16,
                b'b' => 8,
                b'f' => 12,
                b'n' => 10,
                b'r' => 13,
                b't' => 9,
                b'u' => {
                    let digits = self.text.as_bytes().get(self.pos..self.pos + 4)?;
                    if !digits.iter().all(u8::is_ascii_hexdigit) {
                        return None;
                    }
                    self.pos += 4;
                    u16::from_str_radix(std::str::from_utf8(digits).ok()?, 16).ok()?
                }
                _ => return None,
            };
            units.push(unit);
        }
    }

    fn digits(&mut self) -> Option<usize> {
        let start = self.pos;
        while self.byte().is_some_and(|c| c.is_ascii_digit()) {
            self.pos += 1;
        }
        (start < self.pos).then_some(self.pos - start)
    }

    fn number(&mut self) -> Option<()> {
        self.take(b'-');
        let length = if self.take(b'0') { 1 } else { self.digits()? };
        let fraction = self.take(b'.');
        if fraction {
            self.digits()?;
        }
        let exponent = self.take(b'E') || self.take(b'e');
        if exponent {
            if !self.take(b'+') {
                self.take(b'-');
            }
            self.digits()?;
        }
        // The reference decoder limits integer conversion to 4,300 digits.
        (fraction || exponent || length <= 4300).then_some(())
    }

    fn scalar(&mut self) -> Option<()> {
        if self.byte() == Some(b'"') {
            self.string()?;
        } else if let Some(word) = ["true", "false", "null"]
            .iter()
            .find(|w| self.text[self.pos..].starts_with(**w))
        {
            self.pos += word.len();
        } else {
            self.number()?;
        }
        Some(())
    }

    fn key(&mut self, frame: &mut Frame) -> Option<()> {
        let before = &self.text[..self.pos];
        let indent = before.trim_end_matches([' ', '\t']).len();
        let start = if indent == 0 || before[..indent].ends_with(['\r', '\n']) {
            indent
        } else {
            self.pos
        };
        let key = self.string()?;
        if !frame.keys.insert(key.clone()) {
            return None;
        }
        if frame.retain {
            frame.member = Some((key, start));
        }
        self.space();
        self.take(b':').then_some(())?;
        self.space();
        Some(())
    }

    // Keep the parse stack on the heap even for small-stack Windows runners.
    fn object(&mut self) -> Option<Vec<Node>> {
        self.take(b'{').then_some(())?;
        let mut stack = vec![Frame::new(b'}', true)];
        loop {
            self.space();
            let frame = stack.last_mut()?;
            if self.take(frame.close) {
                let finished = stack.pop()?;
                if let Some(parent) = stack.last_mut() {
                    parent.finish(self.pos, finished.nodes);
                    continue;
                }
                return Some(finished.nodes);
            }
            if !frame.first {
                self.take(b',').then_some(())?;
                self.space();
            }
            frame.first = false;
            if frame.close == b'}' {
                self.key(frame)?;
            }
            match self.byte()? {
                b'{' | b'[' => {
                    let object = self.take(b'{');
                    if !object {
                        self.pos += 1;
                    }
                    let retain = frame.retain && object;
                    // The reference parser refuses beyond Python's recursion limit.
                    if stack.len() >= 1000 {
                        return None;
                    }
                    stack.push(Frame::new(if object { b'}' } else { b']' }, retain));
                }
                _ => {
                    self.scalar()?;
                    frame.finish(self.pos, Vec::new());
                }
            }
        }
    }
}

struct Frame {
    close: u8,
    retain: bool,
    first: bool,
    keys: BTreeSet<Vec<u16>>,
    member: Option<(Vec<u16>, usize)>,
    nodes: Vec<Node>,
}

impl Frame {
    fn new(close: u8, retain: bool) -> Self {
        Self {
            close,
            retain,
            first: true,
            keys: BTreeSet::new(),
            member: None,
            nodes: Vec::new(),
        }
    }

    fn finish(&mut self, end: usize, children: Vec<Node>) {
        if let Some((key, start)) = self.member.take() {
            self.nodes.push(Node {
                key,
                start,
                end,
                children,
            });
        }
    }
}

pub(super) fn outline(text: &str) -> Option<Vec<Node>> {
    let mut p = Parser {
        text,
        pos: if text.starts_with('\u{feff}') { 3 } else { 0 },
    };
    p.space();
    let nodes = p.object()?;
    p.space();
    (p.pos == text.len()).then_some(nodes)
}
