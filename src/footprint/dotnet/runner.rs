use super::*;
use std::{
    io::Write,
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    time::Instant,
};

pub(super) const CALLS: &str = "open,openat,openat2,creat,stat,lstat,newfstatat,statx,access,faccessat,faccessat2,readlink,readlinkat";
pub(super) fn seconds(start: Instant) -> Value {
    Value::Number(format!("{:.1}", start.elapsed().as_secs_f64()))
}
pub(super) struct Runner<'a> {
    pub template: &'a [String],
    pub root: &'a Path,
    pub inputs: inputs::Inputs<'a>,
}
pub(super) struct Class {
    pub name: String,
    pub files: Map,
    pub status: i32,
    pub elapsed: Value,
    log: Vec<u8>,
}
impl Runner<'_> {
    pub fn run(
        &self,
        names: &[String],
        all: bool,
        log: Option<&mut fs::File>,
        trace: Option<&Path>,
    ) -> Result<(i32, Value)> {
        let mut argv = Vec::<std::ffi::OsString>::new();
        if let Some(trace) = trace {
            argv.extend(["strace", "-f", "-qq", "-xx", "--seccomp-bpf", "-e"].map(Into::into));
            argv.extend([format!("trace={CALLS}").into(), "-o".into(), trace.into()]);
        }
        for part in self.template {
            if part == "{filter}" {
                if !all {
                    argv.push("--filter".into());
                    argv.push(
                        names
                            .iter()
                            .map(|n| format!("FullyQualifiedName~{n}."))
                            .collect::<Vec<_>>()
                            .join("|")
                            .into(),
                    );
                }
            } else {
                argv.push(part.into());
            }
        }
        let (program, args) = argv.split_first().ok_or("Missing .NET test command")?;
        let mut cmd = Command::new(program);
        cmd.args(args).current_dir(self.root);
        if let Some(log) = log {
            writeln!(
                log,
                "$ {}",
                argv.iter()
                    .map(|s| format!("{s:?}"))
                    .collect::<Vec<_>>()
                    .join(" ")
            )
            .map_err(io)?;
            cmd.stdout(Stdio::from(log.try_clone().map_err(io)?))
                .stderr(Stdio::from(log.try_clone().map_err(io)?));
        }
        let start = Instant::now();
        let code = crate::parity::exit_code(&cmd.status().map_err(io)?);
        Ok((code, seconds(start)))
    }
    fn class(&self, name: &str) -> Result<Class> {
        let tmp = crate::run::private_dir("lit-dotnet-class-")
            .tempdir()
            .map_err(io)?;
        let trace = tmp.path().join("strace.log");
        let log_path = tmp.path().join("run.log");
        let mut log = fs::File::create(&log_path).map_err(io)?;
        let (status, elapsed) =
            self.run(&[name.to_owned()], false, Some(&mut log), Some(&trace))?;
        let files = self.inputs.observed(&trace)?;
        let log = if status == 0 {
            Vec::new()
        } else {
            let v = fs::read(log_path).map_err(io)?;
            v[v.len().saturating_sub(20000)..].to_vec()
        };
        Ok(Class {
            name: name.into(),
            files,
            status,
            elapsed,
            log,
        })
    }
    pub fn record(
        &self,
        names: &[String],
        jobs: usize,
        log: &mut Option<fs::File>,
        mut done: impl FnMut(Class) -> Result<()>,
    ) -> Result<Value> {
        let start = Instant::now();
        let next = AtomicUsize::new(0);
        std::thread::scope(|scope| -> Result<()> {
            let (tx, rx) = mpsc::channel();
            for _ in 0..jobs.max(1).min(names.len()) {
                let tx = tx.clone();
                let next = &next;
                scope.spawn(move || {
                    while let Some(name) = names.get(next.fetch_add(1, Ordering::Relaxed)) {
                        if tx.send(self.class(name)).is_err() {
                            break;
                        }
                    }
                });
            }
            drop(tx);
            for result in rx {
                let c = result?;
                if let Some(log) = log {
                    writeln!(
                        log,
                        "--- {}: exit {} in {}s, {} data inputs",
                        c.name,
                        c.status,
                        crate::json::canonical(&c.elapsed),
                        c.files.len()
                    )
                    .map_err(io)?;
                    log.write_all(&c.log).map_err(io)?;
                }
                done(c)?;
            }
            Ok(())
        })?;
        Ok(seconds(start))
    }
}
