//! 远端同步层（CF-05 的 client 侧）：`resolve` → `batch-upload` → `deletions` → `checkpoint`。
//!
//! **与参考实现（notace）的关键差异**（nova 的 CF-05 已经定型，不能照搬 notace 的形态）：
//!
//! | 主题 | notace | nova（本模块） |
//! |---|---|---|
//! | 项目定位 | 只传 blob 名单，服务端自己认 | **先 `POST /api/projects/resolve`**（D-29 identityKey）拿 `projectId`（云端 MCP 的正解，见就绪度报告 §2） |
//! | blob 内容 | 明文 JSON（`content` 字段） | **base64**（CF-05：`contentB64`，JSON 二进制安全编码） |
//! | blob 名 | `sha256(path‖content)` 无分隔符 | **CF-02 `blob_hash`**（含 `0x00` 分隔符） |
//! | 请求体 | `{blobs:[{path,content}]}` | `{projectId, branch, commit, blobs:[{path,blobHash,contentB64}]}` |
//! | 响应 | `{blob_names[], skipped_blobs[]}` | `{accepted[], skipped[], report{...}}` |
//! | 鉴权 | Bearer（必填） | Bearer（**当前服务端未实现**，见就绪度报告 A1；带上为云端就绪） |
//! | 检索 | `POST /agents/codebase-retrieval`（带 blob 名单） | **`POST /api/query/search`**（带 `projectId` + 可选 `checkpointId`） |
//!
//! 分层超时（D-32）：upload 30s / search 15s / 其它 90s。

use std::collections::HashSet;
use std::path::Path;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use base64::Engine as _;
use serde::{Deserialize, Serialize};

use crate::blobref::blob_hash;

/// 每批上传的字节上限（CF-05：每批 ≤1MB）。
const MAX_BATCH_BYTES: usize = 1024 * 1024;
/// 分层超时（D-32）。
pub const UPLOAD_TIMEOUT: Duration = Duration::from_secs(30);
pub const SEARCH_TIMEOUT: Duration = Duration::from_secs(15);
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(90);
pub const INDEX_WAIT_TIMEOUT: Duration = Duration::from_secs(120);
/// 对外错误文本里的响应体截断（Module 05 §2.2：512 字节）。
const MAX_ERROR_SNIPPET_BYTES: usize = 512;
/// callId（= 服务端的 ``X-Request-Id``）的请求头名（TASK-099 §B-2）。
///
/// **为什么不新增一个头**：服务端 ``app.py`` 的 requestId 中间件早已支持客户端传入
/// ``X-Request-Id`` 并把它回写响应头（TASK-090 §B），而请求头不在 CF-05 的冻结范围
/// （契约只冻结 body 与响应字段集）。直接复用它就少一个契约面，也少一个概念。
pub const CALL_ID_HEADER: &str = "X-Request-Id";

/// 生成一次 Tool 调用的 callId：``{unix_millis}-{16 位十六进制}``。
///
/// 为什么不用 UUID crate：不新增依赖（卡内 §B-2 明确要求）。随机部分用
/// ``std::collections::hash_map::RandomState` 的每进程随机种子 + 时间 + 原子计数器混合——
/// 同进程内不可能重复，跨进程也不同种子；作为"一次调用的分组键"这已经足够
/// （它不是安全凭据，不进入任何鉴权判定）。
pub fn new_call_id(now_millis: u64, extra: u64) -> String {
    use std::hash::{BuildHasher, Hasher};

    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u64(now_millis);
    hasher.write_u64(extra);
    let mixed = hasher.finish();
    format!("{now_millis}-{mixed:016x}")
}

/// 当前时间的毫秒表示（系统时钟异常时退化为 0：callId 仍可用，只是前缀不反映真实时间）。
fn now_millis() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};

    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|delta| delta.as_millis() as u64)
        .unwrap_or(0)
}

