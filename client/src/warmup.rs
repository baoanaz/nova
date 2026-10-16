//! Background sync coordination. No Git/index policy lives here: use the existing sync pipeline.
//!
//! The OS lock is never unlinked (unlinking a locked inode creates two independent locks).
//! All processes sharing a cache root serialize mutations to the same endpoint/project cache.
//! A ready record is only reusable by the same credentials, canonical workspace and version.

use std::collections::{BTreeMap, HashMap};
use std::fs::{File, OpenOptions, TryLockError};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::sync::watch;
use tokio::task::AbortHandle;

use crate::identity::{repo_identity, RepoIdentity};
use crate::ignore::IgnoreRules;
use crate::remote::{validate_project_root, RemoteClient};
use crate::policy::{FilePolicy, WorkspacePolicy};
use crate::tools::{ToolError, ToolLayer};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct SyncResult {
    pub project_id: String,
    pub checkpoint_id: Option<String>,
    pub empty: bool,
    #[serde(default)]
    pub root: Option<PathBuf>,
}
type Outcome = Result<SyncResult, String>;
type Receiver = watch::Receiver<Option<Outcome>>;

#[derive(Clone, Copy)]
pub(crate) struct Settings {
    pub timeout: Duration,
    pub ready_ttl: Duration,
}

impl Default for Settings {
    fn default() -> Self {
        fn seconds(name: &str, default: u64) -> Duration {
            Duration::from_secs(std::env::var(name).ok()
                .and_then(|value| value.parse::<u64>().ok())
                .filter(|value| *value > 0 && *value <= 3600).unwrap_or(default))
        }
        Self {
            timeout: seconds("NOVA_WARMUP_TIMEOUT_SECS", 600),
            ready_ttl: seconds("NOVA_WARMUP_READY_TTL_SECS", 60),
        }
    }
}

struct Task {
    receiver: Receiver,
    abort: AbortHandle,
}

#[derive(Default)]
pub(crate) struct Coordinator {
    tasks: Mutex<HashMap<String, Task>>,
    watchers: Mutex<HashMap<String, AbortHandle>>,
    closed: AtomicBool,
}

pub(crate) struct Prepared {
    pub root: PathBuf,
    pub identity: RepoIdentity,
    policy: WorkspacePolicy,
    cache_root: PathBuf,
    lock_path: PathBuf,
    state_path: PathBuf,
    scope: String,
}

#[derive(Serialize, Deserialize)]
struct Record {
    schema: u32,
    scope: String,
    version: String,
    finished_ms: u128,
    outcome: Outcome,
}

fn digest(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

fn now_ms() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis()
}

async fn blocking<T: Send + 'static>(work: impl FnOnce() -> Result<T> + Send + 'static) -> Result<T> {
    tokio::task::spawn_blocking(work).await.context("background local operation failed")?
}

