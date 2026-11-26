//! Reuse a content digest only after a file's size and both timestamps have settled.
use super::Result;
use crate::{files, json::Value, json_paths as paths, sha256::Sha256};
use std::{
    cell::RefCell,
    collections::{BTreeMap, VecDeque},
    fs,
    io::Read,
    path::Path,
};
thread_local! {static CACHE:RefCell<Cache>=RefCell::new(Cache::default());}
const LIMIT: usize = 200000;
#[derive(Default)]
struct Cache {
    by_key: BTreeMap<String, Value>,
    order: VecDeque<String>,
}
impl Cache {
    fn put(&mut self, key: String, hash: String) {
        if !self.by_key.contains_key(&key) {
            self.order.push_back(key.clone());
        }
        self.by_key.insert(key, Value::String(hash));
        while self.order.len() > LIMIT {
            self.by_key.remove(&self.order.pop_front().unwrap());
        }
    }
}
pub fn load(path: &Path) {
    let mut cache = Cache::default();
    if let Ok(m) = paths::ordered_object(path) {
        for (key, value) in m {
            if let Value::String(v) = value {
                cache.put(key, v);
            }
        }
    }
    CACHE.with(|c| *c.borrow_mut() = cache);
}
pub fn flush(path: &Path) -> Result<()> {
    CACHE.with(|c| {
        let c = c.borrow();
        let mut out = String::from("{");
        for (i, key) in c.order.iter().enumerate() {
            if i != 0 {
                out.push(',');
            }
            out.push_str("\n  ");
            out.push_str(&paths::sorted(&Value::String(key.clone()), false));
            out.push_str(": ");
            out.push_str(&paths::sorted(&c.by_key[key], false));
        }
        if !c.order.is_empty() {
            out.push('\n');
        }
        out.push_str("}\n");
        files::replace(path, out.as_bytes()).map_err(|e| files::error(path, e))
    })
}
#[cfg(unix)]
fn key(path: &Path, st: &fs::Metadata) -> Option<String> {
    use std::{
        os::unix::fs::MetadataExt,
        time::{SystemTime, UNIX_EPOCH},
    };
    let m = i128::from(st.mtime()) * 1_000_000_000 + i128::from(st.mtime_nsec());
    let c = i128::from(st.ctime()) * 1_000_000_000 + i128::from(st.ctime_nsec());
    let now = i128::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .ok()?
            .as_nanos(),
    )
    .ok()?;
    (now - c.max(m) > 2_000_000_000).then(|| {
        format!(
            "{}\0{}\0{}\0{}\0{m}\0{c}",
            paths::path_key(path.as_os_str().as_encoded_bytes()),
            st.dev(),
            st.ino(),
            st.size()
        )
    })
}
#[cfg(not(unix))]
fn key(_path: &Path, _st: &fs::Metadata) -> Option<String> {
    // The portable API lacks a Unix-style change time. Hash from the open handle.
    None
}
pub fn file(path: &Path) -> String {
    let Ok(meta) = fs::metadata(path) else {
        return "absent".into();
    };
    if meta.is_dir() {
        return "dir".into();
    }
    if !meta.is_file() {
        return "other".into();
    }
    let Ok(mut file) = fs::File::open(path) else {
        return "unreadable".into();
    };
    let stable = file.metadata().ok().and_then(|st| key(path, &st));
    if let Some(digest) = stable
        .as_ref()
        .and_then(|k| CACHE.with(|c| c.borrow().by_key.get(k)?.string().ok().map(str::to_owned)))
    {
        return digest;
    }
    let mut sum = Sha256::new();
    let mut buf = vec![0; 1 << 16];
    loop {
        match file.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => sum.update(&buf[..n]),
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return "unreadable".into(),
        }
    }
    let hash = sum.hex();
    if let Some(before) = stable {
        if file.metadata().ok().and_then(|st| key(path, &st)).as_ref() == Some(&before) {
            CACHE.with(|c| c.borrow_mut().put(before, hash.clone()));
        }
    }
    hash
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recent_same_size_rewrites_are_read_again_and_old_cache_order_survives() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("input");
        let db = dir.path().join("digests.json");
        fs::write(&db, r#"{"z":"first","a":"second"}"#).unwrap();
        load(&db);
        fs::write(&file, b"aaa").unwrap();
        let modified = fs::metadata(&file).unwrap().modified().unwrap();
        let a = super::file(&file);
        fs::write(&file, b"bbb").unwrap();
        fs::File::options()
            .write(true)
            .open(&file)
            .unwrap()
            .set_modified(modified)
            .unwrap();
        assert_ne!(a, super::file(&file));
        flush(&db).unwrap();
        let keys: Vec<_> = paths::ordered_object(&db)
            .unwrap()
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        assert_eq!(keys, ["z", "a"]);
    }
}