/// 一次 Tool 调用一个实例（TASK-099 §B-2）。
///
/// 它的**唯一职责**是把同一个 callId 发给该次调用的**所有**请求：服务端据此把
/// N 条 ``index_runs``（初始化）与 1 条 ``query_audit``（检索）串成一条时间线。
/// 没有它，服务端看到的只是一堆互不相干的请求。
#[derive(Debug, Clone)]
pub struct CallContext {
    id: String,
}

impl CallContext {
    /// 开一次新调用（生成新 callId）。
    pub fn new() -> Self {
        static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let seq = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Self {
            id: new_call_id(now_millis(), seq),
        }
    }

    /// 用指定 id 构造（测试与"由调用方给定"的场景）。
    pub fn with_id(id: impl Into<String>) -> Self {
        Self { id: id.into() }
    }

    pub fn id(&self) -> &str {
        &self.id
    }
}

impl Default for CallContext {
    fn default() -> Self {
        Self::new()
    }
}

/// 一次上传的结果。
#[derive(Debug, Default, Clone)]
pub struct UploadOutcome {
    pub accepted: HashSet<String>,
    pub skipped_paths: HashSet<String>,
    pub batches: usize,
    pub indexing_deferred: bool,
}

/// 远端客户端（一个服务端地址一个实例）。
///
/// ``call``（TASK-099 §B-2）是**本次调用**的上下文：所有请求都带上它的 callId。
/// 字段是 ``Option`` 只为兼容手工构造（测试）；生产路径总是由 ``ToolLayer`` 给出。
pub struct RemoteClient {
    base_url: String,
    token: Option<String>,
    upload: reqwest::Client,
    search: reqwest::Client,
    general: reqwest::Client,
    call: CallContext,
}

impl RemoteClient {
    pub fn new(base_url: &str, token: Option<String>) -> Result<Self> {
        Self::with_call(base_url, token, CallContext::new())
    }