impl Coordinator {
    pub async fn ensure(
        self: &Arc<Self>, layer: ToolLayer, remote: RemoteClient, project_root: String,
        cache_root: PathBuf, settings: Settings,
    ) -> Result<SyncResult, ToolError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(ToolError::Failed("MCP 已关闭，后台同步已取消".into()));
        }
        // Validate before any HTTP, but keep filesystem and Git commands off the MCP loop.
        let endpoint = remote.base_url().to_string();
        let credentials = remote.coordination_scope();
        let deadline = tokio::time::Instant::now() + settings.timeout;
        let cancelled = Arc::new(AtomicBool::new(false));
        let _cancel_preparation = CancellationGuard(cancelled.clone());
        let prepared = tokio::time::timeout_at(deadline, blocking(move || {
            if cancelled.load(Ordering::Acquire) { return Err(anyhow!("工作区准备已取消")); }
            let root = validate_project_root(&project_root)?;
            let root = std::fs::canonicalize(root).context("cannot canonicalize project_root")?;
            let identity = repo_identity(&root);
            let policy = WorkspacePolicy::detect(&root, &cache_root, identity.remote_url.is_some())?;
            if cancelled.load(Ordering::Acquire) { return Err(anyhow!("工作区准备已取消")); }
            let lock_key = digest(format!("{endpoint}\0{}", identity.identity_key).as_bytes());
            let directory = cache_root.join("warmup-v1");
            let scope = digest(format!("{credentials}\0{}\0{}", root.to_string_lossy(), policy.fingerprint()).as_bytes());
            Ok(Prepared {
                root, identity, scope, policy, cache_root,
                lock_path: directory.join(format!("{lock_key}.lock")),
                state_path: directory.join(format!("{lock_key}.json")),
            })
        })).await.map_err(|_| ToolError::Failed("工作区准备超时，可重试".into()))?
            .map_err(|error| ToolError::InvalidArguments(format!("工作区校验失败：{error:#}")))?;
        let mut settings = settings;
        settings.timeout = deadline.saturating_duration_since(tokio::time::Instant::now());
        let task_key = format!("{}:{}", prepared.lock_path.display(), prepared.scope);
        let mut receiver = {
            let mut tasks = self.tasks.lock().map_err(|_| ToolError::Failed("预热协调器不可用".into()))?;
            if self.closed.load(Ordering::Acquire) {
                return Err(ToolError::Failed("MCP 已关闭，后台同步已取消".into()));
            }
            if let Some(task) = tasks.get(&task_key) {
                eprintln!("nova-client: warmup=join_in_process");
                task.receiver.clone()
            } else {
                let (sender, receiver) = watch::channel(None);
                let coordinator = Arc::clone(self);
                let key = task_key.clone();
                // A supervisor converts panic/abort into a recoverable result for every waiter.
                let task = tokio::spawn(async move {
                    let worker = tokio::spawn(run(layer, remote, prepared, settings));
                    let guard = AbortOnDrop(worker.abort_handle());
                    let outcome = match worker.await {
                        Ok(outcome) => outcome,
                        Err(error) => Err(format!("后台同步任务中断，可重试：{error}")),
                    };
                    drop(guard);
                    sender.send_replace(Some(outcome));
                    if let Ok(mut tasks) = coordinator.tasks.lock() {
                        tasks.remove(&key);
                    }
                });
                tasks.insert(task_key, Task { receiver: receiver.clone(), abort: task.abort_handle() });
                receiver
            }
        };
        // A cancelled tool only drops its receiver. It does not cancel the shared worker.
        tokio::time::timeout_at(deadline, async {
            loop {
                if let Some(outcome) = receiver.borrow().clone() {
                    return outcome.map_err(ToolError::Failed);
                }
                receiver.changed().await.map_err(|_| ToolError::Failed(
                    "后台同步任务已取消，可重试".into()
                ))?;
            }
        }).await.map_err(|_| ToolError::Failed("等待后台同步超时，可稍后重试".into()))?
    }

    pub fn start_watcher(
        self: &Arc<Self>, layer: ToolLayer, remote: RemoteClient, root: PathBuf,
        cache_root: PathBuf, settings: Settings,
    ) {
        if std::env::var("NOVA_WATCH").as_deref() != Ok("1") { return; }
        let key = digest(format!("{}\0{}\0{}", remote.base_url(),
            remote.coordination_scope(), root.display()).as_bytes());
        let Ok(mut watchers) = self.watchers.lock() else { return; };
        if self.closed.load(Ordering::Acquire) || watchers.contains_key(&key) { return; }
        if watchers.len() >= 4 {
            eprintln!("nova-client: watcher=root_limit (max=4); lazy sync remains enabled");
            return;
        }
        let lock = cache_root.join("warmup-v1").join(format!("watch-{key}.lock"));
        let coordinator = Arc::clone(self);
        let task = tokio::spawn(async move {
            // Followers only wait on an OS lock; no duplicate scans or uploads.
            let Ok((_leader, _)) = acquire(lock).await else {
                eprintln!("nova-client: watcher=leader_lock_failed; lazy sync remains enabled");
                return;
            };
            let seconds = std::env::var("NOVA_WATCH_INTERVAL_SECS").ok()
                .and_then(|s| s.parse::<u64>().ok()).unwrap_or(30).clamp(5, 3600);
            let interval = Duration::from_secs(seconds);
            let mut delay = interval;
            loop {
                tokio::time::sleep(delay).await;
                if coordinator.closed.load(Ordering::Acquire) { break; }
                let result = coordinator.ensure(layer.clone(), remote.for_call(
                    crate::remote::CallContext::new()), root.to_string_lossy().into_owned(),
                    cache_root.clone(), settings).await;
                if result.is_err() {
                    eprintln!("nova-client: watcher=sync_failed; lazy sync remains enabled");
                    delay = (delay * 2).min(Duration::from_secs(seconds.max(300)));
                } else {
                    delay = interval;
                }
            }
        });
        watchers.insert(key, task.abort_handle());
    }

    pub fn shutdown(&self) {
        self.closed.store(true, Ordering::Release);
        if let Ok(mut watchers) = self.watchers.lock() {
            for (_, watcher) in watchers.drain() { watcher.abort(); }
        }
        if let Ok(mut tasks) = self.tasks.lock() {
            for (_, task) in tasks.drain() {
                task.abort.abort();
            }
        }
    }
}

