//! Standalone CLI for testing and landing concurrent changes.

mod agree;
mod args;
mod assembly;
mod cache_report;
mod changes;
mod collect;
mod files;
mod footprint;
mod git;
mod github;
mod handback;
mod intake;
mod json;
mod json_paths;
mod land;
mod merge;
mod merge_tree;
mod parity;
mod parity_footprint;
mod parity_intake;
mod parity_land;
mod preview;
mod proof;
mod propose;
mod readers;
mod reference;
mod reports;
mod run;
mod sampling;
mod sha256;
mod snapshot;
mod stage;
mod store;
mod sys;

use std::process::ExitCode;

type Native = fn(&[String]) -> i32;

const NATIVE: &[(&str, Native)] = &[
    ("collect", collect::command),
    ("intake", intake::command),
    ("proof", proof::command),
    ("preview", preview::command),
    ("run", run::command),
    ("agree", agree::command),
    ("trace", footprint::trace_cli::trace),
    ("trace-units", footprint::trace_cli::units),
    ("select", footprint::select),
    ("cached-units", footprint::unit_store::command),
    ("dotnet-units", footprint::dotnet::command),
    ("blame", reports::blame_command),
    ("score-bun", reports::score_command),
    ("reuse", footprint::reuse),
    ("cached-run", footprint::cache::command),
    ("cache-report", cache_report::command),
    ("shadow", footprint::shadow::command),
    ("land", land::command),
    ("propose", propose::command),
    ("stage", stage::command),
];

fn native(name: &str) -> Option<Native> {
    NATIVE.iter().find(|(n, _)| *n == name).map(|(_, f)| *f)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    ExitCode::from(run(&args).clamp(0, 255) as u8)
}

fn run(args: &[String]) -> i32 {
    let (args, python) = match args.split_first() {
        Some((s, rest))
            if matches!(
                s.as_str(),
                "--internal-parity-python" | "--internal-parity-rust"
            ) =>
        {
            parity::set_epoch();
            (rest, s == "--internal-parity-python")
        }
        _ => (args, std::env::var("LIT_IMPL").as_deref() == Ok("python")),
    };
    match args.first().map(String::as_str) {
        Some("--internal-assemble") if args.len() == 2 => assembly::command(&args[1]),
        Some("--internal-outline") if args.len() == 2 => readers::command(&args[1]),
        Some("--internal-symbol-table") if args.len() == 2 => readers::symbols::command(&args[1]),
        Some("--internal-merge-file") if args.len() == 2 => readers::structure::command(&args[1]),
        Some("version" | "--version") if args.len() == 1 => {
            println!("{}", version_line());
            0
        }
        Some("parity") => parity_command(&args[1..]),
        Some("--internal-action-receipt") if args.len() == 2 => {
            footprint::cache::action_receipt(&args[1])
        }
        _ => {
            let session = handback::begin();
            let command = args.first().and_then(|name| native(name));
            let code = match command {
                Some(cmd) if !python => cmd(&args[1..]),
                _ if python => reference::run(args).unwrap_or_else(|e| {
                    eprintln!("{e}");
                    2
                }),
                _ => root(args),
            };
            if let Err(e) = session.finish(&handback::paths(args)) {
                eprintln!("lit: could not give some files back to their folder's owner: {e}");
            }
            code
        }
    }
}

fn version_line() -> String {
    format!(
        "lit {} {}/{}",
        env!("LIT_BUILD_VERSION"),
        std::env::consts::OS,
        std::env::consts::ARCH
    )
}

fn root(args: &[String]) -> i32 {
    if args
        .first()
        .is_some_and(|s| matches!(s.as_str(), "-h" | "--help"))
    {
        print!("{}", include_str!("help/root.txt"));
        return 0;
    }
    let choices = NATIVE
        .iter()
        .map(|(name, _)| format!("'{name}'"))
        .collect::<Vec<_>>()
        .join(", ");
    let message = match args.iter().find(|s| !s.starts_with('-')) {
        Some(name) => format!("argument action: invalid choice: '{name}' (choose from {choices})"),
        None => "the following arguments are required: action".to_string(),
    };
    eprintln!("{}\nconcurrent-merge: error: {message}", args::ROOT_USAGE);
    2
}

const PARITY_USAGE: &str =
    "usage: lit parity [--ignore KEY]... [--epoch N] [--keep] [--] COMMAND [ARGS...]

Runs a lit command with the Python lit and with the Rust lit, each in its own
copy of the current folder, and fails if they differ in exit code, output,
files written or Git refs. Set LIT_REFERENCE to the trusted lit source checkout\nand use Python 3.12 or later for the reference run.";

fn parity_command(mut args: &[String]) -> i32 {
    let mut ignore = Vec::new();
    let mut epoch = "1700000000".to_string();
    let mut keep = false;
    while let Some(flag) = args.first().filter(|a| a.starts_with('-')) {
        match flag.as_str() {
            "--" => {
                args = &args[1..];
                break;
            }
            "-h" | "--help" => {
                println!("{PARITY_USAGE}");
                return 0;
            }
            "--keep" => {
                keep = true;
                args = &args[1..];
            }
            "--ignore" | "--epoch" if args.len() > 1 => {
                if flag == "--ignore" {
                    ignore.push(args[1].clone());
                } else {
                    epoch = args[1].clone();
                }
                args = &args[2..];
            }
            _ => {
                eprintln!("{PARITY_USAGE}");
                return 2;
            }
        }
    }
    if args.is_empty() || args[0] == "parity" {
        eprintln!("{PARITY_USAGE}");
        return 2;
    }
    let (program, source) = match (std::env::current_exe(), std::env::current_dir()) {
        (Ok(p), Ok(s)) => (p, s),
        (Err(e), _) | (_, Err(e)) => {
            eprintln!("lit parity: {e}");
            return 2;
        }
    };
    if let Err(e) = reference::driver() {
        eprintln!("{e}");
        return 2;
    }
    let opts = parity::Options {
        program,
        source,
        args: args.to_vec(),
        epoch,
        ignore,
    };
    let report = match parity::run(&opts) {
        Ok(r) => r,
        Err((e, work)) => {
            eprintln!("lit parity: {e}");
            if let Some(w) = work {
                eprintln!("lit parity: both runs are in {}", w.display());
            }
            return 2;
        }
    };
    let name = &args[0];
    if !report.diffs.is_empty() {
        for d in &report.diffs {
            println!("{d}");
        }
        println!(
            "lit parity: {name} differs in {} ways; both runs are in {}",
            report.diffs.len(),
            report.work.display()
        );
        return 1;
    }
    if keep {
        println!("lit parity: both runs are in {}", report.work.display());
    } else {
        let _ = std::fs::remove_dir_all(&report.work);
    }
    println!(
        "lit parity: {name} matches between Python and Rust (exit {})",
        report.rust.code
    );
    0
}