    /// 用指定的调用上下文构造（一次 tool call 一个实例；见 ``ToolLayer``）。
    pub fn with_call(base_url: &str, token: Option<String>, call: CallContext) -> Result<Self> {
        let base_url = normalize_base_url(base_url)?;
        let token = token
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty());
        Ok(Self {
            upload: build_client(UPLOAD_TIMEOUT)?,
            search: build_client(SEARCH_TIMEOUT)?,
            general: build_client(DEFAULT_TIMEOUT)?,
            base_url,
            token,
            call,
        })
    }

    /// 本次调用的 callId（= 发给服务端的 ``X-Request-Id``）。
    pub fn call_id(&self) -> &str {
        self.call.id()
    }

    /// 派生一个"同配置、新调用"的客户端（HTTP 连接池共享，``reqwest::Client`` 内部是 Arc）。
    ///
    /// ``ToolLayer`` 在**每个 tool call 开始时**调它：callId 的粒度是"一次 Tool 调用"
    /// （卡内 §B-1），而不是"一个进程"——否则同一会话里的第二次调用会被归到第一次的
    /// 时间线上（错误分组比不分组更坏）。
    pub fn for_call(&self, call: CallContext) -> Self {
        Self {
            base_url: self.base_url.clone(),
            token: self.token.clone(),
            upload: self.upload.clone(),
            search: self.search.clone(),
            general: self.general.clone(),
            call,
        }
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// `POST /api/projects/resolve` → `projectId`（D-29 身份，幂等）。
    pub async fn resolve_project(&self, identity_key: &str, display_name: &str) -> Result<String> {
        let payload = serde_json::json!({
            "identityKey": identity_key,
            "displayName": display_name,
        });
        let response = self
            .request(
                self.general.post(self.url("/api/projects/resolve")),
                &payload,
            )
            .await?;
        let body: ResolveResponse = response.json().await.context("resolve 响应不是合法 JSON")?;
        if body.project_id.is_empty() {
            bail!("resolve 返回了空的 projectId");
        }
        Ok(body.project_id)
    }

    /// Begin a durable fenced input session before scanning (also for cache hits).
    pub async fn begin_sync(&self, project_id: &str) -> Result<()> {
        let payload = serde_json::json!({
            "projectId": project_id, "sessionId": self.call.id(), "begin": true,
        });
        let response = self.request(self.general.post(self.url("/api/sync/flush")), &payload).await?;
        let body: serde_json::Value = response.json().await?;
        if body["sessionId"].as_str() != Some(self.call.id()) {
            bail!("服务端未确认同步会话，请升级服务端后重试");
        }
        Ok(())
    }

    /// `POST /api/sync/batch-upload`：按 ≤1MB 分批；返回 accepted / skipped。
    /// Call begin_sync first when using this pipeline writer.
    pub async fn upload_files(
        &self,
        project_id: &str,
        files: &[(String, String)],
    ) -> Result<UploadOutcome> {
        let mut outcome = UploadOutcome::default();
        let mut start = 0usize;
        while start < files.len() {
            let mut end = start;
            let mut bytes = 0usize;
            while end < files.len() {
                let (path, content) = &files[end];
                // 批字节估算：base64 膨胀 4/3，另加 path 与 JSON 包装余量。
                let item_bytes = path.len() + content.len() * 4 / 3 + 128;
                if end > start && (bytes + item_bytes > MAX_BATCH_BYTES || end - start >= 64) {
                    break;
                }
                bytes += item_bytes;
                end += 1;
            }
            let batch = &files[start..end];
            let blobs: Vec<UploadBlob> = batch
                .iter()
                .map(|(path, content)| UploadBlob {
                    path: path.clone(),
                    blob_hash: blob_hash(path, content.as_bytes()),
                    content_b64: base64::engine::general_purpose::STANDARD
                        .encode(content.as_bytes()),
                })
                .collect();
            let payload = serde_json::json!({
                "projectId": project_id,
                "blobs": blobs,
                "deferIndexing": true,
                "pipeline": true,
                "sessionId": self.call.id(),
            });
            let response = self
                .request(
                    self.upload.post(self.url("/api/sync/batch-upload")),
                    &payload,
                )
                .await?;
            let body: UploadResponse = response
                .json()
                .await
                .context("batch-upload 响应不是合法 JSON")?;
            if let Some(report) = body.report {
                if !report.errors.is_empty() {
                    bail!("索引失败：{}", report.errors.join("; "));
                }
            }
            outcome.indexing_deferred |= body.indexing_deferred;
            outcome.accepted.extend(body.accepted);
            outcome.skipped_paths.extend(body.skipped);
            outcome.batches += 1;
            start = end;
        }
        Ok(outcome)
    }

    /// `POST /api/sync/deletions`：通知服务端删除路径（幂等）。
    pub async fn notify_deletions(&self, project_id: &str, paths: &[String]) -> Result<bool> {
        if paths.is_empty() {
            return Ok(false);
        }
        let payload = serde_json::json!({
            "projectId": project_id, "paths": paths, "deferIndexing": true,
            "sessionId": self.call.id(),
        });
        let response = self.request(self.general.post(self.url("/api/sync/deletions")), &payload)
            .await?;
        let body: serde_json::Value = response.json().await?;
        Ok(body["indexingDeferred"].as_bool().unwrap_or(false))
    }

    /// 上传完成只代表持久化成功；明确等到后台索引就绪后才能检索。
    pub async fn wait_for_indexing(
        &self, project_id: &str, deferred_confirmed: bool,
    ) -> Result<Vec<String>> {
        tokio::time::timeout(
            INDEX_WAIT_TIMEOUT, self.flush_and_poll(project_id, deferred_confirmed, None),
        ).await.map_err(|_| anyhow!(
            "索引等待超过 120 秒，服务端任务继续在后台执行；请稍后再次调用。"
        ))?
    }

    /// Close the input manifest, reconcile orphan uploads, then await persistence.
    pub async fn wait_for_manifest(&self, project_id: &str, hashes: &[String]) -> Result<Vec<String>> {
        tokio::time::timeout(
            INDEX_WAIT_TIMEOUT, self.flush_and_poll(project_id, true, Some(hashes)),
        ).await.map_err(|_| anyhow!("索引等待超过 120 秒，请稍后重试"))?
    }

    async fn flush_and_poll(
        &self, project_id: &str, deferred_confirmed: bool, hashes: Option<&[String]>,
    ) -> Result<Vec<String>> {
        let mut payload = serde_json::json!({"projectId": project_id});
        if let Some(hashes) = hashes {
            payload["sessionId"] = serde_json::json!(self.call.id());
            payload["blobHashes"] = serde_json::json!(hashes);
        }
        let response = self.send(
            self.general.post(self.url("/api/sync/flush")), Some(&payload),
        ).await?;
        if !deferred_confirmed && matches!(response.status().as_u16(), 404 | 405) {
            return Ok(Vec::new()); // 旧服务仍在每次上传请求内完成索引。
        }
        self.checked_response(response).await?;
        loop {
            let response = self.send(
                self.general.get(self.url(&format!("/api/sync/status/{project_id}?progressOnly=true"))), None,
            ).await?;
            let status: SyncIndexStatus = self.checked_response(response).await?.json().await
                .context("sync/status 响应不完整，不能确认索引就绪")?;
            if let Some(error) = status.index_progress.error {
                bail!("后台索引失败，可重试：{error}");
            }
            if status.index_progress.state == "failed" {
                bail!("后台索引失败，可重试");
            }
            if status.pending_jobs == 0 && status.index_progress.state != "running" {
                return Ok(status.skipped_files);
            }
            if matches!(status.index_progress.state.as_str(), "done" | "idle") {
                self.request(self.general.post(self.url("/api/sync/flush")), &payload).await?;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }

    /// `POST /api/sync/checkpoint`：提交 scope blob 集合换取 `checkpointId`。
    pub async fn create_checkpoint(&self, project_id: &str, hashes: &[String]) -> Result<String> {
        let payload = serde_json::json!({
            "projectId": project_id, "blobHashes": hashes, "seal": true,
            "sessionId": self.call.id(),
        });
        let response = self
            .request(
                self.general.post(self.url("/api/sync/checkpoint")),
                &payload,
            )
            .await?;
        let body: CheckpointResponse = response
            .json()
            .await
            .context("checkpoint 响应不是合法 JSON")?;
        if !body.sealed {
            bail!("服务端未确认最终清单封口，请升级服务端后重试");
        }
        Ok(body.checkpoint_id)
    }

    /// `POST /api/query/search`：返回服务端渲染的 Markdown（D-21：渲染在服务端）。
    pub async fn search(
        &self,
        project_id: &str,
        query: &str,
        max_tokens: i64,
        checkpoint_id: Option<&str>,
    ) -> Result<String> {
        let payload = serde_json::json!({
            "projectId": project_id,
            "query": query,
            "maxTokens": max_tokens,
            "checkpointId": checkpoint_id,
        });
        let response = self
            .request(self.search.post(self.url("/api/query/search")), &payload)
            .await?;
        let body: SearchResponse = response.json().await.context("search 响应不是合法 JSON")?;
        Ok(body.markdown)
    }

    /// `POST /api/query/ask`：四个分支（D-24 短路 / D-26 降级 / TASK-088 LLM）共用同一信封。
    ///
    /// 返回 **已经拼好的展示文本**（``AskResponse::into_text``）——调用方不再需要知道
    /// "这个分支有没有 answer"，四种情况的差别已经在那里处理完了。
    pub async fn ask(
        &self,
        project_id: &str,
        question: &str,
        checkpoint_id: Option<&str>,
    ) -> Result<String> {
        let payload = serde_json::json!({
            "projectId": project_id,
            "question": question,
            "checkpointId": checkpoint_id,
        });
        let response = self
            .request(self.general.post(self.url("/api/query/ask")), &payload)
            .await?;
        let body: AskResponse = response.json().await.context("ask 响应不是合法 JSON")?;
        Ok(body.into_text())
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base_url)
    }

    /// 统一发请求：带上 Bearer（若有）、**callId**（TASK-099 §B-2）并映射可读错误。
    ///
    /// ``X-Request-Id`` 在这里**集中注入**（而不是每个方法各写一遍）：所有端点（resolve /
    /// batch-upload / deletions / checkpoint / query）走的都是本函数，因此不可能漏。
    async fn request(
        &self,
        builder: reqwest::RequestBuilder,
        payload: &serde_json::Value,
    ) -> Result<reqwest::Response> {
        let response = self.send(builder, Some(payload)).await?;
        self.checked_response(response).await
    }

    async fn send(
        &self, builder: reqwest::RequestBuilder, payload: Option<&serde_json::Value>,
    ) -> Result<reqwest::Response> {
        let mut builder = builder.header(CALL_ID_HEADER, self.call.id());
        if let Some(payload) = payload {
            builder = builder.json(payload);
        }
        if let Some(token) = &self.token {
            builder = builder.bearer_auth(token);
        }
        builder.send().await
            .map_err(|error| anyhow!("请求 {} 失败：{error}", self.base_url))
    }

    async fn checked_response(&self, response: reqwest::Response) -> Result<reqwest::Response> {
        let status = response.status();
        if status.is_success() {
            return Ok(response);
        }
        let body = response.text().await.unwrap_or_default();
        let snippet = redact(
            &truncate(&body, MAX_ERROR_SNIPPET_BYTES),
            self.token.as_deref(),
        );
        bail!("服务端返回 HTTP {status}：{snippet}");
    }
}

fn build_client(timeout: Duration) -> Result<reqwest::Client> {
    reqwest::Client::builder()
        .timeout(timeout)
        .user_agent(concat!("nova-client/", env!("CARGO_PKG_VERSION")))
        .build()
        .context("failed to build the HTTP client")
}

fn normalize_base_url(input: &str) -> Result<String> {
    let trimmed = input.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        bail!("服务端地址不能为空");
    }
    if !(trimmed.starts_with("http://") || trimmed.starts_with("https://")) {
        bail!("服务端地址必须以 http:// 或 https:// 开头：{input}");
    }
    Ok(trimmed.to_string())
}

