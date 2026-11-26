//! Parse the options shared by the native commands. Long options accept --key=value.
use std::collections::BTreeMap;

pub struct Args<'a> {
    pub positional: Vec<&'a str>,
    values: BTreeMap<&'static str, Vec<&'a str>>,
}

impl<'a> Args<'a> {
    pub fn parse(args: &'a [String], options: &[&'static str]) -> Result<Self, String> {
        Self::with_options(args, options, &[], &[])
    }

    pub fn with_options(
        args: &'a [String],
        options: &[&'static str],
        flags: &[&'static str],
        multiple: &[&'static str],
    ) -> Result<Self, String> {
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
            let all: Vec<_> = options
                .iter()
                .chain(flags)
                .chain(multiple)
                .copied()
                .collect();
            let key = all
                .iter()
                .copied()
                .find(|&o| o == name)
                .or_else(|| {
                    let mut matches = all
                        .iter()
                        .copied()
                        .filter(|o| name.starts_with("--") && o.starts_with(name));
                    let first = matches.next()?;
                    matches.next().is_none().then_some(first)
                })
                .ok_or_else(|| format!("unrecognized argument: {name}"))?;
            if flags.contains(&key) {
                if inline.is_some() {
                    return Err(format!("argument {key}: ignored explicit argument"));
                }
                parsed.values.insert(key, Vec::new());
                continue;
            }
            let is_value = |v: &&str| !v.starts_with('-') || *v == "-" || v.parse::<i64>().is_ok();
            let value = match inline {
                Some(value) => value,
                None => args
                    .next_if(is_value)
                    .ok_or_else(|| format!("argument {key}: expected one argument"))?,
            };
            if multiple.contains(&key) {
                let mut values = vec![value];
                if inline.is_none() {
                    while let Some(v) = args.next_if(is_value) {
                        values.push(v);
                    }
                }
                parsed.values.insert(key, values);
            } else {
                parsed.values.entry(key).or_default().push(value);
            }
        }
        Ok(parsed)
    }

    pub fn has(&self, key: &str) -> bool {
        self.values.contains_key(key)
    }
    pub fn all(&self, key: &str) -> &[&'a str] {
        self.values.get(key).map(Vec::as_slice).unwrap_or_default()
    }
    pub fn optional(&self, key: &str) -> Option<&'a str> {
        self.all(key).last().copied()
    }

    pub fn required(&self, key: &str) -> Result<&'a str, String> {
        self.values
            .get(key)
            .and_then(|values| values.last().copied())
            .ok_or_else(|| format!("the following arguments are required: {key}"))
    }
}
