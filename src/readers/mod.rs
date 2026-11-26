//! A reader identifies byte ranges a three-way structural merge can treat as units.
mod markdown;

use crate::json::{self, Value};
use std::io::{self, Read};

struct Node {
    key: String,
    start: usize,
    end: usize,
    children: Vec<Node>,
}

impl Node {
    fn into_json(self) -> Value {
        Value::Array(vec![
            Value::String(self.key),
            (self.start as u64).into(),
            (self.end as u64).into(),
            Value::Array(self.children.into_iter().map(Node::into_json).collect()),
        ])
    }
}

type Reader = fn(&str) -> Option<Vec<Node>>;
const READERS: &[(&[&str], Reader)] = &[(&[".md", ".markdown"], markdown::outline)];

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
    let result = outline(suffix, &bytes).map_or(Value::Null, |nodes| {
        Value::Array(nodes.into_iter().map(Node::into_json).collect())
    });
    println!("{}", json::pretty(&result));
    0
}
