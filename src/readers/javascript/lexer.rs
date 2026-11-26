use super::patterns::*;
use fancy_regex::Regex;

#[derive(Clone, Copy, PartialEq)]
pub(super) enum Kind {
    Id,
    Number,
    String,
    Template,
    Regex,
    Jsx,
    Punct,
}
pub(super) struct Token<'a> {
    pub kind: Kind,
    pub start: usize,
    pub end: usize,
    pub newline: bool,
    pub value: &'a str,
    pub mate: usize,
}
pub(super) struct Scanner<'a> {
    pub text: &'a str,
    pub jsx: bool,
    pub tsx: bool,
}

fn token<'a>(pattern: &Regex, s: &'a str) -> Option<&'a str> {
    Some(pattern.find(s).ok()??.as_str())
}
fn line(c: char) -> bool {
    matches!(c, '\n' | '\u{2028}' | '\u{2029}')
}
fn white(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{feff}' | '\u{1c}'..='\u{1f}')
}
fn next(s: &str) -> Option<char> {
    s.chars().next()
}
impl<'a> Scanner<'a> {
    pub fn scan(&self, mut i: usize, depth: usize) -> Option<(Vec<Token<'a>>, usize)> {
        if depth > 300 {
            return None;
        }
        let mut tokens: Vec<Token<'a>> = Vec::new();
        let (mut last, mut before) = ("", "");
        let (mut newline, mut operand) = (false, false);
        let mut stack = Vec::<(usize, bool)>::new();
        if i == 0 && self.text.starts_with("#!") {
            i = self.text.find('\n').unwrap_or(self.text.len());
        }
        while i < self.text.len() {
            let s = &self.text[i..];
            let c = next(s)?;
            if white(c) {
                newline |= line(c);
                i += c.len_utf8();
                continue;
            }
            if s.starts_with("//") {
                i += s.find(line).unwrap_or(s.len());
                continue;
            }
            if let Some(tail) = s.strip_prefix("/*") {
                let end = 4 + tail.find("*/")?;
                newline |= s[..end].contains('\n');
                i += end;
                continue;
            }
            let (kind, len) = match c {
                '/' if !operand => (Kind::Regex, token(&REGEX, s)?.len()),
                '\'' => (Kind::String, token(&SQ, s)?.len()),
                '"' => (Kind::String, token(&DQ, s)?.len()),
                '`' => (Kind::Template, self.template(i, depth)? - i),
                '<' if self.jsx
                    && !operand
                    && next(&s[1..])
                        .is_some_and(|c| c.is_alphabetic() || matches!(c, '>' | '_' | '$'))
                    && !(self.tsx && token(&TSX_GENERIC, s).is_some()) =>
                {
                    (Kind::Jsx, self.element(i, depth + 1)? - i)
                }
                _ => {
                    if let Some(id) = token(&IDENT, s) {
                        (Kind::Id, id.len())
                    } else if let Some(number) = (c == '.' || c.is_numeric())
                        .then(|| token(&NUMBER, s))
                        .flatten()
                    {
                        (Kind::Number, number.len())
                    } else {
                        (Kind::Punct, token(&PUNCT, s)?.len())
                    }
                }
            };
            let value = if matches!(kind, Kind::Id | Kind::Punct) {
                &s[..len]
            } else {
                ""
            };
            let idx = tokens.len();
            let mut mate = idx;
            operand = match kind {
                Kind::Id => !matches!(
                    value,
                    "return"
                        | "typeof"
                        | "case"
                        | "do"
                        | "else"
                        | "in"
                        | "of"
                        | "new"
                        | "delete"
                        | "void"
                        | "throw"
                        | "yield"
                        | "await"
                        | "instanceof"
                        | "default"
                ),
                Kind::Punct => match value {
                    "(" | "[" | "{" => {
                        let control = value == "("
                            && (matches!(last, "if" | "while" | "for" | "with")
                                || (last == "await" && before == "for"));
                        stack.push((idx, control));
                        false
                    }
                    ")" | "]" | "}" => {
                        if depth > 0 && stack.is_empty() && value == "}" {
                            return Some((tokens, i + len));
                        }
                        let (open, control) = stack.pop()?;
                        if !matches!(
                            (tokens[open].value, value),
                            ("(", ")") | ("[", "]") | ("{", "}")
                        ) {
                            return None;
                        }
                        tokens[open].mate = idx;
                        mate = open;
                        !control
                    }
                    "++" | "--" | "!" => operand,
                    _ => false,
                },
                _ => true,
            };
            tokens.push(Token {
                kind,
                start: i,
                end: i + len,
                newline,
                value,
                mate,
            });
            before = last;
            last = value;
            newline = false;
            i += len;
        }
        (depth == 0 && stack.is_empty()).then_some((tokens, i))
    }

    fn template(&self, mut i: usize, depth: usize) -> Option<usize> {
        i += 1;
        loop {
            i += self.text.get(i..)?.find(['`', '\\', '$'])?;
            match self.text.as_bytes()[i] {
                b'`' => return Some(i + 1),
                b'\\' => i += 1 + next(&self.text[i + 1..])?.len_utf8(),
                _ if self.text[i + 1..].starts_with('{') => i = self.scan(i + 2, depth + 1)?.1,
                _ => i += 1,
            }
        }
    }
    fn jsx_space(&self, mut i: usize) -> Option<usize> {
        loop {
            let s = self.text.get(i..)?.trim_start_matches(white);
            i = self.text.len() - s.len();
            if let Some(t) = s.strip_prefix("/*") {
                i += 4 + t.find("*/")?;
            } else {
                return Some(i);
            }
        }
    }
    fn element(&self, i: usize, depth: usize) -> Option<usize> {
        if depth > 300 {
            return None;
        }
        let mut i = self.jsx_space(i + 1)?;
        let name = if self.text[i..].starts_with('>') {
            ""
        } else {
            let name = token(&JSX_NAME, &self.text[i..])?;
            i += name.len();
            loop {
                i = self.jsx_space(i)?;
                match next(&self.text[i..])? {
                    '/' => {
                        i = self.jsx_space(i + 1)?;
                        return self.text[i..].starts_with('>').then_some(i + 1);
                    }
                    '>' => break,
                    '{' => {
                        i = self.scan(i + 1, depth + 1)?.1;
                    }
                    _ => {
                        i += token(&JSX_NAME, &self.text[i..])?.len();
                        i = self.jsx_space(i)?;
                        if self.text[i..].starts_with('=') {
                            i = self.jsx_space(i + 1)?;
                            match next(&self.text[i..])? {
                                c @ ('\'' | '"') => i += 2 + self.text[i + 1..].find(c)?,
                                '{' => i = self.scan(i + 1, depth + 1)?.1,
                                '<' => i = self.element(i, depth + 1)?,
                                _ => return None,
                            }
                        }
                    }
                }
            }
            name
        };
        i += 1;
        loop {
            i += self.text.get(i..)?.find(['<', '{'])?;
            if self.text[i..].starts_with('{') {
                i = self.scan(i + 1, depth + 1)?.1;
                continue;
            }
            let mut j = self.jsx_space(i + 1)?;
            if !self.text[j..].starts_with('/') {
                i = self.element(i, depth + 1)?;
                continue;
            }
            j = self.jsx_space(j + 1)?;
            let closing = token(&JSX_NAME, &self.text[j..]).unwrap_or("");
            j = self.jsx_space(j + closing.len())?;
            return (self.text[j..].starts_with('>') && closing == name).then_some(j + 1);
        }
    }
}
