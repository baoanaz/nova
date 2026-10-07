//! Exercise real HTTP sync readiness, legacy compatibility and retry after cached upload.
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::{Duration, Instant};
use serde_json::{json, Value};
use zace_client::remote::RemoteClient;
use zace_client::tools::ToolLayer;

fn server(responses: Vec<(&'static str, u16, Value)>) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let handle = thread::spawn(move || {
        for (expected, code, body) in responses {
            let deadline = Instant::now() + Duration::from_secs(5);
            let (mut stream, _) = loop {
                if let Ok(connection) = listener.accept() { break connection; }
                assert!(Instant::now() < deadline, "missing request: {expected}");
                thread::sleep(Duration::from_millis(10));
            };
            stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            assert!(line.replace("?progressOnly=true", "").starts_with(expected), "unexpected request: {line}");
            let mut length = 0;
            let mut has_call_id = false;
            loop {
                line.clear();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" { break; }
                let lower = line.to_ascii_lowercase();
                if let Some(value) = lower.strip_prefix("content-length:") {
                    length = value.trim().parse().unwrap();
                }
                has_call_id |= lower.starts_with("x-request-id:");
            }
            assert!(has_call_id);
            let mut request_body = vec![0; length];
            reader.read_exact(&mut request_body).unwrap();
            if expected.contains("batch-upload") {
                let value: Value = serde_json::from_slice(&request_body).unwrap();
                assert_eq!(value["deferIndexing"], true);
            }
            let payload = body.to_string();
            write!(stream, "HTTP/1.1 {code} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}", payload.len()).unwrap();
        }
    });
    (url, handle)
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap()
}

fn ready() -> Value {
    json!({"pendingJobs":0,"indexProgress":{"state":"done","error":null},"skippedFiles":[]})
}

#[test]
fn polls_until_ready_and_returns_server_skips() {
    let mut done = ready();
    done["skippedFiles"] = json!(["binary.bin"]);
    let (url, handle) = server(vec![
        ("POST /api/sync/flush ", 200, json!({})),
        ("GET /api/sync/status/p ", 200,
         json!({"pendingJobs":1,"indexProgress":{"state":"running","error":null}})),
        ("GET /api/sync/status/p ", 200, done),
    ]);
    let remote = RemoteClient::new(&url, None).unwrap();
    assert_eq!(runtime().block_on(remote.wait_for_indexing("p", true)).unwrap(), ["binary.bin"]);
    handle.join().unwrap();
}

#[test]
fn old_server_fallback_requires_no_deferred_acknowledgement() {
    for confirmed in [false, true] {
        let (url, handle) = server(vec![("POST /api/sync/flush ", 404, json!({}))]);
        let remote = RemoteClient::new(&url, None).unwrap();
        let result = runtime().block_on(remote.wait_for_indexing("p", confirmed));
        assert_eq!(result.is_err(), confirmed);
        handle.join().unwrap();
    }
}

#[test]
fn failed_index_never_queries_and_retry_uses_cached_upload() {
    let repo = tempfile::tempdir().unwrap();
    let cache = tempfile::tempdir().unwrap();
    let source = "def run():\n    return 1\n";
    std::fs::write(repo.path().join("main.py"), source).unwrap();
    let hash = zace_client::blobref::blob_hash("main.py", source.as_bytes());
    let (url, handle) = server(vec![
        ("POST /api/projects/resolve ", 200, json!({"projectId":"p"})),
        ("POST /api/sync/batch-upload ", 200,
         json!({"accepted":[hash],"skipped":[],"report":null,"indexingDeferred":true})),
        ("POST /api/sync/flush ", 200, json!({})),
        ("GET /api/sync/status/p ", 200,
         json!({"pendingJobs":1,"indexProgress":{"state":"failed","error":"provider unavailable"}})),
        // Retry must flush again even when there are no new local changes.
        ("POST /api/projects/resolve ", 200, json!({"projectId":"p"})),
        ("POST /api/sync/flush ", 200, json!({})),
        ("GET /api/sync/status/p ", 200, ready()),
        ("POST /api/sync/checkpoint ", 200, json!({"checkpointId":"cp_test"})),
        ("POST /api/query/search ", 200, json!({"markdown":"result","meta":{}})),
    ]);
    let layer = ToolLayer::new(RemoteClient::new(&url, None).unwrap(), cache.path().to_path_buf());
    let rt = runtime();
    let args = json!({"query":"run","project_root":repo.path().to_string_lossy()});
    assert!(rt.block_on(layer.execute("search_context", args.clone())).is_err());
    assert!(rt.block_on(layer.execute("search_context", args)).is_ok());
    handle.join().unwrap();
}
