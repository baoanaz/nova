//! TASK-099 §B-2 验收：客户端把 callId 注入**每一个**请求头。
//!
//! 为什么必须真的起一个服务端：本卡的客户端改动价值全在"服务端能收到什么"。只测
//! `new_call_id()` 的形状抓不到"忘了在某个端点上带头"这类缺陷——而 `RemoteClient::request`
//! 是所有端点（resolve / batch-upload / deletions / checkpoint / search / ask）的**唯一**出口，
//! 因此这里用一个极小的 HTTP 服务端把它收到的请求头原样记下来，再断言：
//!
//! 1. 一次 `tool call` 的**所有**请求带**同一个** `X-Request-Id`；
//! 2. **两次** `tool call` 带**不同**的值（callId 的粒度是"一次调用"，不是"一个进程"）；
//! 3. 该头与服务端回写响应头用的是同一个名字（`X-Request-Id`，TASK-090 既有实现）。
//!
//! 无新依赖：服务端只用 `std::net::TcpListener` 手写最小 HTTP/1.1 响应。

use std::collections::HashSet;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;

use serde_json::Value;
use zace_client::tools::ToolLayer;

/// 一次请求里我们关心的部分（方法与请求头）。
#[derive(Debug, Clone)]
struct Seen {
    request_line: String,
    call_id: Option<String>,
}

/// 极小 HTTP 服务端：把收到的请求行与 `X-Request-Id` 记进 `seen`，并返回可用的 JSON。
///
/// 路由是"按路径猜需要什么"：只要让客户端能走完
/// `resolve → batch-upload → checkpoint → search` 这条链即可（不校验 blob 语义，
/// 本测试守的是**请求头**，不是同步正确性——后者由 `service/tests/test_sync_api.py` 守）。
fn serve(listener: TcpListener, seen: Arc<Mutex<Vec<Seen>>>) {
    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        let seen = Arc::clone(&seen);
        thread::spawn(move || handle(stream, seen));
    }
}

fn handle(mut stream: TcpStream, seen: Arc<Mutex<Vec<Seen>>>) {
    let mut reader = BufReader::new(stream.try_clone().expect("clone stream"));
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).is_err() {
        return;
    }
    let mut call_id = None;
    let mut content_length = 0usize;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            break;
        }
        let trimmed = line.trim_end();
        if trimmed.is_empty() {
            break;
        }
        let lower = trimmed.to_ascii_lowercase();
        if let Some(value) = lower.strip_prefix("x-request-id:") {
            call_id = Some(value.trim().to_string());
        } else if let Some(value) = lower.strip_prefix("content-length:") {
            content_length = value.trim().parse().unwrap_or(0);
        }
    }
    let mut body = vec![0u8; content_length];
    if content_length > 0 {
        use std::io::Read;
        let _ = reader.read_exact(&mut body);
    }
    seen.lock().expect("seen lock").push(Seen {
        request_line: request_line.trim_end().to_string(),
        call_id: call_id.clone(),
    });

    let payload = response_json(&request_line);
    let header_id = call_id.unwrap_or_else(|| "server-generated".to_string());
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
         X-Request-Id: {header_id}\r\nConnection: close\r\n\r\n{}",
        payload.len(),
        payload
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
}

/// 按路径给一份形状够用的响应（客户端只解析它要的那几个字段）。
fn response_json(request_line: &str) -> String {
    if request_line.contains("/api/projects/resolve") {
        r#"{"projectId":"proj-test"}"#.to_string()
    } else if request_line.contains("/api/sync/batch-upload") {
        r#"{"accepted":[],"skipped":[],"report":{"added":0,"modified":0,"deleted":0,"chunksNew":0,"chunksReused":0,"chunksRemoved":0,"filesParsed":0,"errors":[],"skippedFiles":[]}}"#
            .to_string()
    } else if request_line.contains("/api/sync/status/") {
        r#"{"pendingJobs":0,"indexProgress":{"state":"done","error":null},"skippedFiles":[]}"#
            .to_string()
    } else if request_line.contains("/api/sync/checkpoint") {
        r#"{"checkpointId":"cp_test"}"#.to_string()
    } else if request_line.contains("/api/query/search") {
        r#"{"markdown":"markdown","meta":{}}"#.to_string()
    } else if request_line.contains("/api/query/ask") {
        r#"{"status":"answered","answer":"answer"}"#.to_string()
    } else {
        r#"{}"#.to_string()
    }
}

/// 起一个夹具服务端 + 一个指向它的 `ToolLayer`，返回 `(base_url, seen, tempdir)`。
fn fixture() -> (String, Arc<Mutex<Vec<Seen>>>, tempfile::TempDir) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
    let addr = listener.local_addr().expect("local addr");
    let seen = Arc::new(Mutex::new(Vec::new()));
    {
        let seen = Arc::clone(&seen);
        thread::spawn(move || serve(listener, seen));
    }
    let base_url = format!("http://{addr}");
    (base_url, seen, tempfile::tempdir().expect("temp dir"))
}