pub(crate) struct CancellationGuard(pub Arc<AtomicBool>);
impl Drop for CancellationGuard {
    fn drop(&mut self) { self.0.store(true, Ordering::Release); }
}

struct AbortOnDrop(AbortHandle);
impl Drop for AbortOnDrop {
    fn drop(&mut self) { self.0.abort(); }
}

async fn acquire(path: PathBuf) -> Result<(File, bool)> {
    let file = blocking(move || {
        std::fs::create_dir_all(path.parent().context("missing lock parent")?)?;
        OpenOptions::new().read(true).write(true).create(true).truncate(false)
            .open(path).context("cannot open sync lock")
    }).await?;
    let mut waited = false;
    loop {
        match file.try_lock() {
            Ok(()) => return Ok((file, waited)),
            Err(TryLockError::WouldBlock) => {
                waited = true;
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            Err(TryLockError::Error(error)) => return Err(error.into()),
        }
    }
}

fn read_record(path: &Path) -> Option<Record> {
    let bytes = std::fs::read(path).ok()?;
    if bytes.len() > 64 * 1024 { return None; }
    serde_json::from_slice(&bytes).ok()
}

fn write_record(path: &Path, record: &Record) -> Result<()> {
    let temporary = path.with_extension("json.tmp");
    std::fs::write(&temporary, serde_json::to_vec(record)?)?;
    // Readers only consume records under the same OS lock, including on Windows.
    #[cfg(windows)]
    if path.exists() { std::fs::remove_file(path)?; }
    std::fs::rename(&temporary, path)?;
    Ok(())
}

async fn run(layer: ToolLayer, remote: RemoteClient, mut prepared: Prepared, settings: Settings) -> Outcome {
    let requested_ms = now_ms();
    let cancelled = Arc::new(AtomicBool::new(false));
    let _cancel = CancellationGuard(cancelled.clone());
    // Bound lock waiting, scanning, upload and indexing together, not only the final poll.
    tokio::time::timeout(settings.timeout, async {
        let (_lock, waited) = acquire(prepared.lock_path.clone()).await?;
        if prepared.policy.route.is_none() {
            let owner_path = prepared.lock_path.with_extension("owner");
            let root = prepared.root.clone();
            let conflicting = blocking(move || {
                let workspace = root.to_string_lossy().into_owned();
                match std::fs::read_to_string(&owner_path) {
                    Ok(owner) => Ok(owner != workspace),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                        std::fs::write(owner_path, workspace)?;
                        Ok(false)
                    }
                    Err(error) => Err(error.into()),
                }
            }).await?;
            if conflicting {
                let root = prepared.root.clone();
                let cache = prepared.cache_root.clone();
                let mut policy = prepared.policy.clone();
                prepared.policy = blocking(move || {
                    policy.isolate(&root, &cache)?;
                    Ok(policy)
                }).await?;
                eprintln!("nova-client: index_decision=isolated_checkout_conflict historical_primary_preserved=true");
            }
        }
        let root = prepared.root.clone();
        let scope = digest(format!("{}\0{}\0{}", remote.coordination_scope(),
            prepared.root.to_string_lossy(), prepared.policy.fingerprint()).as_bytes());
        let identity = blocking(move || Ok(repo_identity(&root))).await?;
        if identity.identity_key != prepared.identity.identity_key {
            return Err(anyhow!("等待同步期间项目身份/分支发生变化，请重试"));
        }
        let root = prepared.root.clone();
        let files = prepared.policy.files;
        let exclude = prepared.policy.exclude_repositories();
        let signal = cancelled.clone();
        let mut version = blocking(move || workspace_version(&root, files, exclude, Some(&signal))).await?;
        let path = prepared.state_path.clone();
        let cached = blocking(move || Ok(read_record(&path))).await?;
        if let Some(record) = cached {
            if record.schema == 2 && record.scope == scope && record.version == version {
                let fresh = now_ms().saturating_sub(record.finished_ms) <= settings.ready_ttl.as_millis();
                if record.outcome.is_ok() && fresh {
                    eprintln!("nova-client: warmup=reuse_ready cross_process={waited}");
                    return record.outcome.map_err(|error| anyhow!(error));
                }
                if waited && record.finished_ms >= requested_ms && record.outcome.is_err() {
                    return record.outcome.map_err(|error| anyhow!(error));
                }
            }
        }
        // Invalidate a previous success BEFORE mutation: crash/timeout must not expose it as ready.
        let path = prepared.state_path.clone();
        blocking(move || {
            match std::fs::remove_file(path) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error.into()),
            }
        }).await?;
        eprintln!("nova-client: warmup=start cross_process_wait={waited}");
        loop {
            let outcome = layer.sync_project_once(&remote, &prepared.root, &prepared.identity, &prepared.policy, cancelled.clone()).await
                .map_err(|error| match error {
                    ToolError::Failed(message) | ToolError::InvalidArguments(message)
                        | ToolError::UnknownTool(message) => message,
                });
            let root = prepared.root.clone();
            let signal = cancelled.clone();
            let after = blocking(move || workspace_version(&root, files, exclude, Some(&signal))).await?;
            let root = prepared.root.clone();
            let identity = blocking(move || Ok(repo_identity(&root))).await?;
            if identity.identity_key != prepared.identity.identity_key {
                return Err(anyhow!("同步期间项目身份/分支发生变化，请重试"));
            }
            // Never publish a mixed/old version. Changes during sync require an incremental pass.
            if outcome.is_ok() && after != version {
                version = after;
                eprintln!("nova-client: warmup=changed_during_sync incremental_retry=true");
                continue;
            }
            let record = Record {
                schema: 2, scope, version,
                finished_ms: now_ms(), outcome: outcome.clone(),
            };
            let path = prepared.state_path.clone();
            blocking(move || write_record(&path, &record)).await?;
            eprintln!("nova-client: warmup={} ", if outcome.is_ok() { "ready" } else { "failed" });
            return outcome.map_err(|error| anyhow!(error));
        }
    }).await.map_err(|_| "后台同步总超时（含锁等待/扫描/上传/索引），可稍后重试".to_string())?
        .map_err(|error| format!("后台同步失败：{error:#}"))
}

