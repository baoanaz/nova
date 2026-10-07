//! 二进制级 stdio 验收（notace `tests/stdio.rs` 的对应物）。
//!
//! **为什么必须有这一层**：协议在"有任何东西写到 stdout"的那一刻就碎了。单元测试抓不到——
//! 它只测函数，不测进程；只有真的把二进制当编辑器那样拉起来、喂 stdin、读 stdout，才能发现
//! 一条 `println!`、一个 panic 回溯、或者忘了 flush 导致的协议污染。
//!
//! 本测试**不需要网络**：
//! - `initialize` / `tools/list` / `ping` 在本地回答；
//! - 未知方法在发起任何 HTTP 之前就被拒；
//! - 需要联网的工具调用把地址指向 `127.0.0.1:1`（必然连不上），断言它变成
//!   `isError: true` 的**正常响应**而不是崩溃/污染 stdout（Module 05 §2.2 的工具执行错误面）。

use std::io::Write;
use std::process::{Command, Stdio};

/// 拉起二进制、喂入若干帧、返回 `(stdout, stderr, exit_ok)`。
fn run_binary(requests: &[&str], base_url: &str) -> (String, String, bool) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_nova-client"))
        .env("NOVA_BASE_URL", base_url)
        .env(
            "NOVA_CLIENT_CACHE",
            std::env::temp_dir().join("nova-stdio-test-cache"),
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("nova-client 二进制应能启动");

    {
        let stdin = child.stdin.as_mut().expect("stdin 已管道化");
        for frame in requests {
            writeln!(stdin, "{frame}").expect("写入一帧");
        }
    }
    // 关闭 stdin → 主循环读到 EOF → 进程正常退出；
    let output = child.wait_with_output().expect("进程应正常结束");
    (
        String::from_utf8_lossy(&output.stdout).into_owned(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
        output.status.success(),
    )
}

/// stdout 的每一行都必须是合法 JSON-RPC 帧（这是"协议纯净"的唯一判据）。
fn parse_stdout_frames(stdout: &str) -> Vec<serde_json::Value> {
    stdout
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| {
            serde_json::from_str::<serde_json::Value>(line).unwrap_or_else(|error| {
                panic!("stdout 出现非 JSON-RPC 内容（协议被污染）：{line:?}（{error}）")
            })
        })
        .collect()
}

#[test]
fn stdout_carries_only_json_rpc_frames() {
    let (stdout, stderr, ok) = run_binary(
        &[
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}"#,
            r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#,
            r#"{"jsonrpc":"2.0","id":3,"method":"ping"}"#,
            r#"{"jsonrpc":"2.0","id":4,"method":"no/such/method"}"#,
        ],
        "http://127.0.0.1:1",
    );

    assert!(ok, "进程应以 0 退出；stderr={stderr}");
    let frames = parse_stdout_frames(&stdout);

    // 4 个请求 → 4 帧（通知不产生帧）
    assert_eq!(frames.len(), 4, "实际帧：{stdout}");
    assert!(frames.iter().all(|frame| frame["jsonrpc"] == "2.0"));

    // initialize
    assert_eq!(frames[0]["id"], 1);
    assert_eq!(frames[0]["result"]["protocolVersion"], "2025-11-25");
    assert_eq!(frames[0]["result"]["serverInfo"]["name"], "nova");

    // tools/list：恰好两个 CF-06 工具，字段齐全
    let tools = frames[1]["result"]["tools"]
        .as_array()
        .expect("tools 是数组");
    let names: Vec<&str> = tools
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["search_context", "ask_project"]);
    for tool in tools {
        assert_eq!(tool["inputSchema"]["additionalProperties"], false);
        assert!(tool["inputSchema"]["required"].as_array().is_some());
    }

    // ping
    assert_eq!(frames[2]["result"], serde_json::json!({}));

    // 未知方法 → -32601（协议错误，不是 isError）
    assert_eq!(frames[3]["error"]["code"], -32601);
}

#[test]
fn argument_errors_use_json_rpc_error_codes_without_touching_the_network() {
    let (stdout, stderr, ok) = run_binary(
        &[
            // 相对路径：参数错误，必须在发起 HTTP 之前被拒
            r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_context","arguments":{"query":"x","project_root":"relative/path"}}}"#,
            // 空查询
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_context","arguments":{"query":"   ","project_root":"/tmp"}}}"#,
            // 未知工具
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"nope","arguments":{}}}"#,
            // max_tokens 超服务端上限（TASK-MCP-BUDGET：已从 schema 移除，但仍接收并校验）
            r#"{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"search_context","arguments":{"query":"x","project_root":"/tmp","max_tokens":99999}}}"#,
        ],
        "http://127.0.0.1:1",
    );

    assert!(ok, "进程应以 0 退出；stderr={stderr}");
    let frames = parse_stdout_frames(&stdout);
    assert_eq!(frames.len(), 4);
    for frame in &frames {
        assert_eq!(
            frame["error"]["code"], -32602,
            "参数错误应为 -32602：{frame}"
        );
        assert!(frame.get("result").is_none(), "错误响应不得带 result");
    }
}

#[test]
fn unreachable_server_becomes_an_is_error_result_not_a_crash() {
    // 目录存在但服务端不可达：工具调用应变成 isError=true 的正常响应。
    let dir = tempfile::tempdir().expect("temp dir");
    std::fs::write(dir.path().join("a.txt"), "hello").expect("write fixture");
    let call = format!(
        r#"{{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{{"name":"search_context","arguments":{{"query":"hello","project_root":"{}"}}}}}}"#,
        dir.path().to_string_lossy()
    );

    let (stdout, _stderr, ok) = run_binary(&[&call], "http://127.0.0.1:1");
    assert!(ok, "进程不应因服务端不可达而退出非 0");
    let frames = parse_stdout_frames(&stdout);

    assert_eq!(frames.len(), 1);
    let result = &frames[0]["result"];
    assert_eq!(result["isError"], true, "应为工具执行错误：{result}");
    assert_eq!(result["content"][0]["type"], "text");
    let text = result["content"][0]["text"].as_str().expect("text");
    assert!(!text.is_empty(), "错误文本必须给出可读原因");
}

#[test]
fn malformed_json_produces_a_parse_error_without_killing_the_loop() {
    let (stdout, _stderr, ok) = run_binary(
        &["{not json", r#"{"jsonrpc":"2.0","id":2,"method":"ping"}"#],
        "http://127.0.0.1:1",
    );

    assert!(ok, "解析错误不应终止进程");
    let frames = parse_stdout_frames(&stdout);
    assert_eq!(frames.len(), 2, "解析错误后应继续处理下一帧");
    assert_eq!(frames[0]["error"]["code"], -32700);
    assert_eq!(frames[1]["result"], serde_json::json!({}));
}

#[test]
fn missing_base_url_fails_loudly_on_stderr_with_clean_stdout() {
    let output = Command::new(env!("CARGO_BIN_EXE_nova-client"))
        .env_remove("NOVA_BASE_URL")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .expect("进程应能启动");

    assert!(!output.status.success(), "缺少 --base-url 应退出非 0");
    assert!(
        output.stdout.is_empty(),
        "stdout 必须为空（诊断只能走 stderr）：{:?}",
        String::from_utf8_lossy(&output.stdout)
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("base-url"), "stderr 应说明缺什么：{stderr}");
}
