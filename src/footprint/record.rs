use super::{
    compare::environment,
    events::{ObservedUnit, Trace},
    glob::Pattern,
    summary::{array, summarize, EPHEMERAL},
    tracer::traced,
    workspace::{self, Workspace},
    Result,
};
use crate::{
    files,
    git::Repo,
    json::{object, Value},
    json_paths as paths, run,
};
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    fs,
    io::BufReader,
    path::{Path, PathBuf},
    process::Command,
    time::Instant,
};
pub const RUN_ENV: &[&str] = &[
    "GITHUB_RUN_*",
    "GITHUB_JOB",
    "GITHUB_ACTION*",
    "GITHUB_OUTPUT",
    "GITHUB_STATE",
    "GITHUB_ENV",
    "GITHUB_PATH",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_SHA",
    "GITHUB_REF*",
    "GITHUB_HEAD_REF",
    "GITHUB_BASE_REF",
    "GITHUB_EVENT_PATH",
    "GITHUB_TRIGGERING_ACTOR",
    "GITHUB_RETENTION_DAYS",
    "RUNNER_TRACKING_ID",
    "ACTIONS_*",
    "INVOCATION_ID",
    "JOURNAL_STREAM",
    "OLDPWD",
    "PWD",
    "SHLVL",
    "_",
];

