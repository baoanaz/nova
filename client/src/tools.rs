//! 工具层：两个 CF-06 工具（`search_context` / `ask_project`）+ 会话编排
//! （身份 → resolve → 扫描对账 → 上传 → checkpoint → 检索）。
//!
//! **参数名与 CF-06 一致**：编辑器侧仍传 `project_root`（本地绝对路径）。云端语义下
//! `project_root` 不用于服务端定位，而是**客户端**用来算 D-29 身份（就绪度报告 §2 方案乙：
//! 零契约变更）。
//!
//! `max_tokens`（TASK-MCP-BUDGET，2026-09-16）：**不再出现在 `inputSchema` 里**——
//! 包大小由服务端按证据密度决定，不由调用方猜；结构体仍保留该字段以便接收旧版编辑器
//! 发来的值（不报错），但不再向 AI 声明。
//!
//! 会话状态（进程内 `project_root → SessionState`，Module 05 §3.6）：缓存最后一次的
//! `projectId` / `scope` / `checkpointId`，避免每次 tool call 都重建 checkpoint。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use crate::identity::repo_identity;
use crate::index::{require_non_empty, IndexManager};
use crate::remote::{validate_project_root, CallContext, RemoteClient};

/// `max_tokens` 的可接受上限（服务端 `MAX_MAX_TOKENS`，运行时兜底）。
///
/// TASK-MCP-BUDGET：该参数已从 `inputSchema` 移除（不再向 AI 声明），但**仍可接收**——
/// 故校验与默认值保留，保证旧编辑器发来的值不会变成参数错误。
pub const MAX_TOKENS_ASK: i64 = 20_000;
/// 未收到 `max_tokens` 时发给服务端的值。
///
/// 实际很可能被服务端忽略（service 以自己的默认档位为准，Fast 14K / Deep 16K）；
/// 这里保留 10000 只为与旧版服务端（仍读该字段）兼容，不参与本地的预算决策。
pub const DEFAULT_MAX_TOKENS: i64 = 10_000;
/// 查询长度上限（与 service 侧 `MAX_QUERY_CHARS` 同口径）。
///
/// TASK-MCP-BUDGET：2000→8000（服务端已放宽到 8000）。此前客户端 8000 / 服务端 2000，
/// 2000～8000 字符的查询会在服务端被 400 拒，而调用方自认为合法。
const MAX_QUERY_CHARS: usize = 8_000;

/// 工具层错误（→ 协议层的三分类，Module 05 §2.2）。
#[derive(Debug)]
pub enum ToolError {
    /// 参数错误 → JSON-RPC `-32602`。
    InvalidArguments(String),
    /// 未知工具 → JSON-RPC `-32602`。
    UnknownTool(String),
    /// 工具执行错误 → 正常响应 + `isError: true`（agent 可读原因并决策重试）。
    Failed(String),
}

impl ToolError {
    fn invalid(message: impl Into<String>) -> Self {
        Self::InvalidArguments(message.into())
    }
}

/// CF-06 冻结合同（**description 的单一来源**，权威文件在 `docs/contracts/mcp-tools.json`）。
///
/// 这里读的是 crate **内**的构建期副本 `client/contract/mcp-tools.json`：
/// `include_str!` 是**编译期**展开，而 `aarch64-unknown-linux-musl` 走 `cross`（Docker），
/// **只把 crate 根目录挂载进容器**——实测容器内 `/project/..` 是容器的 `/`（`bin/boot/dev/...`），
/// 仓库上层的 `docs/` 完全不可见，直接写 `../../docs/...` 会让该平台构建失败
/// （2026-09-16 实测事故：Release 资产不全 → `npm publish` 后用户拿到 404）。
///
/// 副本与权威文件的一致性由两边测试守住：
/// - client：`contract_copy_matches_repo_copy`（本文件测试模块）逐字节比对；
/// - 仓库层：`scripts/check-version.sh` 之外，CI 与本地均可跑同一比对。
/// 改描述时**先改 `docs/contracts/mcp-tools.json`，再同步到此副本**（否则测试红）。
const CF06_CONTRACT: &str = include_str!("../contract/mcp-tools.json");