fn truncate(text: &str, limit: usize) -> String {
    if text.len() <= limit {
        return text.to_string();
    }
    let mut end = limit;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}…（已截断）", &text[..end])
}

/// 错误文本脱敏：token 出现即替换（Module 05 §2.2 纪律）。
fn redact(text: &str, token: Option<&str>) -> String {
    match token {
        Some(token) if !token.is_empty() && text.contains(token) => text.replace(token, "***"),
        _ => text.to_string(),
    }
}

#[derive(Deserialize)]
struct ResolveResponse {
    #[serde(rename = "projectId")]
    project_id: String,
}

#[derive(Deserialize)]
struct UploadResponse {
    #[serde(default)]
    accepted: Vec<String>,
    #[serde(default)]
    skipped: Vec<String>,
    #[serde(default, rename = "indexingDeferred")]
    indexing_deferred: bool,
    report: Option<UploadReport>,
}

#[derive(Deserialize)]
struct UploadReport {
    #[serde(default)]
    errors: Vec<String>,
}

#[derive(Deserialize)]
struct SyncIndexStatus {
    #[serde(rename = "pendingJobs")]
    pending_jobs: usize,
    #[serde(rename = "indexProgress")]
    index_progress: SyncProgress,
    #[serde(default, rename = "skippedFiles")]
    skipped_files: Vec<String>,
}

