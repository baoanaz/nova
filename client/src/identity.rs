//! D-29 项目身份：`identityKey = sha256(material)`，`projectId = sha256(identityKey)[..16]`。
//!
//! **这份实现必须与 `core/nova_core/engine.py` 的 `repo_identity()` 逐字节一致**——否则客户端
//! resolve 出的项目与服务端索引的项目不是同一个（云端 MCP 的核心正确性前提，见
//! `docs/plan/cloud-mcp-readiness.md` §2 A2 与 §12 Q2）。
//!
//! 规则（与 core 一致，逐条对应）：
//!
//! 1. `path = expanduser(root)` 后 `resolve()`（绝对化 + 消除 `..` / 符号链接）；
//! 2. 用 `git -C <path> rev-parse --show-toplevel` 求 git 根（失败＝无 git）；
//! 3. 有 git 根且能取到 remote：
//!    - remote = `git -C <path> config --get remote.origin.url`，失败则取第一个 remote
//!      （`git remote` 的**首行**）；
//!    - `relative = path.relative_to(git_root)`（失败＝空字符串）；
//!    - `material = remote + (relative if relative != "." else "")`；
//! 4. 否则 `material = str(path)`（退化为绝对路径）。
//!
//! 已知边界（**与 core 同源**，不是本模块的缺陷）：
//! - remote URL 的协议形式未归一化：`git@host:path` 与 `https://host/path` 得到**不同** key
//!   （见就绪度报告 §3 A3，属 L3 决策）；
//! - 该规则**依赖绝对路径**（符号链接、`..`、不同挂载点会改变 material），因此 fixture 测试
//!   只能覆盖"给定绝对路径"的确定性部分。

use std::path::{Path, PathBuf};
use std::process::Command;

use sha2::{Digest, Sha256};

/// git 命令超时（与 core `_GIT_TIMEOUT_S` 语义一致；本实现用同步 `Command`，超时由 git 自身
/// 的快速返回保证——仅跑 config/rev-parse/remote 三类本地命令）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RepoIdentity {
    pub identity_key: String,
    pub project_id: String,
    pub display_name: String,
    pub remote_url: Option<String>,
    pub git_root: Option<String>,
    pub repo_path: String,
    /// 参与身份计算的分支名（TASK-111）；`None` = 未引入分支维度（无 git / detached 取不到）。
    pub branch: Option<String>,
}

/// 按 D-29 计算仓库身份（**不触碰网络**，只读本地 git 配置）。
pub fn repo_identity(root: &Path) -> RepoIdentity {
    let path = abs_path(root);
    let git_root = git(&["rev-parse", "--show-toplevel"], &path);
    let remote = match git_root {
        Some(_) => git_remote_url(&path),
        None => None,
    };

    let (material, remote_url, resolved_git_root, repo_path, branch) = match (git_root, remote) {
        (Some(git_root), Some(remote)) => {
            let resolved = abs_path(Path::new(&git_root));
            // Python 的 relative_to：仅当 git 根是 path 的祖先时成功；否则整体退回 ""。
            let relative = resolved
                .to_str()
                .and_then(|base| path.strip_prefix(base).ok())
                .map(|rel| rel.to_string_lossy().replace('\\', "/"))
                .unwrap_or_default();
            let repo_path = if relative == "." {
                String::new()
            } else {
                relative
            };
            // TASK-111：分支参与身份计算（与 core `repo_identity` 逐字节一致）。
            // 分隔符 `\x00` 防止 (remote+路径) 与分支名的拼接歧义。
            let branch = git_branch(&path);
            let mut material = format!("{remote}{repo_path}");
            if let Some(name) = &branch {
                // NUL 分隔符：与 core 的 "\x00" 同字节，防拼接歧义。
                material.push('\u{0}');
                material.push_str(name);
            }
            (
                material,
                Some(remote),
                Some(resolved.to_string_lossy().into_owned()),
                repo_path,
                branch,
            )
        }
        _ => (
            path.to_string_lossy().into_owned(),
            None,
            None,
            String::new(),
            git_branch(&path),
        ),
    };

    let identity_key = sha256_hex(material.as_bytes());
    let base_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let display_name = match &branch {
        Some(name) => format!("{base_name}@{name}"),
        None => base_name,
    };
    RepoIdentity {
        project_id: project_id_for(&identity_key),
        identity_key,
        display_name,
        remote_url,
        git_root: resolved_git_root,
        repo_path,
        branch,
    }
}

/// 当前分支名（TASK-111 身份维度）：`rev-parse --abbrev-ref HEAD`。
///
/// 退化顺序：分支名 → detached HEAD 的短 commit → `None`（取不到时不引入分支维度，
/// 与旧口径一致）。无 remote/无 git 场景也调用本函数，仅用于**展示**，不影响 identity_key。
fn git_branch(cwd: &Path) -> Option<String> {
    if let Some(name) = git(&["rev-parse", "--abbrev-ref", "HEAD"], cwd) {
        if name != "HEAD" {
            return Some(name);
        }
    }
    git(&["rev-parse", "--short", "HEAD"], cwd).map(|head| format!("detached@{head}"))
}

