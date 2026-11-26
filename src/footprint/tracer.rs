use super::Result;
use crate::{git::Repo, json::Value, run};
use std::{
    ffi::OsString,
    fs,
    path::Path,
    process::{Command, Stdio},
};

const SYSCALLS: &str =
    "trace=%file,getdents,getdents64,connect,clone,clone3,fork,vfork,fchdir,io_uring_setup";
pub fn traced(cwd: &Path, argv: &[OsString], events: &Path, log: Option<&fs::File>) -> Result<i32> {
    let mut cmd = Command::new("strace");
    cmd.args([
        "-f",
        "--seccomp-bpf",
        "-q",
        "-ttt",
        "-xx",
        "-y",
        "-s",
        "65535",
        "-e",
        "signal=none",
        "-e",
        SYSCALLS,
        "-o",
    ])
    .arg(events)
    .arg("--")
    .args(argv)
    .current_dir(cwd);
    if let Some(out) = log {
        cmd.stdout(Stdio::from(out.try_clone().map_err(|e| e.to_string())?))
            .stderr(Stdio::from(out.try_clone().map_err(|e| e.to_string())?));
    }
    let status = cmd.status().map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            "Recording a job footprint requires strace on PATH".into()
        } else {
            e.to_string()
        }
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        Ok(status
            .code()
            .unwrap_or_else(|| -status.signal().unwrap_or(1)))
    }
    #[cfg(not(unix))]
    {
        Ok(status.code().unwrap_or(2))
    }
}
pub fn verify(repo: &Repo, commit: &str, command: &Value) -> Result<Value> {
    let tmp = run::private_dir("footprint-")
        .tempdir()
        .map_err(|e| e.to_string())?;
    let work = tmp.path().join("checkout");
    fs::create_dir(&work).map_err(|e| e.to_string())?;
    run::checkout(repo, &work, commit)?;
    let args = super::compare::strings(command)?
        .into_iter()
        .map(|s| crate::merge_tree::os(&crate::json_paths::bytes(s)?))
        .collect::<Result<Vec<_>>>()?;
    let null = fs::File::options()
        .write(true)
        .open(if cfg!(windows) { "NUL" } else { "/dev/null" })
        .map_err(|e| e.to_string())?;
    let rc = traced(&work, &args, &tmp.path().join("strace.log"), Some(&null))?;
    Ok(Value::Number(rc.to_string()))
}
