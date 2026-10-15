//! Workspace/file policy. Identity hashing remains in identity.rs unchanged.
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::remote::CallContext;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum FilePolicy {
    #[default]
    Full,
    Markdown,
}

impl FilePolicy {
    pub fn accepts(self, path: &str) -> bool {
        self == Self::Full || Path::new(path).extension().and_then(|value| value.to_str())
            .is_some_and(|value| value.eq_ignore_ascii_case("md") || value.eq_ignore_ascii_case("markdown"))
    }

    pub(crate) fn check_limits(self, walked: usize, files: usize, bytes: u64) -> Result<()> {
        if self != Self::Markdown {
            return Ok(());
        }
        let limits = ScanLimits::get();
        if walked > limits.visited || files > limits.files || bytes > limits.bytes {
            bail!(
                "非 Git Markdown 扫描超出安全上限（遍历 {} 项 / {} 文件 / {} MiB）：请指定更小的 project_root，\
                 或用 NOVA_MARKDOWN_MAX_VISITED / NOVA_MARKDOWN_MAX_FILES / NOVA_MARKDOWN_MAX_MB 调高",
                limits.visited, limits.files, limits.bytes / (1024 * 1024)
            );
        }
        Ok(())
    }
}

/// 非 Git Markdown 扫描的安全上限。
///
/// 超限**整体报错**而不是截断：遍历顺序不保证稳定，按"前 N 个"截断会让每次同步选中的文件集合
/// 不同，进而对服务端产生反复的新增/删除。因此默认值取宽（足以覆盖常见文档库），并允许用环境变量调整。
pub(crate) struct ScanLimits {
    pub visited: usize,
    pub files: usize,
    pub bytes: u64,
}

impl ScanLimits {
    pub(crate) fn get() -> &'static Self {
        static LIMITS: std::sync::OnceLock<ScanLimits> = std::sync::OnceLock::new();
        LIMITS.get_or_init(|| Self {
            visited: env_limit("NOVA_MARKDOWN_MAX_VISITED", 50_000),
            files: env_limit("NOVA_MARKDOWN_MAX_FILES", 2_000),
            bytes: env_limit("NOVA_MARKDOWN_MAX_MB", 64) as u64 * 1024 * 1024,
        })
    }
}