#[derive(Deserialize)]
struct SyncProgress {
    state: String,
    error: Option<String>,
}

#[derive(Deserialize)]
struct CheckpointResponse {
    #[serde(rename = "checkpointId")]
    checkpoint_id: String,
    #[serde(default)]
    sealed: bool,
}

#[derive(Deserialize)]
struct SearchResponse {
    markdown: String,
}

#[derive(Deserialize)]
struct AskResponse {
    status: String,
    /// 四个分支共用同一响应信封（CF-05 / openapi.yaml 的 ``AskResponse``）：
    ///
    /// - ``answered`` / ``degraded``：有答案正文；
    /// - ``insufficient_evidence``（D-24 短路）：**没有 ``answer``**，改给
    ///   ``bestEffortContext`` + ``missingEvidence`` + ``nextQueries``。
    ///
    /// 契约（``docs/contracts/openapi.yaml``）明确写了 "insufficient_evidence 时不出现"，
    /// 因此这里**必须**是 ``Option``——写成必填会让 D-24 短路路径整个反序列化失败，
    /// 客户端只看到 "missing field `answer`"（真实故障：A2 题连续 3 次失败）。
    #[serde(default)]
    answer: Option<String>,
    /// 证据不足时的尽力而为上下文包（``render_markdown(pack)``）。
    #[serde(default, rename = "bestEffortContext")]
    best_effort_context: Option<String>,
    /// 证据不足时的缺口说明（可读文本，与 ``### Missing Evidence`` 同形）。
    #[serde(default, rename = "missingEvidence")]
    missing_evidence: Vec<String>,
    /// 证据不足时的自愈查询建议。
    #[serde(default, rename = "nextQueries")]
    next_queries: Vec<String>,
}