fn info(name: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(name).args(args).output().ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}
fn runner(tracer: &str) -> Value {
    let machine = info("uname", &["-m"]).unwrap_or_else(|| std::env::consts::ARCH.into());
    let system = info("uname", &["-s"]).unwrap_or_else(|| std::env::consts::OS.into());
    let release = info("uname", &["-r"])
        .or_else(|| {
            fs::read_to_string("/proc/sys/kernel/osrelease")
                .ok()
                .map(|v| v.trim().into())
        })
        .unwrap_or_default();
    let libc = info("getconf", &["GNU_LIBC_VERSION"])
        .unwrap_or_default()
        .replace(' ', "");
    let mut platform = vec![system.as_str(), release.as_str(), machine.as_str()];
    let processor = info("uname", &["-p"]).unwrap_or_default();
    if processor != machine && processor != "unknown" && !processor.is_empty() {
        platform.push(&processor);
    }
    platform.push("with");
    if !libc.is_empty() {
        platform.push(&libc);
    }
    object([
        (
            "platform",
            Value::String(platform.join("-").replace(' ', "_")),
        ),
        ("machine", Value::String(machine)),
        ("tracer", tracer.into()),
    ])
}
fn number(code: i32) -> Value {
    Value::Number(code.to_string())
}
fn millis(s: f64) -> Value {
    Value::Number(format!("{s:.3}"))
}
fn command_value(args: &[String]) -> Value {
    array(args.iter().map(String::as_str))
}
pub struct Recording {
    tmp: tempfile::TempDir,
    dir: PathBuf,
    root: String,
    work: Workspace,
    meta: BTreeMap<String, Value>,
    log: Option<PathBuf>,
}
impl Recording {
    pub fn new(
        repo: &Repo,
        rev: &str,
        workdir: Option<&Path>,
        ignore: &[String],
        log: Option<&Path>,
        units: bool,
    ) -> Result<Self> {
        let version = info("strace", &["-V"]).ok_or(if units {
            "Recording unit footprints requires strace on PATH"
        } else {
            "Recording a job footprint requires strace on PATH"
        })?;
        let started = format!("{:.6}", jiff::Timestamp::now()).replace('Z', "+00:00");
        let work = Workspace::new(repo, rev, workdir)?;
        let tmp = run::private_dir("footprint-")
            .tempdir()
            .map_err(|e| e.to_string())?;
        let dir = if let Some(p) = workdir {
            Repo::new(p)?.path().to_owned()
        } else {
            let dir = tmp.path().join("checkout");
            fs::create_dir(&dir).map_err(|e| e.to_string())?;
            run::checkout(repo, &dir, &work.commit)?;
            dir
        };
        let root = paths::path_key(
            dir.canonicalize()
                .map_err(|e| e.to_string())?
                .as_os_str()
                .as_encoded_bytes(),
        );
        let patterns = ignore.iter().map(|s| Pattern::new(s)).collect::<Vec<_>>();
        let meta = object([
            (
                "kind",
                if units {
                    "lit-unit-footprints"
                } else {
                    "lit-job-footprint"
                }
                .into(),
            ),
            ("version", 1u64.into()),
            ("commit", work.commit.as_str().into()),
            ("tree", work.entries[""].oid.clone()),
            ("in_place", Value::Bool(workdir.is_some())),
            ("started", Value::String(started)),
            (
                "runner",
                runner(version.lines().next().unwrap_or("unknown")),
            ),
            ("environment", Value::Object(environment(&patterns))),
            ("ignored_environment", command_value(ignore)),
            ("ephemeral_prefixes", array(EPHEMERAL)),
        ]);
        Ok(Self {
            tmp,
            dir,
            root,
            work,
            meta: meta.object()?.clone(),
            log: log.map(Path::to_owned),
        })
    }
    fn run(
        &self,
        args: &[String],
        cwd: &str,
        record: bool,
        append: bool,
    ) -> Result<(Trace, i32, f64)> {
        let trace = self.tmp.path().join("strace.log");
        let command = args
            .iter()
            .map(|s| crate::merge_tree::os(&paths::bytes(s)?))
            .collect::<Result<Vec<OsString>>>()?;
        let out = self
            .log
            .as_ref()
            .map(|p| {
                fs::OpenOptions::new()
                    .write(true)
                    .create(true)
                    .append(append)
                    .truncate(!append)
                    .open(p)
                    .map_err(|e| files::error(p, e))
            })
            .transpose()?;
        let clock = Instant::now();
        let code = traced(
            &self.dir.join(workspace::path(cwd)?),
            &command,
            &trace,
            out.as_ref(),
        )?;
        let seconds = clock.elapsed().as_secs_f64();
        let events = Trace::parse(
            BufReader::new(fs::File::open(&trace).map_err(|e| files::error(&trace, e))?),
            record,
        )?;
        Ok((events, code, seconds))
    }
    pub fn job(mut self, args: &[String]) -> Result<Value> {
        let (trace, code, seconds) = self.run(args, "", false, false)?;
        let data = summarize(
            &trace.all,
            &mut self.work,
            &self.root,
            &BTreeSet::new(),
            &[],
        )?;
        self.meta.extend(data.object()?.clone());
        self.meta.insert("command".into(), command_value(args));
        self.meta.insert("exit_code".into(), number(code));
        self.meta.insert("seconds".into(), millis(seconds));
        Ok(Value::Object(self.meta))
    }
    pub fn units(mut self, source: UnitSource<'_>, cwd: &str, scratch: &[String]) -> Result<Value> {
        let scratch = scratch
            .iter()
            .map(|s| {
                let rel = s.trim_matches('/');
                if rel.split('/').any(|s| matches!(s, "" | "." | ".."))
                    || rel.split('/').next() == Some(".git")
                {
                    return Err(format!(
                        "A scratch directory is a plain path inside the repository, got '{rel}'"
                    ));
                }
                Ok(rel.to_owned())
            })
            .collect::<Result<Vec<_>>>()?;
        let absolute = scratch
            .iter()
            .map(|s| format!("{}/{s}", self.root))
            .collect::<Vec<_>>();
        let clock = Instant::now();
        let mut observed = Bundle::default();
        let (command, pattern, each, code) = match source {
            UnitSource::Command(args, pat) => {
                let (trace, code, _) = self.run(args, "", true, false)?;
                for u in trace.units(pat)? {
                    observed.keep(u, &mut self.work, &self.root, &absolute)?;
                }
                (command_value(args), pat.into(), Value::Null, code)
            }
            UnitSource::Each(commands) => {
                let mut each = BTreeMap::new();
                let mut code = 0;
                for (name, argv) in commands {
                    let (trace, rc, seconds) = self.run(argv, cwd, false, true)?;
                    if code == 0 {
                        code = rc;
                    }
                    observed.keep(
                        ObservedUnit {
                            name: name.clone(),
                            events: trace.all,
                            code: Some(rc),
                            seconds: Some(seconds),
                        },
                        &mut self.work,
                        &self.root,
                        &absolute,
                    )?;
                    each.insert(name.clone(), command_value(argv));
                }
                (
                    Value::Null,
                    Value::Null,
                    if each.is_empty() {
                        Value::Null
                    } else {
                        Value::Object(each)
                    },
                    code,
                )
            }
        };
        let seconds = millis(clock.elapsed().as_secs_f64());
        let mut children = BTreeMap::new();
        for directory in observed.listings {
            let children_here = self
                .work
                .children
                .get(&directory)
                .map(Vec::as_slice)
                .unwrap_or_default();
            children.insert(directory, command_value(children_here));
        }
        self.meta.extend(
            [
                ("command", command),
                ("pattern", pattern),
                ("each", each),
                ("cwd", cwd.into()),
                ("exit_code", number(code)),
                ("seconds", seconds),
                ("scratch", command_value(&scratch)),
                ("tracked", Value::Object(observed.tracked)),
                ("external", Value::Object(observed.external)),
                ("listing_children", Value::Object(children)),
                ("units", Value::Object(observed.units)),
            ]
            .map(|(k, v)| (k.into(), v)),
        );
        Ok(Value::Object(self.meta))
    }
}
pub enum UnitSource<'a> {
    Command(&'a [String], &'a str),
    Each(&'a [(String, Vec<String>)]),
}
#[derive(Default)]
struct Bundle {
    units: BTreeMap<String, Value>,
    tracked: BTreeMap<String, Value>,
    external: BTreeMap<String, Value>,
    listings: BTreeSet<String>,
    produced: BTreeSet<String>,
}
impl Bundle {
    fn keep(
        &mut self,
        u: ObservedUnit,
        work: &mut Workspace,
        root: &str,
        scratch: &[String],
    ) -> Result<()> {
        let Value::Object(mut v) = summarize(&u.events, work, root, &self.produced, scratch)?
        else {
            unreachable!()
        };
        self.produced.extend(
            u.events
                .written
                .into_iter()
                .filter(|p| !scratch.iter().any(|s| super::compare::under(p, s))),
        );
        self.listings
            .extend(v["listings"].object()?.keys().cloned());
        for (k, all) in [
            ("tracked", &mut self.tracked),
            ("external", &mut self.external),
        ] {
            let Some(Value::Object(fields)) = v.remove(k) else {
                unreachable!()
            };
            v.insert(k.into(), super::names(super::sorted(fields.keys())));
            all.extend(fields);
        }
        v.insert(
            "exit_code".into(),
            u.code.map(number).unwrap_or(Value::Null),
        );
        v.insert(
            "seconds".into(),
            u.seconds.map(millis).unwrap_or(Value::Null),
        );
        self.units.insert(u.name, Value::Object(v));
        Ok(())
    }
}
