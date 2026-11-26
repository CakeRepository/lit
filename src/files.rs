//! Publish complete state files beside their destination, even when two workers write at once.
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

pub fn replace(path: &Path, contents: &[u8]) -> io::Result<()> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let dir = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(dir)?;
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    loop {
        let seq = NEXT.fetch_add(1, Ordering::Relaxed);
        let tmp = dir.join(format!(".lit-{}-{seq}.tmp", std::process::id()));
        let mut output = match options.open(&tmp) {
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            result => result?,
        };
        let written = output.write_all(contents);
        drop(output);
        let result = written.and_then(|()| fs::rename(&tmp, path));
        if result.is_err() {
            let _ = fs::remove_file(tmp);
        }
        return result;
    }
}

pub fn read(path: &Path) -> Result<Vec<u8>, String> {
    fs::read(path).map_err(|e| error(path, e))
}

pub fn error(path: &Path, e: io::Error) -> String {
    match e.kind() {
        io::ErrorKind::NotFound => {
            let name = path.to_string_lossy();
            let quote = if name.contains('\'') && !name.contains('"') {
                '"'
            } else {
                '\''
            };
            let mut shown = String::new();
            for c in name.chars() {
                match c {
                    '\\' => shown.push_str("\\\\"),
                    '\r' => shown.push_str("\\r"),
                    '\n' => shown.push_str("\\n"),
                    '\t' => shown.push_str("\\t"),
                    c if c == quote => {
                        shown.push('\\');
                        shown.push(c);
                    }
                    c if c.is_control() => {
                        use std::fmt::Write;
                        write!(shown, "\\x{:02x}", c as u32).unwrap();
                    }
                    c => shown.push(c),
                }
            }
            format!("[Errno 2] No such file or directory: {quote}{shown}{quote}")
        }
        _ => format!("{}: {e}", path.display()),
    }
}