impl AskResponse {
    /// 响应 → 展示给 Agent 的文本（**四个分支都要有可用输出**，绝不空手）。
    ///
    /// 为什么在客户端拼而不是让服务端只回一个字段：服务端已把结构化缺口给全了，
    /// 而 Agent 需要的是“一跟就能看懂”的一段文本——把前提、缺口、建议查询拼在一起，
    /// 比让模型自己去解读 JSON 可靠（也与 ``search_context`` 返回 Markdown 的形态一致）。
    fn into_text(self) -> String {
        if let Some(answer) = self.answer {
            return format!("[nova] status={}\n\n{answer}", self.status);
        }
        let mut parts = vec![format!(
            "[nova] status={}（证据不足：按 D-24 不调用 LLM，以下是尽力而为的上下文）",
            self.status
        )];
        if !self.missing_evidence.is_empty() {
            parts.push(format!(
                "## Missing Evidence\n{}",
                self.missing_evidence
                    .iter()
                    .map(|item| format!("- {item}"))
                    .collect::<Vec<_>>()
                    .join("\n")
            ));
        }
        if !self.next_queries.is_empty() {
            parts.push(format!(
                "## Suggested Next Queries\n{}",
                self.next_queries
                    .iter()
                    .map(|query| format!("- {query}"))
                    .collect::<Vec<_>>()
                    .join("\n")
            ));
        }
        if let Some(context) = self.best_effort_context {
            parts.push(context);
        }
        parts.join("\n\n")
    }
}

#[derive(Serialize)]
struct UploadBlob {
    path: String,
    #[serde(rename = "blobHash")]
    blob_hash: String,
    #[serde(rename = "contentB64")]
    content_b64: String,
}