/// 从 CF-06 契约取指定工具的 `description`。
///
/// 契约损坏时不应静默回退成空描述（那会让 Agent 完全不会用工具）——
/// 这里用 `expect` 在启动阶段大声失败，而不是把空串发给编辑器。
fn cf06_description(tool: &str) -> String {
    let payload: Value = serde_json::from_str(CF06_CONTRACT)
        .expect("CF-06 契约不是合法 JSON（docs/contracts/mcp-tools.json）");
    payload["tools"]
        .as_array()
        .expect("CF-06 契约缺少 tools 数组")
        .iter()
        .find(|item| item["name"] == tool)
        .and_then(|item| item["description"].as_str())
        .unwrap_or_else(|| panic!("CF-06 契约里没有工具 {tool} 的 description"))
        .to_string()
}

/// 两个工具的 JSON Schema（CF-06；`additionalProperties: false`）。
///
/// `description` 取自 CF-06 契约（与 service 同一份）；`inputSchema` 在这里**手工写**，
/// 因为运行时字段比契约多一层约束（`minLength`）且 `max_tokens` 已不再声明
/// （TASK-MCP-BUDGET：包大小是服务端按证据密度决定的内部量，让 AI 猜只会猜小——
/// 实测把长函数截断成“签名 + 前 15 行”）。
pub fn definitions() -> Value {
    json!([
        {
            "name": "search_context",
            "description": cf06_description("search_context"),
            "inputSchema": {
                "type": "object",
                "additionalProperties": false,
                "required": ["query", "project_root"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "description": "自然语言或符号混合查询，中英均可"},
                    "project_root": {"type": "string", "minLength": 1, "description": "项目根绝对路径，正斜杠"}
                }
            }
        },
        {
            "name": "ask_project",
            "description": cf06_description("ask_project"),
            "inputSchema": {
                "type": "object",
                "additionalProperties": false,
                "required": ["question", "project_root"],
                "properties": {
                    "question": {"type": "string", "minLength": 1, "description": "需要项目级回答的问题，中英均可"},
                    "project_root": {"type": "string", "minLength": 1, "description": "项目根绝对路径，正斜杠"}
                }
            }
        }
    ])
}

#[derive(Debug, Default, Clone)]
struct SessionState {
    checkpoint_id: Option<String>,
    scope: Option<Arc<Vec<String>>>,
}

/// 工具层（一个进程一个实例；服务端地址 + 可选 token）。
pub struct ToolLayer {
    remote: RemoteClient,
    cache_root: PathBuf,
    sessions: Mutex<HashMap<String, SessionState>>,
}

impl ToolLayer {
    pub fn new(remote: RemoteClient, cache_root: PathBuf) -> Self {
        Self {
            remote,
            cache_root,
            sessions: Mutex::new(HashMap::new()),
        }
    }

    /// 执行一个工具调用。
    ///
    /// **一次调用一个 callId**（TASK-099 §B-2）：进入这里先开一个 :class:`CallContext`，
    /// 再派生一个带它的 ``RemoteClient``——该次调用的所有 HTTP 请求（resolve / 上传 /
    /// 删除通知 / checkpoint / 检索）因此共享同一个 ``X-Request-Id``。服务端靠这个值把
    /// "N 次初始化 + 1 次检索"归成一次调用（卡内 §B-1 的问题）。
    ///
    /// 并发安全：callId 是**每次调用局部**的值，不存进 ``self``，因此两个 tool call 并发
    /// 执行时不会把彼此的 id 串起来（会话缓存 ``self.sessions`` 是跨调用状态，与它无关）。
    pub async fn execute(&self, tool_name: &str, arguments: Value) -> Result<String, ToolError> {
        let remote = self.remote.for_call(CallContext::new());
        // 诊断走 stderr（stdout 只出 JSON-RPC 帧）：用户/日志据此把客户端侧与
        // 服务端 ``GET /api/calls/{callId}`` 对上（TASK-099 §G 要抓的就是它）。
        eprintln!("zace-client: callId={} tool={tool_name}", remote.call_id());
        match tool_name {
            "search_context" => {
                let args: SearchArguments = decode(tool_name, arguments)?;
                require_query(&args.query, "query")?;
                require_max_tokens(args.max_tokens, MAX_TOKENS_ASK, "max_tokens")?;
                self.search(
                    &remote,
                    &args.project_root,
                    args.query.trim(),
                    args.max_tokens.unwrap_or(DEFAULT_MAX_TOKENS),
                )
                .await
            }
            "ask_project" => {
                let args: AskArguments = decode(tool_name, arguments)?;
                require_query(&args.question, "question")?;
                require_max_tokens(args.max_tokens, MAX_TOKENS_ASK, "max_tokens")?;
                self.ask(
                    &remote,
                    &args.project_root,
                    args.question.trim(),
                    args.max_tokens.unwrap_or(DEFAULT_MAX_TOKENS),
                )
                .await
            }
            other => Err(ToolError::UnknownTool(other.to_string())),
        }
    }

