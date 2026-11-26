use fancy_regex::Regex;
use std::sync::LazyLock;

fn pattern(source: &str) -> Regex {
    Regex::new(&format!("^(?:{source})")).expect("JavaScript token pattern")
}

pub(super) static IDENT: LazyLock<Regex> =
    LazyLock::new(|| pattern(r##"#?(?:[\p{L}\p{Nl}\p{No}_$])[\p{L}\p{N}_$]*"##));
pub(super) static NUMBER: LazyLock<Regex> = LazyLock::new(|| {
    pattern(
        r##"(?:0[xXbBoO][0-9a-fA-F_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?)n?"##,
    )
});
pub(super) static PUNCT: LazyLock<Regex> = LazyLock::new(|| {
    pattern(
        r##">>>=|\.\.\.|===|!==|\*\*=|<<=|>>=|>>>|&&=|\|\|=|\?\?=|=>|==|!=|<=|>=|&&|\|\||\?\?|\?\.(?!\d)|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|\*\*|<<|>>|[{}()\[\];,<>+\-*/%&|^!~?:=.@]"##,
    )
});
pub(super) static SQ: LazyLock<Regex> =
    LazyLock::new(|| pattern(r##"'(?:[^'\\\n\r]|\\(?:\r\n|[\s\S]))*'"##));
pub(super) static DQ: LazyLock<Regex> =
    LazyLock::new(|| pattern(r##""(?:[^"\\\n\r]|\\(?:\r\n|[\s\S]))*""##));
pub(super) static REGEX: LazyLock<Regex> = LazyLock::new(|| {
    pattern(r##"/(?![*/])(?:[^/\\\[\n\r]|\\[^\n\r]|\[(?:[^\]\\\n\r]|\\[^\n\r])*\])+/[A-Za-z]*"##)
});
pub(super) static JSX_NAME: LazyLock<Regex> =
    LazyLock::new(|| pattern(r##"[\p{L}\p{N}_$][\p{L}\p{N}_$.:\-]*"##));
pub(super) static TSX_GENERIC: LazyLock<Regex> =
    LazyLock::new(|| pattern(r##"<\s*[A-Za-z_$][\w$]*\s*(?:,|extends\b)"##));