/// 校验本地目录（MCP 工具入口的共同前置）。
pub fn validate_project_root(root: &str) -> Result<std::path::PathBuf> {
    if root.trim().is_empty() {
        bail!("project_root 不能为空");
    }
    if root.contains('\\') {
        bail!(
            "project_root 含反斜杠，必须是**正斜杠**绝对路径（服务端跑在 WSL/Linux，\
             Windows 路径如 C:\\... 在这里不存在）：收到 {root:?}"
        );
    }
    let path = Path::new(root);
    if !path.is_absolute() {
        bail!("project_root 必须是绝对路径：收到 {root:?}");
    }
    if !path.is_dir() {
        bail!("project_root 不是可读目录：{root}");
    }
    Ok(path.to_path_buf())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn call_id_is_unique_and_time_prefixed() {
        let a = CallContext::new();
        let b = CallContext::new();
        assert_ne!(a.id(), b.id(), "两次调用不能拿到同一个 callId");
        let (prefix, random) = a.id().split_once('-').expect("形如 {millis}-{hex}");
        assert!(prefix.chars().all(|c| c.is_ascii_digit()));
        assert_eq!(random.len(), 16);
        assert!(random.chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(CallContext::with_id("fixed").id(), "fixed");
    }

    #[test]
    fn new_call_id_mixes_inputs() {
        assert_ne!(new_call_id(1, 1), new_call_id(1, 2));
        assert!(new_call_id(1, 1).starts_with("1-"));
    }

    #[test]
    fn base_url_requires_scheme_and_drops_trailing_slash() {
        assert_eq!(
            normalize_base_url("https://nova.example.com/").unwrap(),
            "https://nova.example.com"
        );
        assert!(normalize_base_url("nova.example.com").is_err());
        assert!(normalize_base_url("   ").is_err());
    }

    #[test]
    fn error_text_is_truncated_and_token_redacted() {
        let long = "x".repeat(MAX_ERROR_SNIPPET_BYTES + 50);
        let snippet = truncate(&long, MAX_ERROR_SNIPPET_BYTES);
        assert!(snippet.len() < long.len());
        assert!(snippet.ends_with("…（已截断）"));

        let leaked = r#"{"error":{"message":"bad token sk-secret-123"}}"#;
        assert_eq!(
            redact(leaked, Some("sk-secret-123")),
            r#"{"error":{"message":"bad token ***"}}"#
        );
    }

    #[test]
    fn project_root_validation_rejects_relative_and_backslash_paths() {
        assert!(validate_project_root("relative/path").is_err());
        assert!(validate_project_root("C:\\Users\\x").is_err());
        let dir = tempfile::tempdir().expect("temp dir");
        assert!(validate_project_root(&dir.path().to_string_lossy()).is_ok());
    }

    /// 契约回归（TASK-108）：``answer`` 是**可选**的（D-24 短路包不包含它）。
    ///
    /// 真实故障：`AskResponse.answer` 曾写成必填 ``String``，于是 ``answerable=false``
    /// 时整个响应反序列化失败，Agent 只看到 ``missing field `answer` ```——
    /// 而服务端返回的其实是完整的证据不足说明（契约 ``openapi.yaml`` 明写
    /// "insufficient_evidence 时不出现"）。四个分支都必须能解出可用输出。
    #[test]
    fn ask_response_accepts_every_contract_branch() {
        // ① answered：有 answer，无 bestEffortContext。
        let answered: AskResponse = serde_json::from_value(serde_json::json!({
            "status": "answered",
            "answer": "结论 [E1]。"
        }))
        .expect("answered 分支必须可解");
        let text = answered.into_text();
        assert!(text.contains("status=answered"));
        assert!(text.contains("结论 [E1]。"));

        // ② insufficient_evidence（D-24 短路）：**没有 answer 字段**。
        let short: AskResponse = serde_json::from_value(serde_json::json!({
            "status": "insufficient_evidence",
            "bestEffortContext": "## Relevant Context\n[E1] a.py:1-2",
            "missingEvidence": ["[unresolved_reference] 缺 X"],
            "nextQueries": ["X 在哪里实现"]
        }))
        .expect("D-24 短路包必须可解（旧实现正是在这里失败）");
        let text = short.into_text();
        assert!(text.contains("insufficient_evidence"));
        assert!(text.contains("缺 X"), "缺口说明要如实转达");
        assert!(text.contains("X 在哪里实现"), "建议查询要转达给 Agent");
        assert!(text.contains("[E1] a.py:1-2"), "尽力而为的上下文不能丢");

        // ③ degraded：有 answer（服务端把降级说明写进正文），无短路字段。
        let degraded: AskResponse = serde_json::from_value(serde_json::json!({
            "status": "degraded",
            "answer": "未配置总结模型；以下为检索结果。"
        }))
        .expect("degraded 分支必须可解");
        assert!(degraded.into_text().contains("未配置总结模型"));

        // ④ 空信封：至少不能 panic（缺字段一律走默认）。
        let bare: AskResponse = serde_json::from_value(serde_json::json!({"status": "answered"}))
            .expect("缺字段走默认");
        assert!(bare.into_text().contains("answered"));
    }
}