    async fn search(
        &self,
        remote: &RemoteClient,
        project_root: &str,
        query: &str,
        max_tokens: i64,
    ) -> Result<String, ToolError> {
        let (project_id, checkpoint_id) = self.sync_project(remote, project_root).await?;
        remote
            .search(&project_id, query, max_tokens, checkpoint_id.as_deref())
            .await
            .map_err(|error| ToolError::Failed(format!("检索失败：{error:#}")))
    }

    async fn ask(
        &self,
        remote: &RemoteClient,
        project_root: &str,
        question: &str,
        _max_tokens: i64,
    ) -> Result<String, ToolError> {
        let (project_id, checkpoint_id) = self.sync_project(remote, project_root).await?;
        remote
            .ask(&project_id, question, checkpoint_id.as_deref())
            .await
            .map_err(|error| ToolError::Failed(format!("提问失败：{error:#}")))
    }

    /// 懒同步编排（D-27）：每次 tool call 保证工作区新鲜，返回 `(projectId, checkpointId)`。
    ///
    /// 时序（Module 05 §3.3 + 就绪度报告 §5）：
    /// ① 算身份 → ② resolve → ③ 扫描对账 → ④ 上传变更 → ⑤ 通知删除 → ⑥ 建 checkpoint。
    ///
    /// ``remote`` 是**本次调用**的客户端（带该次调用的 callId，TASK-099 §B-2）：
    /// 上面的六步全走它，因此服务端那 N 条 ``index_runs`` 与随后的 ``query_audit`` 共享同一 id。
    async fn sync_project(
        &self,
        remote: &RemoteClient,
        project_root: &str,
    ) -> Result<(String, Option<String>), ToolError> {
        let root = validate_project_root(project_root)
            .map_err(|error| ToolError::invalid(error.to_string()))?;
        let identity = repo_identity(&root);

        let project_id = remote
            .resolve_project(&identity.identity_key, &identity.display_name)
            .await
            .map_err(|error| {
                ToolError::Failed(format!(
                    "无法在服务端定位项目（identityKey={}）：{error:#}",
                    identity.identity_key
                ))
            })?;

        // TASK-100：把服务端端点传给缓存（缓存目录按端点分片，且字段自证）。
        let manager = IndexManager::new(
            root.clone(),
            project_id.clone(),
            self.cache_root.clone(),
            remote.base_url(),
        );
        let mut scan = manager
            .scan()
            .map_err(|error| ToolError::Failed(format!("本地扫描失败：{error:#}")))?;
        require_non_empty(&scan.index).map_err(|error| ToolError::Failed(error.to_string()))?;

        let mut deferred_confirmed = false;
        // 上传变更（有变更才发请求）。
        if !scan.to_upload.is_empty() {
            let payload = crate::index::upload_payload(&scan.to_upload);
            let outcome = remote
                .upload_files(&project_id, &payload)
                .await
                .map_err(|error| ToolError::Failed(format!("上传失败：{error:#}")))?;
            deferred_confirmed |= outcome.indexing_deferred;
            let rejected = IndexManager::apply_upload_outcome(
                &mut scan.index,
                &scan.to_upload,
                &outcome.accepted,
                &outcome.skipped_paths,
            );
            if !rejected.is_empty() {
                eprintln!(
                    "zace-client: 服务端跳过 {} 个文件（示例：{:?}）",
                    rejected.len(),
                    rejected
                        .iter()
                        .take(3)
                        .map(|item| &item.path)
                        .collect::<Vec<_>>()
                );
            }
        }

        // 通知删除（幂等）。
        if !scan.deleted.is_empty() {
            deferred_confirmed |= remote
                .notify_deletions(&project_id, &scan.deleted)
                .await
                .map_err(|error| ToolError::Failed(format!("删除通知失败：{error:#}")))?;
        }

        // 提交缓存（只有成功上传的文件才在里面）+ 更新 scope/checkpoint。
        manager
            .commit(&scan.index)
            .map_err(|error| ToolError::Failed(format!("缓存写入失败：{error:#}")))?;

        // 每次调用都 flush，包括上传已缓存、但上次索引失败/超时的恢复路径。
        let skipped = remote.wait_for_indexing(&project_id, deferred_confirmed).await
            .map_err(|error| ToolError::Failed(format!("索引尚不可用：{error:#}")))?;
        if !skipped.is_empty() {
            for path in skipped {
                scan.index.entries.remove(&path);
            }
            manager.commit(&scan.index)
                .map_err(|error| ToolError::Failed(format!("缓存写入失败：{error:#}")))?;
        }

        let scope: Arc<Vec<String>> = Arc::new(scan.index.all_blob_hashes());
        let previous = self.session(&project_id);
        let checkpoint_id = match previous.checkpoint_id.clone() {
            Some(id) if previous.scope.as_ref() == Some(&scope) => Some(id),
            _ => match remote.create_checkpoint(&project_id, &scope).await {
                Ok(id) => Some(id),
                Err(error) => {
                    // checkpoint 是传输优化，失败不阻断检索（降级为服务端不用 checkpoint）。
                    eprintln!("zace-client: checkpoint 创建失败（不影响检索）：{error:#}");
                    None
                }
            },
        };
        self.store_session(
            &project_id,
            SessionState {
                checkpoint_id: checkpoint_id.clone(),
                scope: Some(scope),
            },
        );
        Ok((project_id, checkpoint_id))
    }

