//! Exercise real HTTP sync readiness, legacy compatibility and retry after cached upload.
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::{Duration, Instant};
use serde_json::{json, Value};
use nova_client::remote::RemoteClient;
use nova_client::tools::ToolLayer;

fn server(responses: Vec<(&'static str, u16, Value)>) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let responses: Vec<_> = responses.into_iter().flat_map(|response| {
        let resolved = response.0.contains("/api/projects/resolve");
        let mut steps = vec![response];
        if resolved {
            steps.push(("POST /api/sync/flush ", 200, json!({"beginAck":true})));
        }
        steps
    }).collect();
    let handle = thread::spawn(move || {
        for (expected, code, mut body) in responses {
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
            if body["beginAck"] == true {
                let value: Value = serde_json::from_slice(&request_body).unwrap();
                assert_eq!(value["begin"], true);
                body = json!({"sessionId": value["sessionId"]});
            }
            if expected.contains("batch-upload") {
                let value: Value = serde_json::from_slice(&request_body).unwrap();
                assert_eq!(value["deferIndexing"], true);
                assert_eq!(value["pipeline"], true);
            }
            if expected.contains("checkpoint") {
                let value: Value = serde_json::from_slice(&request_body).unwrap();
                assert_eq!(value["seal"], true);
            }
            if code == 0 { continue; } // Drop the connection after consuming the request.
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
    let hash = nova_client::blobref::blob_hash("main.py", source.as_bytes());
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
        ("POST /api/sync/checkpoint ", 200, json!({"checkpointId":"cp_test","sealed":true})),
        ("POST /api/query/search ", 200, json!({"markdown":"result","meta":{}})),
    ]);
    let layer = ToolLayer::new(RemoteClient::new(&url, None).unwrap(), cache.path().to_path_buf());
    let rt = runtime();
    let args = json!({"query":"run","project_root":repo.path().to_string_lossy()});
    assert!(rt.block_on(layer.execute("search_context", args.clone())).is_err());
    assert!(rt.block_on(layer.execute("search_context", args)).is_ok());
    handle.join().unwrap();
}

#[test]
fn disconnected_upload_is_retried_without_committing_local_cache() {
    let repo = tempfile::tempdir().unwrap();
    let cache = tempfile::tempdir().unwrap();
    let source = "def run():\n    return 1\n";
    std::fs::write(repo.path().join("main.py"), source).unwrap();
    let hash = nova_client::blobref::blob_hash("main.py", source.as_bytes());
    let (url, handle) = server(vec![
        ("POST /api/projects/resolve ", 200, json!({"projectId":"p"})),
        ("POST /api/sync/batch-upload ", 0, json!({})),
        ("POST /api/projects/resolve ", 200, json!({"projectId":"p"})),
        ("POST /api/sync/batch-upload ", 200,
         json!({"accepted":[hash],"skipped":[],"indexingDeferred":true})),
        ("POST /api/sync/flush ", 200, json!({})),
        ("GET /api/sync/status/p ", 200, ready()),
        ("POST /api/sync/checkpoint ", 200, json!({"checkpointId":"cp_test","sealed":true})),
        ("POST /api/query/search ", 200, json!({"markdown":"result","meta":{}})),
    ]);
    let layer = ToolLayer::new(RemoteClient::new(&url, None).unwrap(), cache.path().to_path_buf());
    let args = json!({"query":"run","project_root":repo.path().to_string_lossy()});
    let rt = runtime();
    assert!(rt.block_on(layer.execute("search_context", args.clone())).is_err());
    assert!(rt.block_on(layer.execute("search_context", args)).is_ok());
    handle.join().unwrap();
}

#[test]
fn seal_conflict_never_queries_and_retry_reuploads_after_client_restart() {
    let repo = tempfile::tempdir().unwrap();
    let cache = tempfile::tempdir().unwrap();
    let source = "def run():\n    return 1\n";
    std::fs::write(repo.path().join("main.py"), source).unwrap();
    let hash = nova_client::blobref::blob_hash("main.py", source.as_bytes());
    let accepted = json!({"accepted":[hash],"skipped":[],"indexingDeferred":true});
    let (url, handle) = server(vec![
        ("POST /api/projects/resolve ", 200, json!({"projectId":"p"})),
        ("POST /api/sync/batch-upload ", 200, accepted.clone()),
        ("POST /api/sync/flush ", 200, json!({})),
        ("GET /api/sync/status/p ", 200, ready()),
        ("POST /api/sync/checkpoint ", 409, json!({"error":"manifest_conflict"})),
        ("POST /api/projects/resolve ", 200, json!({"projectId":"p"})),
        ("POST /api/sync/batch-upload ", 200, accepted),
        ("POST /api/sync/flush ", 200, json!({})),
        ("GET /api/sync/status/p ", 200, ready()),
        ("POST /api/sync/checkpoint ", 200, json!({"checkpointId":"cp_test","sealed":true})),
        ("POST /api/query/search ", 200, json!({"markdown":"result","meta":{}})),
    ]);
    let args = json!({"query":"run","project_root":repo.path().to_string_lossy()});
    let rt = runtime();
    let layer = ToolLayer::new(RemoteClient::new(&url, None).unwrap(), cache.path().to_path_buf());
    assert!(rt.block_on(layer.execute("search_context", args.clone())).is_err());
    drop(layer);
    let restarted = ToolLayer::new(RemoteClient::new(&url, None).unwrap(), cache.path().to_path_buf());
    assert!(rt.block_on(restarted.execute("search_context", args)).is_ok());
    handle.join().unwrap();
}

#[test]
fn checkpoint_without_seal_ack_is_rejected() {
    let (url, handle) = server(vec![
        ("POST /api/sync/checkpoint ", 200, json!({"checkpointId":"legacy"})),
    ]);
    let remote = RemoteClient::new(&url, None).unwrap();
    assert!(runtime().block_on(remote.create_checkpoint("p", &[])).is_err());
    handle.join().unwrap();
}
