//! Skip literal bodies and directives before pairing declaration-level brackets.
#[derive(Clone, Copy, PartialEq)]
pub(super) enum Kind {
    Word,
    Number,
    Literal,
    Punct,
}
pub(super) struct Token<'a> {
    pub k: Kind,
    pub text: &'a str,
    pub start: usize,
    pub end: usize,
    pub mate: usize,
}
pub(super) fn whitespace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}' | '\u{feff}')
}
fn word(c: char) -> bool {
    if c.is_ascii() {
        return c == '_' || c.is_ascii_alphanumeric();
    }
    static WORD: std::sync::LazyLock<fancy_regex::Regex> =
        std::sync::LazyLock::new(|| fancy_regex::Regex::new(r"^[\p{L}\p{N}]$").unwrap());
    WORD.is_match(c.encode_utf8(&mut [0; 4])).unwrap_or(false)
}
fn digit(c: char) -> bool {
    if c.is_ascii() || !c.is_numeric() {
        return c.is_ascii_digit();
    }
    // The reference accepts Unicode decimal digits and characters whose
    // Numeric_Type is Digit, including superscripts and circled digits.
    static DIGIT: std::sync::LazyLock<fancy_regex::Regex> = std::sync::LazyLock::new(|| {
        fancy_regex::Regex::new(concat!(
        r"^[\p{Nd}\x{b2}-\x{b3}\x{b9}\x{1369}-\x{1371}\x{19da}\x{2070}\x{2074}-\x{2079}\x{2080}-\x{2089}",
        r"\x{2460}-\x{2468}\x{2474}-\x{247c}\x{2488}-\x{2490}\x{24ea}\x{24f5}-\x{24fd}\x{24ff}",
        r"\x{2776}-\x{277e}\x{2780}-\x{2788}\x{278a}-\x{2792}\x{10a40}-\x{10a43}\x{10e60}-\x{10e68}",
        r"\x{11052}-\x{1105a}\x{1f100}-\x{1f10a}]$")).unwrap()
    });
    DIGIT.is_match(c.encode_utf8(&mut [0; 4])).unwrap_or(false)
}
fn at(s: &str, i: usize) -> Option<char> {
    s.get(i..)?.chars().next()
}
fn run(s: &str, i: usize, c: u8) -> usize {
    s.as_bytes()[i..].iter().take_while(|&&b| b == c).count()
}
fn character(s: &str, i: usize) -> Option<usize> {
    let c = at(s, i + 1)?;
    let j = i + 1 + c.len_utf8();
    if c == '\\' {
        let j = j + at(s, j)?.len_utf8();
        let end = j + s.get(j..)?.find('\'')?;
        return (s[i..end].chars().count() <= 12).then_some(end + 1);
    }
    (!matches!(c, '\'' | '\n') && at(s, j) == Some('\'')).then_some(j + 1)
}
fn string_at(s: &str, i: usize) -> bool {
    s[i..].starts_with(['"', '$']) || s[i..].starts_with("@\"") || s[i..].starts_with("@$")
}
fn hole(s: &str, mut i: usize, nesting: usize) -> Option<usize> {
    let mut bracket = 0usize;
    while let Some(c) = at(s, i) {
        match c {
            '(' | '[' | '{' => bracket += 1,
            '}' if bracket == 0 => return Some(i),
            ')' | ']' | '}' => bracket = bracket.checked_sub(1)?,
            '\'' => {
                i = character(s, i)?;
                continue;
            }
            _ if string_at(s, i) => {
                i = string(s, i, nesting + 1)?;
                continue;
            }
            ':' if bracket == 0 => {
                if s[i..].starts_with("::") {
                    i += 2;
                    continue;
                }
                return Some(i + s[i..].find('}')?);
            }
            _ => (),
        }
        i += c.len_utf8();
    }
    None
}
fn string(s: &str, mut i: usize, nesting: usize) -> Option<usize> {
    if nesting > 300 {
        return None;
    }
    let (mut dollars, mut ats) = (0, 0);
    while let Some(c @ ('$' | '@')) = at(s, i) {
        dollars += usize::from(c == '$');
        ats += usize::from(c == '@');
        i += 1;
    }
    if ats > 1 || at(s, i) != Some('"') {
        return None;
    }
    let quotes = run(s, i, b'"');
    if ats == 0 && quotes >= 3 {
        i += quotes;
        loop {
            i += s
                .get(i..)?
                .find(|c| c == '"' || (dollars > 0 && c == '{'))?;
            let c = s.as_bytes()[i];
            let count = run(s, i, c);
            i += count;
            if c == b'"' {
                if count >= quotes {
                    return (count == quotes).then_some(i);
                }
            } else if count >= dollars {
                if count >= 2 * dollars {
                    return None;
                }
                i = hole(s, i, nesting)?;
                if run(s, i, b'}') < dollars {
                    return None;
                }
                i += dollars;
            }
        }
    }
    if dollars > 1 {
        return None;
    }
    i += 1;
    loop {
        i += s.get(i..)?.find(|c| {
            c == '"'
                || (ats == 0 && matches!(c, '\\' | '\n'))
                || (dollars > 0 && matches!(c, '{' | '}'))
        })?;
        match s.as_bytes()[i] {
            b'"' if ats == 1 && at(s, i + 1) == Some('"') => i += 2,
            b'"' => return Some(i + 1),
            b'\n' => return None,
            b'\\' => i += 1 + at(s, i + 1)?.len_utf8(),
            b'{' if at(s, i + 1) != Some('{') => i = hole(s, i + 1, nesting)? + 1,
            b'}' if at(s, i + 1) != Some('}') => return None,
            _ => i += 2,
        }
    }
}
pub(super) fn lex(s: &str) -> Option<Vec<Token<'_>>> {
    let mut i = 0;
    let mut tokens = Vec::<Token<'_>>::new();
    let mut groups = Vec::<usize>::new();
    while let Some(c) = at(s, i) {
        let start = i;
        let tail = &s[i..];
        let kind;
        if whitespace(c) {
            i += c.len_utf8();
            continue;
        } else if tail.starts_with("//") {
            i += tail.find('\n').unwrap_or(tail.len());
            continue;
        } else if let Some(comment) = tail.strip_prefix("/*") {
            i += 4 + comment.find("*/")?;
            continue;
        } else if c == '#' {
            let line = s[..i].rfind('\n').map_or(0, |j| j + 1);
            if !s[line..i].trim().is_empty() {
                return None;
            }
            let size = tail.find('\n').unwrap_or(tail.len());
            let mut chars = tail[1..size].trim_start().chars();
            let name: String = chars
                .by_ref()
                .take_while(char::is_ascii_alphabetic)
                .collect();
            if !matches!(
                name.as_str(),
                "region"
                    | "endregion"
                    | "pragma"
                    | "nullable"
                    | "define"
                    | "undef"
                    | "line"
                    | "error"
                    | "warning"
            ) {
                return None;
            }
            i += size;
            continue;
        } else if string_at(s, i) {
            kind = Kind::Literal;
            i = string(s, i, 0)?;
        } else if c == '\'' {
            kind = Kind::Literal;
            i = character(s, i)?;
        } else if c == '@' || word(c) {
            if c == '@' {
                i += 1;
                let next = at(s, i)?;
                if !word(next) || digit(next) {
                    return None;
                }
            }
            kind = if digit(c) { Kind::Number } else { Kind::Word };
            i += s[i..].find(|c| !word(c)).unwrap_or(s.len() - i);
        } else {
            if matches!(c, '\\' | '`') {
                return None;
            }
            kind = Kind::Punct;
            i += c.len_utf8();
            if matches!(
                (c, at(s, i)),
                ('=', Some('>' | '='))
                    | ('!' | '<' | '>', Some('='))
                    | (':', Some(':'))
                    | ('-', Some('>'))
            ) {
                i += 1;
            }
        }
        let text = &s[start..i];
        let idx = tokens.len();
        let mut mate = idx;
        match text {
            "(" | "[" | "{" => groups.push(idx),
            ")" | "]" | "}" => {
                let open = groups.pop()?;
                if !matches!(
                    (tokens[open].text, text),
                    ("(", ")") | ("[", "]") | ("{", "}")
                ) {
                    return None;
                }
                tokens[open].mate = idx;
                mate = open;
            }
            _ => (),
        }
        tokens.push(Token {
            k: kind,
            text,
            start,
            end: i,
            mate,
        });
    }
    groups.is_empty().then_some(tokens)
}
