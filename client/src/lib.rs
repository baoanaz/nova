//! nova-client：MCP stdio 客户端 + 本地同步代理（Module 05；MCP 最终形态，D-39 / R38）。
//!
//! ```text
//! 编辑器（Claude Code / Codex / Cursor）
//!    │ stdio JSON-RPC 2.0
//!    ▼
//! nova-client ── 本地：扫描 / 三层忽略 / blob_hash / 增量对账 / 缓存 / checkpoint
//!    │ HTTPS + Bearer
//!    ▼
//! nova-service（VPS）── 检索 / 组装 / 渲染
//! ```
//!
//! 模块划分（与 `docs/design/Background/01-notace-tool-rs.md` 的参考实现对应）：
//!
//! | 模块 | 职责 |
//! |---|---|
//! | [`identity`] | D-29 身份（与 core 逐字节一致）——云端定位项目的唯一依据 |
//! | [`blobref`] | CF-02 `blob_hash`（含 `0x00` 分隔符）+ 二进制判定 + 控制字符清洗 |
//! | [`ignore`] | D-28 三层忽略（`ignore` crate + 内置目录模式） |
//! | [`index`] | 本地缓存（`~/.cache/nova/<projectId>/`）+ 已验证缓存命中 + 对账 |
//! | [`remote`] | CF-05 客户端：resolve / batch-upload / deletions / checkpoint / query |
//! | [`tools`] | CF-06 两个工具 + 懒同步编排 |
//! | [`protocol`] | MCP stdio（JSON-RPC 2.0；stdout 纯净） |
//!
//! **与参考实现的关键差异**见 [`remote`] 的模块文档（nova 的 CF-05/CF-02 与 notace 的
//! 线上协议不同，不能照搬）。

pub mod blobref;
pub mod identity;
pub mod ignore;
pub mod index;
pub mod protocol;
pub mod remote;
pub mod tools;

use anyhow::Result;

use crate::protocol::Server;
use crate::remote::RemoteClient;
use crate::tools::ToolLayer;

/// 缓存根：`$NOVA_CLIENT_CACHE` 优先，否则 `~/.cache/nova`。
///
/// **为什么不放工作区**（对参考实现的有意改进，`Background/01` §7-1）：不污染仓库、不与
/// `.gitignore` 纠缠；且按 `projectId` 分目录，同仓库不同 checkout 天然隔离。
pub fn default_cache_root() -> std::path::PathBuf {
    if let Some(explicit) = std::env::var_os("NOVA_CLIENT_CACHE") {
        return std::path::PathBuf::from(explicit);
    }
    if let Some(cache_home) = std::env::var_os("XDG_CACHE_HOME") {
        return std::path::PathBuf::from(cache_home).join("nova");
    }
    std::env::var_os("HOME")
        .map(|home| std::path::PathBuf::from(home).join(".cache").join("nova"))
        .unwrap_or_else(|| std::path::PathBuf::from(".nova-cache"))
}

/// 装配并跑 stdio 服务（`main.rs` 的唯一出口）。
pub async fn run(
    base_url: &str,
    token: Option<String>,
    cache_root: std::path::PathBuf,
) -> Result<()> {
    let remote = RemoteClient::new(base_url, token)?;
    let layer = ToolLayer::new(remote, cache_root);
    protocol::run_stdio(Server::new(layer)).await
}
