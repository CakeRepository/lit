//! A reader identifies byte ranges a three-way structural merge can treat as units.
mod csharp;
mod javascript;
pub(crate) use javascript::symbols;
mod json_reader;
mod markdown;
mod powershell;
mod python;
pub(crate) mod structure;
mod yaml;

use std::fmt::Write;
use std::io::{self, Read};

struct Node {
    // JSON member names can contain escaped, unpaired UTF-16 surrogates.
    key: Vec<u16>,
    start: usize,
    end: usize,
    children: Vec<Node>,
}

fn write_nodes(nodes: &[Node], out: &mut String) {
    // Preorder rows keep a deep source outline shallow in the JSON transport.
    let mut levels = vec![nodes.iter()];
    out.push('[');
    let mut first = true;
    while let Some(siblings) = levels.last_mut() {
        let Some(node) = siblings.next() else {
            levels.pop();
            continue;
        };
        if !first {
            out.push(',');
        }
        first = false;
        write!(out, "[{},\"", levels.len() - 1).unwrap();
        for &unit in &node.key {
            match unit {
                0x20..=0x7e if unit != 0x22 && unit != 0x5c => out.push(unit as u8 as char),
                _ => write!(out, "\\u{unit:04x}").unwrap(),
            }
        }
        write!(out, "\",{},{}]", node.start, node.end).unwrap();
        levels.push(node.children.iter());
    }
    out.push(']');
}

enum Reader {
    Dialect(fn(&str, &str) -> Option<Vec<Node>>),
    Text(fn(&str) -> Option<Vec<Node>>),
    Encoded(fn(&[u8]) -> Option<Vec<Node>>),
}
struct Language {
    name: &'static str,
    suffixes: &'static [&'static str],
    reader: Reader,
}

const LANGUAGES: &[Language] = &[
    Language {
        name: "javascript",
        suffixes: &[".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"],
        reader: Reader::Dialect(javascript::outline),
    },
    Language {
        name: "powershell",
        suffixes: &[".ps1", ".psm1", ".psd1"],
        reader: Reader::Encoded(powershell::outline),
    },
    Language {
        name: "markdown",
        suffixes: &[".md", ".markdown"],
        reader: Reader::Text(markdown::outline),
    },
    Language {
        name: "json",
        suffixes: &[".json"],
        reader: Reader::Text(json_reader::outline),
    },
    Language {
        name: "csharp",
        suffixes: &[".cs"],
        reader: Reader::Text(csharp::outline),
    },
    Language {
        name: "yaml",
        suffixes: &[".yml", ".yaml"],
        reader: Reader::Text(yaml::outline),
    },
    Language {
        name: "python",
        suffixes: &[".py", ".pyi", ".pyw"],
        reader: Reader::Encoded(python::outline),
    },
];

pub fn suffixes() -> Vec<&'static str> {
    LANGUAGES
        .iter()
        .flat_map(|l| l.suffixes.iter().copied())
        .collect()
}

fn find(suffix: &str) -> Option<&'static Language> {
    LANGUAGES.iter().find(|l| l.suffixes.contains(&suffix))
}

pub fn language(suffix: &str) -> Option<&'static str> {
    Some(find(suffix)?.name)
}

fn outline(suffix: &str, bytes: &[u8]) -> Option<Vec<Node>> {
    let reader = &find(suffix)?.reader;
    if bytes.contains(&0) {
        return None;
    }
    match reader {
        Reader::Dialect(read) => read(std::str::from_utf8(bytes).ok()?, suffix),
        Reader::Text(read) => read(std::str::from_utf8(bytes).ok()?),
        Reader::Encoded(read) => read(bytes),
    }
}

pub fn command(suffix: &str) -> i32 {
    if suffix == "--list" {
        println!("{}", suffixes().join(" "));
        return 0;
    }
    let mut bytes = Vec::new();
    if let Err(error) = io::stdin().read_to_end(&mut bytes) {
        eprintln!("lit: could not read the source: {error}");
        return 2;
    }
    if let Some(nodes) = outline(suffix, &bytes) {
        let mut result = String::new();
        write_nodes(&nodes, &mut result);
        println!("{result}");
    } else {
        println!("null");
    }
    0
}
