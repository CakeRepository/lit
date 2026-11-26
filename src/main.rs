//! lit as one binary.
//!
//! Commands move from Python to Rust one at a time (CakeRepository/lit#35). A
//! command that has not moved yet runs the Python lit embedded in this binary,
//! so the binary already does everything concurrent-merge does.

mod handback;
mod json;
mod parity;
mod pyfallback;
mod sha256;
mod sys;

use std::process::ExitCode;

mod embedded {
    include!(concat!(env!("OUT_DIR"), "/embedded.rs"));
}

type Native = fn(&[String]) -> i32;

/// The commands written in Rust. Every other command runs on the embedded
/// Python. A command joins this table once its parity cases match.
const NATIVE: &[(&str, Native)] = &[];

fn native(name: &str) -> Option<Native> {
    NATIVE.iter().find(|(n, _)| *n == name).map(|(_, f)| *f)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    ExitCode::from(run(&args).clamp(0, 255) as u8)
}

fn run(args: &[String]) -> i32 {
    match args.first().map(String::as_str) {
        Some("version" | "--version") if args.len() == 1 => {
            println!("{}", version_line());
            0
        }
        Some("parity") => parity_command(&args[1..]),
        Some(name) => match native(name) {
            // LIT_IMPL=python forces the Python lit; lit parity sets it on one side.
            Some(cmd) if std::env::var("LIT_IMPL").as_deref() != Ok("python") => cmd(&args[1..]),
            _ => fallback(args),
        },
        None => fallback(args),
    }
}

fn version_line() -> String {
    let digest = pyfallback::digest(embedded::FILES);
    format!(
        "lit {} {}/{} (embedded python {})",
        env!("LIT_BUILD_VERSION"),
        std::env::consts::OS,
        std::env::consts::ARCH,
        &digest[..12]
    )
}

/// Runs args with the embedded Python lit.
fn fallback(args: &[String]) -> i32 {
    let session = handback::begin();
    let code = fallback_inner(args);
    if let Err(e) = session.finish(&handback::paths(args)) {
        eprintln!("lit: could not give some files back to their folder's owner: {e}");
    }
    code
}

fn fallback_inner(args: &[String]) -> i32 {
    let python = match pyfallback::python() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let dir = match pyfallback::extract(embedded::FILES, &std::env::temp_dir()) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("lit: could not prepare the embedded Python: {e}");
            return 2;
        }
    };
    let mut child = match pyfallback::command(&python, &dir, args).spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("lit: {e}");
            return 2;
        }
    };
    #[cfg(unix)]
    sys::forward_signals_to(child.id());
    match child.wait() {
        Ok(status) => parity::exit_code(&status),
        Err(e) => {
            eprintln!("lit: {e}");
            2
        }
    }
}

const PARITY_USAGE: &str =
    "usage: lit parity [--ignore KEY]... [--epoch N] [--keep] [--] COMMAND [ARGS...]

Runs a lit command with the Python lit and with the Rust lit, each in its own
copy of the current folder, and fails if they differ in exit code, output,
files written or Git refs. A command not in Rust yet runs on Python both times,
which checks that it gives the same result twice.";

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
    let opts = parity::Options {
        program,
        source,
        args: args.to_vec(),
        epoch,
        ignore,
        native: native(&args[0]).is_some(),
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
    if opts.native {
        println!(
            "lit parity: {name} matches between Python and Rust (exit {})",
            report.rust.code
        );
    } else {
        println!(
            "lit parity: {name} is not in Rust yet, so Python ran twice; the runs match (exit {})",
            report.python.code
        );
    }
    0
}