/// 读取正整数环境变量；缺失/非法/为 0 → 默认值。
pub(crate) fn env_limit(name: &str, default: usize) -> usize {
    std::env::var(name).ok().and_then(|value| value.trim().parse::<usize>().ok())
        .filter(|value| *value > 0).unwrap_or(default)
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct RouteScope {
    pub device_key: String,
    pub workspace_key: String,
}

#[derive(Clone, Debug)]
pub(crate) struct WorkspacePolicy {
    pub kind: &'static str,
    pub files: FilePolicy,
    pub route: Option<RouteScope>,
}

/// Bounded local Git metadata commands. git init (unborn HEAD) is still a Git repository.
pub(crate) fn git(root: &Path, args: &[&str]) -> Result<Option<String>> {
    let mut child = match Command::new("git").arg("-C").arg(root).args(args)
        .env("GIT_TERMINAL_PROMPT", "0").stdout(Stdio::piped()).stderr(Stdio::null()).spawn() {
        Ok(child) => child,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let started = Instant::now();
    loop {
        if child.try_wait()?.is_some() { break; }
        if started.elapsed() > Duration::from_secs(2) {
            let _ = child.kill();
            let _ = child.wait();
            bail!("本地 Git 元数据检查超时");
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    let output = child.wait_with_output()?;
    if !output.status.success() { return Ok(None); }
    Ok(Some(String::from_utf8(output.stdout)?.trim().to_string()))
}

fn hash(bytes: &[u8]) -> String { hex::encode(Sha256::digest(bytes)) }

fn device_key(cache: &Path) -> Result<String> {
    if let Ok(explicit) = std::env::var("NOVA_DEVICE_ID") {
        if explicit.trim().is_empty() || explicit.len() > 256 { bail!("NOVA_DEVICE_ID 必须为 1～256 字符"); }
        return Ok(hash(explicit.as_bytes()));
    }
    // Stable OS identity lets processes using different cache roots still address the same device.
    #[cfg(target_os = "linux")]
    if let Ok(bytes) = std::fs::read("/etc/machine-id") {
        if !bytes.is_empty() { return Ok(hash(&bytes)); }
    }
    std::fs::create_dir_all(cache)?;
    let lock = OpenOptions::new().create(true).truncate(false).read(true).write(true)
        .open(cache.join("device.lock"))?;
    lock.lock()?;
    let path = cache.join("device-id");
    let id = if path.is_file() {
        let mut value = String::new();
        File::open(&path)?.take(256).read_to_string(&mut value)?;
        if value.is_empty() { bail!("设备标识文件损坏：{}", path.display()); }
        value
    } else {
        let value = CallContext::new().id().to_string();
        let mut file = File::create(&path)?;
        file.write_all(value.as_bytes())?;
        file.sync_all()?;
        value
    };
    Ok(hash(id.as_bytes()))
}

impl WorkspacePolicy {
    pub fn detect(root: &Path, cache: &Path, has_remote: bool) -> Result<Self> {
        let git_root = git(root, &["rev-parse", "--show-toplevel"])?;
        let kind = if git_root.is_some() {
            let git_dir = git(root, &["rev-parse", "--path-format=absolute", "--git-dir"])?
                .context("Git 元数据缺少 git-dir")?;
            let common_dir = git(root, &["rev-parse", "--path-format=absolute", "--git-common-dir"])?
                .context("Git 元数据缺少 git-common-dir")?;
            let git_dir = std::fs::canonicalize(git_dir).context("Git git-dir 无效")?;
            let common_dir = std::fs::canonicalize(common_dir).context("Git common-dir 无效")?;
            if git_dir != common_dir {
                "linked_worktree"
            } else if git(root, &["rev-parse", "--show-superproject-working-tree"])?
                .is_some_and(|value| !value.is_empty()) {
                "submodule"
            } else { "git" }
        } else {
            // Do not reinterpret a corrupt/inaccessible Git workspace as a documentation directory.
            if root.ancestors().any(|directory| directory.join(".git").symlink_metadata().is_ok()) {
                bail!("工作区存在 Git 元数据，但 Git 无法识别；请修复后重试");
            }
            "directory"
        };
        let files = if kind == "directory" {
            match std::env::var("NOVA_NON_GIT_INDEX").unwrap_or_else(|_| "markdown".into()).as_str() {
                "markdown" => FilePolicy::Markdown,
                "full" => FilePolicy::Full,
                _ => bail!("NOVA_NON_GIT_INDEX 只接受 markdown 或 full"),
            }
        } else { FilePolicy::Full };
        if kind == "directory" && files == FilePolicy::Markdown {
            let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))
                .and_then(|path| std::fs::canonicalize(path).ok());
            if root.parent().is_none() || home.as_deref() == Some(root) {
                bail!("非 Git Markdown 模式拒绝扫描用户主目录/文件系统根；请显式选择项目子目录");
            }
        }
        // Keep historical Git/Worktree branch projects. Document roots get explicit routing;
        // a conflicting writable checkout can opt into routing later, under the project lock.
        let isolate = kind == "directory"
            || std::env::var("NOVA_WORKSPACE_ISOLATION").is_ok_and(|value| value == "1");
        let _ = has_remote;
        let route = if isolate {
            Some(RouteScope {
                device_key: device_key(cache)?,
                workspace_key: hash(root.to_string_lossy().as_bytes()),
            })
        } else { None };
        eprintln!("nova-client: workspace_type={kind} index_decision={} workspace_isolated={isolate}",
            if files == FilePolicy::Markdown { "markdown_only" } else { "incremental_full" });
        Ok(Self { kind, files, route })
    }

    pub fn isolate(&mut self, root: &Path, cache: &Path) -> Result<()> {
        self.route = Some(RouteScope {
            device_key: device_key(cache)?,
            workspace_key: hash(root.to_string_lossy().as_bytes()),
        });
        Ok(())
    }

    pub fn exclude_repositories(&self) -> bool { self.kind == "directory" }

    pub fn fingerprint(&self) -> String {
        format!("{}:{:?}:{}", self.kind, self.files,
            self.route.as_ref().map(|scope| format!("{}:{}", scope.device_key, scope.workspace_key)).unwrap_or_default())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn command(root: &Path, args: &[&str]) {
        assert!(Command::new("git").arg("-C").arg(root).args(args).status().unwrap().success());
    }

    #[test]
    fn no_remote_git_init_is_full_not_markdown() {
        let root = tempfile::tempdir().unwrap();
        let cache = tempfile::tempdir().unwrap();
        command(root.path(), &["init", "-q"]);
        let policy = WorkspacePolicy::detect(root.path(), cache.path(), false).unwrap();
        assert_eq!(policy.kind, "git");
        assert_eq!(policy.files, FilePolicy::Full);
        assert!(policy.route.is_none(), "Git path identity must stay compatible");
    }

    #[test]
    fn plain_directory_is_strict_markdown_and_bounded() {
        let root = tempfile::tempdir().unwrap();
        let cache = tempfile::tempdir().unwrap();
        let policy = WorkspacePolicy::detect(root.path(), cache.path(), false).unwrap();
        assert_eq!(policy.files, FilePolicy::Markdown);
        for path in ["x.md", "notes.MARKDOWN", "AGENTS.md"] { assert!(policy.files.accepts(path)); }
        for path in ["x.py", "x.mdx", "secret.txt", "token.env", "README"] { assert!(!policy.files.accepts(path)); }
        assert!(policy.files.check_limits(10, 500, 1).is_ok(), "旧上限 500 文件现在放行");
        assert!(policy.files.check_limits(10, 2_001, 1).is_err());
        assert!(policy.files.check_limits(50_001, 1, 1).is_err());
        assert!(policy.files.check_limits(10, 1, 65 * 1024 * 1024).is_err());
        let error = policy.files.check_limits(10, 2_001, 1).unwrap_err().to_string();
        assert!(error.contains("NOVA_MARKDOWN_MAX_FILES"), "报错要说明如何调整：{error}");
        assert!(FilePolicy::Full.check_limits(usize::MAX, usize::MAX, u64::MAX).is_ok());
    }

    #[test]
    fn linked_worktree_is_independent_full_index() {
        let root = tempfile::tempdir().unwrap();
        let worktree_parent = tempfile::tempdir().unwrap();
        let cache = tempfile::tempdir().unwrap();
        command(root.path(), &["init", "-q"]);
        command(root.path(), &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "initial"]);
        let linked = worktree_parent.path().join("linked");
        command(root.path(), &["worktree", "add", "-q", "-b", "task", linked.to_str().unwrap()]);
        let policy = WorkspacePolicy::detect(&linked, cache.path(), true).unwrap();
        assert_eq!(policy.kind, "linked_worktree");
        assert_eq!(policy.files, FilePolicy::Full);
        assert!(policy.route.is_none(), "normal worktree must reuse its historical branch project");
    }

    #[test]
    fn broken_git_metadata_is_not_a_document_directory() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join(".git"), "gitdir: /nonexistent/nova-test\n").unwrap();
        assert!(WorkspacePolicy::detect(root.path(), root.path(), false).is_err());
    }
}