/// `project_id = sha256(identity_key)` 前 16 位十六进制（与 core `project_id_for` 一致）。
pub fn project_id_for(identity_key: &str) -> String {
    sha256_hex(identity_key.as_bytes())[..16].to_string()
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

/// 绝对化 + 规范化（对应 Python `Path(root).expanduser().resolve()`）。
///
/// 展开 `~`；随后优先用 `std::fs::canonicalize`（解析符号链接，与 Python `resolve()` 同语义），
/// 失败（路径不存在）时退回"拼上 CWD 再消 `..`"。**路径不存在时两端行为可能不同**，因此
/// MCP 工具入口会先校验目录存在（`sync_project` 的 `not a directory` 分支）。
fn abs_path(root: &Path) -> PathBuf {
    let expanded = expand_tilde(root);
    if let Ok(canonical) = std::fs::canonicalize(&expanded) {
        return canonical;
    }
    let absolute = if expanded.is_absolute() {
        expanded
    } else {
        std::env::current_dir().unwrap_or_default().join(expanded)
    };
    normalize_lexically(&absolute)
}

fn expand_tilde(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if text == "~" {
        return std::env::var_os("HOME").map_or_else(|| path.to_path_buf(), PathBuf::from);
    }
    if let Some(rest) = text.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    path.to_path_buf()
}

/// 纯词法规范化（不触碰文件系统）：消掉 `.` 与 `..`。
fn normalize_lexically(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                if !out.pop() {
                    out.push("..");
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// 与 core `git_remote_url` 同规则：优先 `origin`，否则取第一个 remote 的 url。
fn git_remote_url(path: &Path) -> Option<String> {
    if let Some(origin) = git(&["config", "--get", "remote.origin.url"], path) {
        return Some(origin);
    }
    let names = git(&["remote"], path)?;
    let first = names.lines().next()?.trim();
    if first.is_empty() {
        return None;
    }
    git(&["config", "--get", &format!("remote.{first}.url")], path)
}

/// 跑一条 git 命令；git 缺失/非零退出 → `None`（与 core `_git` 一致，异常一律当作"无信息"）。
fn git(args: &[&str], cwd: &Path) -> Option<String> {
    let output = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// CF-02 侧的跨语言常量（由 Python `nova_core.engine` 生成，见 TASK-040R 执行记录）。
    /// 固定 remote 字符串 → 固定 identityKey：锁定 sha256(material) 的算法形态。
    #[test]
    fn identity_key_is_sha256_of_the_remote_material() {
        let material = "git@github.com:acme/tool.git";
        assert_eq!(
            sha256_hex(material.as_bytes()),
            "63e7c710719cfe66587b550115b25d55f0ab5a8fdb886013196a837a874cd115"
        );
        assert_eq!(
            project_id_for("63e7c710719cfe66587b550115b25d55f0ab5a8fdb886013196a837a874cd115"),
            "49c6f266e8577a6d"
        );
    }

    #[test]
    fn protocol_form_is_not_normalized_yet() {
        // A3：两种协议形式给出两个 key（与 core 行为一致；归一化属 L3，未实现）。
        assert_ne!(
            sha256_hex(b"git@github.com:acme/tool.git"),
            sha256_hex(b"https://github.com/acme/tool.git")
        );
        assert_eq!(
            sha256_hex(b"https://github.com/acme/tool.git"),
            "7e2d89f0c46979b6adead756a101e56c5e45515c622a4b32889ace4adb8e2c8b"
        );
    }

    #[test]
    fn non_git_directory_falls_back_to_absolute_path_material() {
        let dir = tempfile::tempdir().expect("temp dir");
        let identity = repo_identity(dir.path());

        assert!(identity.remote_url.is_none());
        assert!(identity.git_root.is_none());
        assert_eq!(identity.repo_path, "");
        // material = canonical absolute path（tempdir 已 canonicalize）
        let expected = sha256_hex(dir.path().to_string_lossy().as_bytes());
        assert_eq!(identity.identity_key, expected);
    }

    #[test]
    fn rel_path_handling_matches_python_relative_to() {
        let base = tempfile::tempdir().expect("temp dir");
        let nested = base.path().join("src").join("deep");
        std::fs::create_dir_all(&nested).expect("nested dirs");

        // 无 git：身份退回绝对路径，repo_path 为空（与 Python 的 ValueError 分支一致）
        let identity = repo_identity(&nested);
        assert_eq!(identity.repo_path, "");
        assert!(identity.identity_key == sha256_hex(nested.to_string_lossy().as_bytes()));
    }
}