    fn session(&self, project_id: &str) -> SessionState {
        self.sessions
            .lock()
            .ok()
            .and_then(|sessions| sessions.get(project_id).cloned())
            .unwrap_or_default()
    }

    fn store_session(&self, project_id: &str, state: SessionState) {
        if let Ok(mut sessions) = self.sessions.lock() {
            sessions.insert(project_id.to_string(), state);
        }
    }
}

fn decode<T: serde::de::DeserializeOwned>(
    tool_name: &str,
    arguments: Value,
) -> Result<T, ToolError> {
    serde_json::from_value(arguments)
        .map_err(|error| ToolError::invalid(format!("{tool_name} 的参数不合法：{error}")))
}

fn require_query(raw: &str, field: &str) -> Result<(), ToolError> {
    if raw.trim().is_empty() {
        return Err(ToolError::invalid(format!(
            "{field} 不能为空或纯空白：请给出自然语言或符号混合的问题（中英均可）。"
        )));
    }
    if raw.chars().count() > MAX_QUERY_CHARS {
        return Err(ToolError::invalid(format!(
            "{field} 过长（{} 字符，上限 {MAX_QUERY_CHARS}）：请把问题聚焦成一句话。",
            raw.chars().count()
        )));
    }
    Ok(())
}

fn require_max_tokens(value: Option<i64>, limit: i64, field: &str) -> Result<(), ToolError> {
    if let Some(value) = value {
        if value < 1 {
            return Err(ToolError::invalid(format!(
                "{field} 必须 ≥ 1，收到 {value}"
            )));
        }
        if value > limit {
            return Err(ToolError::invalid(format!(
                "{field}={value} 超出上传上限 {limit}：请调小。"
            )));
        }
    }
    Ok(())
}

#[derive(serde::Deserialize)]
struct SearchArguments {
    query: String,
    project_root: String,
    #[serde(default)]
    max_tokens: Option<i64>,
}

#[derive(serde::Deserialize)]
struct AskArguments {
    question: String,
    project_root: String,
    #[serde(default)]
    max_tokens: Option<i64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn definitions_match_cf06_required_fields_and_limits() {
        let tools = definitions();
        let array = tools.as_array().expect("array");
        assert_eq!(array.len(), 2);
        assert_eq!(array[0]["name"], "search_context");
        assert_eq!(array[1]["name"], "ask_project");
        for tool in array {
            assert_eq!(tool["inputSchema"]["additionalProperties"], false);
            let required = tool["inputSchema"]["required"]
                .as_array()
                .expect("required");
            assert_eq!(required.len(), 2);
            // TASK-MCP-BUDGET：max_tokens 不再对 AI 声明（服务端按证据密度决定包大小）
            let props = tool["inputSchema"]["properties"]
                .as_object()
                .expect("properties");
            assert!(
                !props.contains_key("max_tokens"),
                "{} 不应再声明 max_tokens：{:?}",
                tool["name"],
                props.keys().collect::<Vec<_>>()
            );
            assert_eq!(props.len(), 2);
        }
    }