/// Version verification only: no upload, parsing, embedding or checkpoint creation.
/// Content hashes are necessary on coarse-timestamp filesystems (including WSL mounts).
/// Include the eligible path set to detect additions/deletions and ignore-rule changes.
fn workspace_version(root: &Path, policy: FilePolicy, exclude_repositories: bool, cancelled: Option<&AtomicBool>) -> Result<String> {
    if !root.is_dir() { return Err(anyhow!("工作区已不存在：{}", root.display())); }
    let rules = IgnoreRules::load(root);
    let mut files = BTreeMap::new();
    let mut bytes = 0;
    for (visited, entry) in rules.walk_union_pruned(exclude_repositories).enumerate() {
        if cancelled.is_some_and(|flag| flag.load(Ordering::Acquire)) { return Err(anyhow!("版本扫描已取消")); }
        policy.check_limits(visited + 1, files.len(), bytes)?;
        let entry = entry.context("cannot enumerate workspace")?;
        if !entry.file_type().is_some_and(|kind| kind.is_file()) { continue; }
        let path = entry.path();
        if !policy.accepts(&path.to_string_lossy()) { continue; }
        let metadata = std::fs::metadata(path).context("cannot stat workspace file")?;
        let relative = path.strip_prefix(root)?.to_string_lossy().replace('\\', "/");
        let mut stamp = format!("{}:{:?}", metadata.len(), metadata.modified()?);
        #[cfg(unix)] {
            use std::os::unix::fs::MetadataExt;
            stamp.push_str(&format!(":{}:{}:{}:{}", metadata.dev(), metadata.ino(), metadata.ctime(), metadata.ctime_nsec()));
        }
        if metadata.len() <= crate::blobref::max_bytes_for(&relative) as u64 {
            if !files.contains_key(&relative) { bytes += metadata.len(); }
            policy.check_limits(0, files.len() + 1, bytes)?;
            stamp.push_str(&digest(&std::fs::read(path)?));
        }
        files.insert(relative, stamp);
    }
    // Rules can be excluded from the eligible file set; their contents still affect freshness.
    let mut hasher = Sha256::new();
    hasher.update(b"nova-warmup-v1\0");
    hasher.update(format!("{policy:?}:{exclude_repositories}").as_bytes());
    hasher.update(rules.fingerprint().as_bytes());
    hasher.update(serde_json::to_vec(&files)?);
    for name in ["NOVA_INDEX_ALLOWLIST", "NOVA_ALLOWLIST_DEPTH"] {
        hasher.update(name.as_bytes());
        hasher.update(std::env::var(name).unwrap_or_default().as_bytes());
    }
    for directory in root.ancestors() {
        for name in [".gitignore", ".novaignore", ".novainclude"] {
            hasher.update(directory.join(name).to_string_lossy().as_bytes());
            if let Ok(bytes) = std::fs::read(directory.join(name)) { hasher.update(bytes); }
        }
    }
    Ok(hex::encode(hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_detect_same_size_edits_additions_deletions_and_rules() {
        let repo = tempfile::tempdir().unwrap();
        let file = repo.path().join("a.txt");
        std::fs::write(&file, "aaaa").unwrap();
        let old_time = std::fs::metadata(&file).unwrap().modified().unwrap();
        let first = workspace_version(repo.path(), FilePolicy::Full, false, None).unwrap();
        std::fs::write(&file, "bbbb").unwrap();
        File::options().write(true).open(&file).unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(old_time)).unwrap();
        let edited = workspace_version(repo.path(), FilePolicy::Full, false, None).unwrap();
        assert_ne!(first, edited);
        assert_eq!(edited, workspace_version(repo.path(), FilePolicy::Full, false, None).unwrap());
        std::fs::write(repo.path().join("new.txt"), "new").unwrap();
        let added = workspace_version(repo.path(), FilePolicy::Full, false, None).unwrap();
        assert_ne!(added, edited);
        std::fs::remove_file(repo.path().join("new.txt")).unwrap();
        assert_eq!(edited, workspace_version(repo.path(), FilePolicy::Full, false, None).unwrap());
        std::fs::write(repo.path().join(".novaignore"), "a.txt\n").unwrap();
        assert_ne!(edited, workspace_version(repo.path(), FilePolicy::Full, false, None).unwrap());
    }

    #[tokio::test]
    async fn os_lock_is_exclusive_and_drop_releases_it() {
        let cache = tempfile::tempdir().unwrap();
        let path = cache.path().join("sync.lock");
        let (first, _) = acquire(path.clone()).await.unwrap();
        let other = File::options().read(true).write(true).open(&path).unwrap();
        assert!(matches!(other.try_lock(), Err(TryLockError::WouldBlock)));
        drop(first);
        other.try_lock().unwrap();
    }
}
