use std::path::{Path, PathBuf};

pub fn job_dir(store: &Path, job: &str) -> Result<PathBuf, String> {
    if job.is_empty()
        || matches!(job, "." | "..")
        || !job
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return Err("Job names use letters, digits, '.', '_' and '-'".into());
    }
    Ok(store.join(job))
}