    /// description 必须**逐字**等于 CF-06 契约里那份（TASK-MCP-BUDGET）。
    ///
    /// 为什么要守：此前 client 与 service 各写一份，长度差 4 倍（298 vs 1309 字符），
    /// 而 AI 在 stdio 面看到的是 client 那份——模块文档要求的“查询写法/分工边界/导航”
    /// 实际上到不了 AI 眼前。现在两端同源，本测试防回退。
    #[test]
    fn descriptions_come_from_the_cf06_contract() {
        let payload: Value = serde_json::from_str(CF06_CONTRACT).expect("契约应是合法 JSON");
        let tools = definitions();
        for tool in tools.as_array().expect("array") {
            let name = tool["name"].as_str().expect("name");
            let expected = payload["tools"]
                .as_array()
                .expect("contract tools")
                .iter()
                .find(|item| item["name"] == name)
                .and_then(|item| item["description"].as_str())
                .expect("契约里应有该工具");
            assert_eq!(
                tool["description"].as_str().expect("description"),
                expected,
                "{name} 的 description 与 CF-06 契约不一致（两端同源不能漂移）"
            );
            assert!(!expected.is_empty(), "description 是行为控制，不能为空");
        }
    }

    #[test]
    fn query_validation_rejects_blank_and_oversized() {
        assert!(require_query("   ", "query").is_err());
        assert!(require_query("ok", "query").is_ok());
        let long = "x".repeat(MAX_QUERY_CHARS + 1);
        assert!(require_query(&long, "query").is_err());
    }

    #[test]
    fn max_tokens_validation_still_accepts_legacy_callers() {
        // TASK-MCP-BUDGET：参数已从 schema 移除，但旧编辑器仍可能发送——不得变成参数错误。
        assert!(require_max_tokens(Some(0), MAX_TOKENS_ASK, "max_tokens").is_err());
        assert!(
            require_max_tokens(Some(MAX_TOKENS_ASK + 1), MAX_TOKENS_ASK, "max_tokens").is_err()
        );
        assert!(
            require_max_tokens(Some(MAX_TOKENS_ASK), MAX_TOKENS_ASK, "max_tokens").is_ok()
        );
        assert!(require_max_tokens(None, MAX_TOKENS_ASK, "max_tokens").is_ok());
        // 旧版常用的 10000 仍放行（现在两面共用一个上限）
        assert!(require_max_tokens(Some(DEFAULT_MAX_TOKENS), MAX_TOKENS_ASK, "max_tokens").is_ok());
    }

    #[tokio::test]
    async fn unknown_tool_and_bad_arguments_are_argument_errors() {
        let layer = ToolLayer::new(
            RemoteClient::new("http://127.0.0.1:9", None).expect("client"),
            std::env::temp_dir(),
        );
        assert!(matches!(
            layer.execute("nope", json!({})).await,
            Err(ToolError::UnknownTool(_))
        ));
        assert!(matches!(
            layer.execute("search_context", json!({"query": "x"})).await,
            Err(ToolError::InvalidArguments(_))
        ));
        assert!(matches!(
            layer
                .execute(
                    "search_context",
                    json!({"query": "  ", "project_root": "/tmp"})
                )
                .await,
            Err(ToolError::InvalidArguments(_))
        ));
    }

    /// 构建期副本必须与仓库权威契约**逐字节一致**。
    ///
    /// 为什么要这条：`aarch64-unknown-linux-musl` 走 `cross`（Docker），容器里看不到
    /// 仓库上层的 `docs/`，所以编译时读的是 crate 内的 `contract/mcp-tools.json`。
    /// 副本一旦与权威文件漂移，AI 拿到的就是过期描述——而这和“两端漂移”是同一类
    /// 静默故障（构建成功、行为错误），必须由测试拦住。
    ///
    /// 只拷了 `client/` 出来构建时（权威文件不在位）本测试自动跳过。
    #[test]
    fn contract_copy_matches_repo_copy() {
        let repo_copy = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../docs/contracts/mcp-tools.json");
        if !repo_copy.is_file() {
            eprintln!("跳过：仓库权威契约不在位（{}）", repo_copy.display());
            return;
        }
        let expected = std::fs::read_to_string(&repo_copy).expect("读权威契约");
        let vendored = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("contract/mcp-tools.json"),
        )
        .expect("读构建副本");
        assert_eq!(
            vendored, expected,
            "client/contract/mcp-tools.json 与 docs/contracts/mcp-tools.json 不一致：\n"
        );
    }
}
