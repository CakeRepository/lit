//! Unix fnmatchcase: wildcards include directory separators and leading dots.
use crate::json_paths::points;

enum Token {
    Star,
    Any,
    Literal(u32),
    Class {
        negate: bool,
        ranges: Vec<(u32, u32)>,
    },
}

pub struct Pattern(Vec<Token>);
impl Pattern {
    pub fn new(pattern: &str) -> Self {
        let p: Vec<_> = points(pattern).collect();
        let mut tokens = Vec::new();
        let mut i = 0;
        while i < p.len() {
            let c = p[i];
            i += 1;
            match c {
                42 => {
                    if !matches!(tokens.last(), Some(Token::Star)) {
                        tokens.push(Token::Star);
                    }
                }
                63 => tokens.push(Token::Any),
                91 => {
                    let negate = p.get(i) == Some(&33);
                    let start = i + usize::from(negate);
                    let search = start + usize::from(p.get(start) == Some(&93));
                    let Some(end) = p
                        .get(search..)
                        .and_then(|tail| tail.iter().position(|c| *c == 93))
                        .map(|n| n + search)
                    else {
                        tokens.push(Token::Literal(c));
                        continue;
                    };
                    let mut ranges = Vec::new();
                    let mut j = start;
                    while j < end {
                        let first = p[j];
                        if j + 2 < end && p[j + 1] == 45 {
                            ranges.push((first, p[j + 2]));
                            j += 3;
                        } else {
                            ranges.push((first, first));
                            j += 1;
                        }
                    }
                    tokens.push(Token::Class { negate, ranges });
                    i = end + 1;
                }
                _ => tokens.push(Token::Literal(c)),
            }
        }
        Self(tokens)
    }
    pub fn matches(&self, text: &str) -> bool {
        let mut active = vec![false; self.0.len() + 1];
        active[0] = true;
        for (i, token) in self.0.iter().enumerate() {
            if matches!(token, Token::Star) {
                active[i + 1] = active[i];
            }
        }
        for c in points(text) {
            let mut left = active[0];
            active[0] = false;
            for (i, token) in self.0.iter().enumerate() {
                let previous = active[i + 1];
                active[i + 1] = match token {
                    Token::Star => active[i] || previous,
                    Token::Any => left,
                    Token::Literal(v) => left && *v == c,
                    Token::Class { negate, ranges } => {
                        left && (ranges.iter().any(|(a, b)| (*a..=*b).contains(&c)) != *negate)
                    }
                };
                left = previous;
            }
        }
        *active.last().unwrap()
    }
}
