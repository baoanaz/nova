//! MCP stdio 协议层（JSON-RPC 2.0 over stdin/stdout）。
//!
//! 纪律（Module 05 §2.2）：
//!
//! - **stdout 只出 JSON-RPC 帧**（一行一帧）；诊断/日志全走 stderr；
//! - 错误三分类：参数错误 → JSON-RPC error（`-32602`）；工具执行错误 → 正常响应 + `isError: true`
//!   （agent 可据原因决策重试）；协议错误 → `-32700` / `-32600` / `-32601`；
//! - 通知（无 `id`）不产生响应帧。
//!
//! 协议版本：与 `mcp==2.2.0`（service 侧 TASK-040 实测的 SDK 版本）一致地协商
//! `2025-11-25` / `2024-11-05`；未知版本回落到最近版本（不报错，保持与编辑器兼容）。

use std::sync::Arc;

use anyhow::Context;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, BufWriter};

use crate::tools::{ToolError, ToolLayer};

pub const SERVER_NAME: &str = "nova";
/// 首选协议版本（与 service 侧 SDK 同代）。
pub const PROTOCOL_VERSION: &str = "2025-11-25";
pub const SUPPORTED_PROTOCOL_VERSIONS: [&str; 2] = [PROTOCOL_VERSION, "2024-11-05"];
/// `tools/list` 的指令文案（行为控制：与 grep/read 的分工）。
const INSTRUCTIONS: &str =
    "nova 提供基于项目索引的上下文检索：search_context 定位跨文件实现/调用链，\
ask_project 回答项目级问题。已知精确标识符的全量引用请用 grep，已知文件请直接 read。";

#[derive(Debug)]
enum RequestId {
    Notification,
    Request(Value),
}

#[derive(Debug)]
struct RpcRequest {
    id: RequestId,
    method: String,
    params: Option<Value>,
}

fn parse_request(frame: &str) -> Result<RpcRequest, RpcError> {
    let value: Value = serde_json::from_str(frame).map_err(|error| RpcError {
        id: Value::Null,
        code: -32700,
        message: format!("Parse error: {error}"),
    })?;
    let object = value.as_object().ok_or_else(invalid_request)?;
    if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return Err(invalid_request());
    }
    let method = object
        .get("method")
        .and_then(Value::as_str)
        .ok_or_else(invalid_request)?
        .to_string();
    let id = match object.get("id") {
        None => RequestId::Notification,
        Some(value) if value.is_null() || value.is_string() || value.is_number() => {
            RequestId::Request(value.clone())
        }
        Some(_) => return Err(invalid_request()),
    };
    Ok(RpcRequest {
        id,
        method,
        params: object.get("params").cloned(),
    })
}

fn invalid_request() -> RpcError {
    RpcError {
        id: Value::Null,
        code: -32600,
        message: "Invalid Request".to_string(),
    }
}

#[derive(Debug)]
struct RpcError {
    id: Value,
    code: i32,
    message: String,
}

#[derive(Serialize)]
struct RpcResponse {
    jsonrpc: &'static str,
    id: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<RpcErrorBody>,
}

#[derive(Serialize)]
struct RpcErrorBody {
    code: i32,
    message: String,
}

impl RpcResponse {
    fn success(id: Value, result: Value) -> Self {
        Self {
            jsonrpc: "2.0",
            id,
            result: Some(result),
            error: None,
        }
    }

    fn error(id: Value, code: i32, message: impl Into<String>) -> Self {
        Self {
            jsonrpc: "2.0",
            id,
            result: None,
            error: Some(RpcErrorBody {
                code,
                message: message.into(),
            }),
        }
    }

    fn from_rpc_error(error: RpcError) -> Self {
        Self::error(error.id, error.code, error.message)
    }
}

/// 工具层 + 协议层（一个进程一个实例）。
#[derive(Clone)]
pub struct Server {
    tools: Arc<ToolLayer>,
}

impl Server {
    pub fn new(tools: ToolLayer) -> Self {
        Self {
            tools: Arc::new(tools),
        }
    }

    /// 处理一帧；返回 `None` 表示"通知，不产生响应"。
    pub async fn process_frame(&self, frame: &str) -> anyhow::Result<Option<String>> {
        let request = match parse_request(frame) {
            Ok(request) => request,
            Err(error) => return Ok(Some(encode(RpcResponse::from_rpc_error(error))?)),
        };
        let RequestId::Request(id) = request.id else {
            return Ok(None);
        };
        let response = self.handle(id, &request.method, request.params).await;
        Ok(Some(encode(response)?))
    }

