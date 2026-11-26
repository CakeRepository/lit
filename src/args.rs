//! Parse the options shared by the native commands. Long options accept --key=value.
use std::collections::BTreeMap;

pub struct Args<'a> {
    pub positional: Vec<&'a str>,
    values: BTreeMap<&'static str, Vec<&'a str>>,
}

impl<'a> Args<'a> {
    pub fn parse(args: &'a [String], options: &[&'static str]) -> Result<Self, String> {
        let mut parsed = Self {
            positional: Vec::new(),
            values: BTreeMap::new(),
        };
        let mut args = args.iter().map(String::as_str).peekable();
        while let Some(arg) = args.next() {
            if arg == "--" {
                parsed.positional.extend(args);
                break;
            }
            if !arg.starts_with('-') || arg == "-" {
                parsed.positional.push(arg);
                continue;
            }
            let (name, inline) = arg
                .split_once('=')
                .map_or((arg, None), |(n, v)| (n, Some(v)));
            let key = options
                .iter()
                .copied()
                .find(|&o| o == name)
                .or_else(|| {
                    let mut matches = options
                        .iter()
                        .copied()
                        .filter(|o| name.starts_with("--") && o.starts_with(name));
                    let first = matches.next()?;
                    matches.next().is_none().then_some(first)
                })
                .ok_or_else(|| format!("unrecognized argument: {name}"))?;
            let value = match inline {
                Some(value) => value,
                None => args
                    .next_if(|v| !v.starts_with('-') || *v == "-")
                    .ok_or_else(|| format!("argument {key}: expected one argument"))?,
            };
            parsed.values.entry(key).or_default().push(value);
        }
        Ok(parsed)
    }

    pub fn required(&self, key: &str) -> Result<&'a str, String> {
        self.values
            .get(key)
            .and_then(|values| values.last().copied())
            .ok_or_else(|| format!("the following arguments are required: {key}"))
    }
}
