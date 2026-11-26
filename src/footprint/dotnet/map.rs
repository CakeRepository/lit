use super::*;
use std::{ffi::OsString, io::Write, process::Command};

const SOURCES: &[(&str, &[u8])] = &[
    (
        "LitIlMap.csproj",
        include_bytes!("../../../concurrent_merge/dotnet/LitIlMap.csproj"),
    ),
    (
        "Program.cs",
        include_bytes!("../../../concurrent_merge/dotnet/Program.cs"),
    ),
];

fn tool(store: &Path, log: &mut Option<fs::File>) -> Result<PathBuf> {
    let version: String = SOURCES
        .iter()
        .map(|(n, b)| format!("{n}{}", sha256::hex(b)))
        .collect();
    let tools = store.join("tools");
    let out = tools.join(format!(
        "litilmap-{}",
        &sha256::hex(version.as_bytes())[..16]
    ));
    let dll = out.join("LitIlMap.dll");
    if dll.is_file() {
        return Ok(dll);
    }
    fs::create_dir_all(&tools).map_err(io)?;
    let src = crate::run::private_dir("litilmap-").tempdir().map_err(io)?;
    // Publish all runtime files together. Two first users can safely build the same version.
    let built = crate::run::private_dir(".litilmap-")
        .tempdir_in(&tools)
        .map_err(io)?;
    for (name, data) in SOURCES {
        fs::write(src.path().join(name), data).map_err(io)?;
    }
    let result = Command::new("dotnet")
        .arg("build")
        .arg(src.path())
        .args(["-c", "Release", "-o"])
        .arg(built.path().canonicalize().map_err(io)?)
        .args(["-v", "q", "-nologo"])
        .output()
        .map_err(io)?;
    if let Some(log) = log {
        log.write_all(&result.stdout)
            .and_then(|_| log.write_all(&result.stderr))
            .map_err(io)?;
    }
    if !result.status.success() || !built.path().join("LitIlMap.dll").is_file() {
        return Err(format!(
            "Could not build the .NET program map tool:\n{}",
            String::from_utf8_lossy(&result.stdout)
        ));
    }
    if let Err(e) = fs::rename(built.path(), &out) {
        if !dll.is_file() {
            return Err(io(e));
        }
    }
    Ok(dll)
}

pub(super) struct Program {
    pub units: Map,
    pub types: Map,
    pub type_names: Vec<Value>,
    pub globals: Value,
    pub content: Map,
}

fn outputs(
    dir: &Path,
    root: &Path,
    skip: &BTreeSet<OsString>,
    external: &mut Map,
    content: &mut Map,
) -> Result<()> {
    for entry in fs::read_dir(dir).map_err(io)? {
        let entry = entry.map_err(io)?;
        let p = entry.path();
        if entry.file_type().map_err(io)?.is_dir() {
            outputs(&p, root, skip, external, content)?;
            continue;
        }
        if !p.is_file() || skip.contains(&entry.file_name()) {
            continue;
        }
        let ext = p.extension().and_then(|s| s.to_str()).unwrap_or("");
        if matches!(ext, "xml" | "pdb") {
            continue;
        }
        let rel = relative(&p, root)?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        let target = if matches!(ext, "dll" | "so" | "dylib" | "exe")
            || rel.starts_with("runtimes/")
            || (name.ends_with(".json") && name.contains(".runtimeconfig."))
        {
            &mut *external
        } else {
            &mut *content
        };
        target.insert(rel, file_hash(&p)?);
    }
    Ok(())
}

fn info(cmd: &str, args: &[&str]) -> Option<String> {
    Command::new(cmd)
        .args(args)
        .output()
        .ok()
        .map(|v| String::from_utf8_lossy(&v.stdout).trim().into())
}

pub(super) fn load(store: &Path, tests: &Path, log: &mut Option<fs::File>) -> Result<Program> {
    let dll = tool(store, log)?;
    let dir = tests.parent().ok_or("Missing .NET test output folder")?;
    let deps = tests.with_extension("deps.json");
    let data = fs::read_to_string(&deps).map_err(|e| files::error(&deps, e))?;
    let mut packages = Vec::new();
    let mut own = Vec::new();
    let mut skip = BTreeSet::from([deps
        .file_name()
        .ok_or("Missing dependencies filename")?
        .to_owned()]);
    for (key, lib) in crate::json::ordered_field(&data, "libraries")? {
        if lib.get("type") == Some(&"project".into()) {
            let name = key.split('/').next().unwrap();
            let path = dir.join(format!("{name}.dll"));
            if path.is_file() {
                skip.extend([format!("{name}.dll").into(), format!("{name}.pdb").into()]);
                own.push(path);
            }
        } else {
            packages.push(key);
        }
    }
    packages.sort();
    let (mut external, mut content) = (Map::new(), Map::new());
    outputs(dir, dir, &skip, &mut external, &mut content)?;
    let tmp = crate::run::private_dir("lit-dotnet-")
        .tempdir()
        .map_err(io)?;
    let output = tmp.path().join("map.json");
    let mut cmd = Command::new("dotnet");
    cmd.arg(dll)
        .arg("--tests")
        .arg(tests)
        .arg("--out")
        .arg(&output);
    for p in own {
        cmd.arg("--own").arg(p);
    }
    let done = cmd.output().map_err(io)?;
    if !done.status.success() {
        return Err(format!(
            "The .NET program map failed:\n{}",
            String::from_utf8_lossy(&done.stderr)
        ));
    }
    let map = paths::read(&output)?;
    let type_names = map.field("type_names")?.array()?.to_vec();
    let mut types = Map::new();
    for (name, hash) in type_names.iter().zip(map.field("type_hashes")?.array()?) {
        types.insert(name.string()?.into(), hash.clone());
    }
    let sdk = info("dotnet", &["--version"]).unwrap_or_else(|| "?".into());
    let runtimes = info("dotnet", &["--list-runtimes"]).unwrap_or_else(|| "?".into());
    let machine = info("uname", &["-m"]).unwrap_or_else(|| std::env::consts::ARCH.into());
    let runtime = sha256::hex(format!("{sdk}\n{runtimes}\n{machine}").as_bytes());
    let globals = object([
        ("packages", super::super::names(&packages)),
        ("external", Value::Object(external)),
        ("runtime", Value::String(runtime)),
    ]);
    Ok(Program {
        units: map.field("units")?.object()?.clone(),
        types,
        type_names,
        content,
        globals: Value::String(sha256::hex(crate::json::canonical(&globals).as_bytes())),
    })
}
