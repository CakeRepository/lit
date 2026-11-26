//! A reader identifies byte ranges a three-way structural merge can treat as units.
mod json_reader;
mod markdown;

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

type Reader = fn(&str) -> Option<Vec<Node>>;
const READERS: &[(&[&str], Reader)] = &[
    (&[".md", ".markdown"], markdown::outline),
    (&[".json"], json_reader::outline),
];

pub fn suffixes() -> Vec<&'static str> {
    READERS
        .iter()
        .flat_map(|(exts, _)| exts.iter().copied())
        .collect()
}

fn outline(suffix: &str, bytes: &[u8]) -> Option<Vec<Node>> {
    let (_, reader) = READERS.iter().find(|(exts, _)| exts.contains(&suffix))?;
    if bytes.contains(&0) {
        return None;
    }
    reader(std::str::from_utf8(bytes).ok()?)
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