    async fn handle(&self, id: Value, method: &str, params: Option<Value>) -> RpcResponse {
        match method {
            "initialize" => {
                let requested = params
                    .as_ref()
                    .and_then(|params| params.get("protocolVersion"))
                    .and_then(Value::as_str)
                    .filter(|version| SUPPORTED_PROTOCOL_VERSIONS.contains(version))
                    .unwrap_or(PROTOCOL_VERSION);
                RpcResponse::success(
                    id,
                    json!({
                        "protocolVersion": requested,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": SERVER_NAME, "version": env!("CARGO_PKG_VERSION")},
                        "instructions": INSTRUCTIONS,
                    }),
                )
            }
            "tools/list" => RpcResponse::success(id, json!({"tools": crate::tools::definitions()})),
            "tools/call" => self.handle_tool_call(id, params).await,
            "ping" => RpcResponse::success(id, json!({})),
            _ => RpcResponse::error(id, -32601, format!("Method not found: {method}")),
        }
    }

    async fn handle_tool_call(&self, id: Value, params: Option<Value>) -> RpcResponse {
        let Some(params) = params else {
            return RpcResponse::error(id, -32602, "Missing tools/call params");
        };
        let call: ToolCallParams = match serde_json::from_value(params) {
            Ok(call) => call,
            Err(error) => {
                return RpcResponse::error(
                    id,
                    -32602,
                    format!("Invalid tools/call params: {error}"),
                );
            }
        };
        match self.tools.execute(&call.name, call.arguments).await {
            Ok(text) => RpcResponse::success(id, tool_result(text, false)),
            Err(ToolError::InvalidArguments(message)) => RpcResponse::error(id, -32602, message),
            Err(ToolError::UnknownTool(name)) => {
                RpcResponse::error(id, -32602, format!("Unknown tool: {name}"))
            }
            Err(ToolError::Failed(message)) => RpcResponse::success(id, tool_result(message, true)),
        }
    }
}

#[derive(Deserialize)]
struct ToolCallParams {
    name: String,
    #[serde(default = "empty_object")]
    arguments: Value,
}

fn empty_object() -> Value {
    json!({})
}

fn tool_result(text: String, is_error: bool) -> Value {
    if is_error {
        json!({"content": [{"type": "text", "text": text}], "isError": true})
    } else {
        json!({"content": [{"type": "text", "text": text}]})
    }
}

fn encode(response: RpcResponse) -> anyhow::Result<String> {
    let mut frame =
        serde_json::to_string(&response).context("failed to encode a JSON-RPC frame")?;
    frame.push('\n');
    Ok(frame)
}

/// stdio 主循环（一行一帧；stdout 只写协议帧）。
pub async fn run_stdio(server: Server) -> anyhow::Result<()> {
    let mut input = BufReader::new(tokio::io::stdin()).lines();
    let mut output = BufWriter::new(tokio::io::stdout());
    while let Some(frame) = input
        .next_line()
        .await
        .context("failed to read a JSON-RPC frame from stdin")?
    {
        if frame.trim().is_empty() {
            continue;
        }
        if let Some(response) = server.process_frame(&frame).await? {
            output
                .write_all(response.as_bytes())
                .await
                .context("failed to write a JSON-RPC frame to stdout")?;
            output.flush().await.context("failed to flush stdout")?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_errors_and_invalid_requests_use_standard_codes() {
        assert_eq!(parse_request("{").unwrap_err().code, -32700);
        assert_eq!(
            parse_request(r#"{"jsonrpc":"1.0","id":1,"method":"x"}"#)
                .unwrap_err()
                .code,
            -32600
        );
    }

    #[test]
    fn notifications_are_recognized() {
        let request = parse_request(r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#)
            .expect("notification parses");
        assert!(matches!(request.id, RequestId::Notification));
    }

    #[test]
    fn response_frame_is_a_single_json_line() {
        let frame = encode(RpcResponse::success(
            json!(7),
            json!({"instructions": "line one\nline two"}),
        ))
        .expect("encode");
        assert!(frame.ends_with('\n'));
        assert_eq!(frame.matches('\n').count(), 1);
        let parsed: Value = serde_json::from_str(frame.trim_end()).expect("json");
        assert_eq!(parsed["jsonrpc"], "2.0");
        assert_eq!(parsed["result"]["instructions"], "line one\nline two");
    }

    #[test]
    fn tool_errors_map_to_is_error_result() {
        assert_eq!(
            tool_result("boom".to_string(), true),
            json!({"content": [{"type": "text", "text": "boom"}], "isError": true})
        );
        assert!(tool_result("ok".to_string(), false)
            .get("isError")
            .is_none());
    }
}