/// 一个包含一个文件的仓库（客户端要求索引非空才发上传）。
fn repo_dir() -> tempfile::TempDir {
    let dir = tempfile::tempdir().expect("temp dir");
    std::fs::write(dir.path().join("main.py"), "def run():\n    return 1\n").expect("write");
    dir
}

/// 从记录里取出所有 callId（含 `None`，用于断言"一个都没漏"）。
fn call_ids(seen: &[Seen]) -> Vec<Option<String>> {
    seen.iter().map(|item| item.call_id.clone()).collect()
}

#[test]
fn every_request_of_one_tool_call_carries_the_same_call_id() {
    let (base_url, seen, cache) = fixture();
    let repo = repo_dir();
    let remote = zace_client::remote::RemoteClient::new(&base_url, None).expect("client");
    let layer = ToolLayer::new(remote, cache.path().to_path_buf());
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");

    let result = runtime.block_on(layer.execute(
        "search_context",
        serde_json::json!({
            "query": "run",
            "project_root": repo.path().to_string_lossy(),
        }),
    ));
    assert!(result.is_ok(), "夹具服务端应让调用成功：{result:?}");

    let recorded = seen.lock().expect("seen lock").clone();
    assert!(
        recorded.len() >= 3,
        "一次调用应产生多个请求（resolve/上传/checkpoint/检索），实际：{recorded:?}"
    );
    let ids = call_ids(&recorded);
    assert!(
        ids.iter().all(|item| item.is_some()),
        "**每个**请求都必须带 X-Request-Id（漏一个就是服务端时间线上断链）：{recorded:?}"
    );
    let unique: HashSet<&str> = ids.iter().flatten().map(String::as_str).collect();
    assert_eq!(
        unique.len(),
        1,
        "同一次调用的所有请求必须共享一个 callId：{recorded:?}"
    );
    let id = unique.into_iter().next().expect("至少一个 id");
    let (millis, random) = id.split_once('-').expect("形如 {millis}-{hex}");
    assert!(millis.chars().all(|c| c.is_ascii_digit()), "前缀应是毫秒：{id}");
    assert_eq!(random.len(), 16, "随机部分应是 16 位十六进制：{id}");
}

#[test]
fn two_tool_calls_use_different_call_ids() {
    let (base_url, seen, cache) = fixture();
    let repo = repo_dir();
    let remote = zace_client::remote::RemoteClient::new(&base_url, None).expect("client");
    let layer = ToolLayer::new(remote, cache.path().to_path_buf());
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");

    let args = serde_json::json!({
        "query": "run",
        "project_root": repo.path().to_string_lossy(),
    });
    for _ in 0..2 {
        runtime
            .block_on(layer.execute("search_context", args.clone()))
            .expect("调用成功");
    }

    let recorded = seen.lock().expect("seen lock").clone();
    let unique: HashSet<String> = call_ids(&recorded).into_iter().flatten().collect();
    assert_eq!(
        unique.len(),
        2,
        "两次 tool call 必须拿到两个 callId（否则第二次会被归到第一次的时间线上）：{recorded:?}"
    );
}

#[test]
fn ask_tool_also_carries_the_call_id() {
    let (base_url, seen, cache) = fixture();
    let repo = repo_dir();
    let remote = zace_client::remote::RemoteClient::new(&base_url, None).expect("client");
    let layer = ToolLayer::new(remote, cache.path().to_path_buf());
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");

    runtime
        .block_on(layer.execute(
            "ask_project",
            serde_json::json!({
                "question": "这个项目做什么",
                "project_root": repo.path().to_string_lossy(),
            }),
        ))
        .expect("调用成功");

    let recorded = seen.lock().expect("seen lock").clone();
    assert!(
        recorded.iter().any(|item| item.request_line.contains("/api/query/ask")),
        "ask 应真的打到服务端：{recorded:?}"
    );
    let unique: HashSet<Option<String>> = call_ids(&recorded).into_iter().collect();
    assert_eq!(unique.len(), 1, "ask 的全部请求共享一个 callId：{recorded:?}");
    assert!(unique.into_iter().all(|item| item.is_some()));
}

#[test]
fn response_call_id_header_is_readable_by_the_same_name() {
    // 服务端把同一个头回写（TASK-090 既有行为）；这里确认客户端不会因为大小写
    // 或拼写差异而拿不到它——名字必须是 `X-Request-Id`（服务端 app.py 的常量）。
    assert_eq!(zace_client::remote::CALL_ID_HEADER, "X-Request-Id");
    let ctx = zace_client::remote::CallContext::new();
    let value: Value = serde_json::json!(ctx.id());
    assert!(value.as_str().is_some_and(|text| text.contains('-')));
    assert!(zace_client::remote::RemoteClient::new("http://127.0.0.1:1", None).is_ok());
}
