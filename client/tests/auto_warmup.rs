//! Real MCP processes + local HTTP fixture. No paid API, model download or external network.
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

#[derive(Clone)]
struct Seen { path: String, body: Value }
#[derive(Default)]
struct State {
    seen: Mutex<Vec<Seen>>,
    files: Mutex<HashMap<String, HashMap<String, String>>>,
    ready: AtomicBool,
    failed: AtomicBool,
    network_failure: AtomicBool,
    unavailable_identity: Mutex<Option<String>>,
    stop: AtomicBool,
}
struct Mock { url: String, state: Arc<State>, listener: Option<thread::JoinHandle<()>> }
impl Mock {
    fn new(ready: bool) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let state = Arc::new(State::default());
        state.ready.store(ready, Ordering::SeqCst);
        let shared = state.clone();
        let worker = thread::spawn(move || {
            let mut handlers = Vec::new();
            while !shared.stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let state = shared.clone();
                        handlers.push(thread::spawn(move || respond(stream, &state)));
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(2)),
                    Err(error) => panic!("accept: {error}"),
                }
            }
            for handler in handlers { handler.join().unwrap(); }
        });
        Self { url, state, listener: Some(worker) }
    }
    fn count(&self, suffix: &str) -> usize {
        self.state.seen.lock().unwrap().iter().filter(|seen| seen.path.contains(suffix)).count()
    }
    fn uploads(&self) -> Vec<Value> {
        self.state.seen.lock().unwrap().iter().filter(|seen| seen.path.contains("batch-upload"))
            .map(|seen| seen.body.clone()).collect()
    }
}
impl Drop for Mock {
    fn drop(&mut self) {
        self.state.stop.store(true, Ordering::SeqCst);
        self.listener.take().unwrap().join().unwrap();
    }
}
fn respond(mut stream: TcpStream, state: &State) {
    stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut line = String::new();
    if reader.read_line(&mut line).unwrap_or(0) == 0 { return; }
    let path = line.split_whitespace().nth(1).unwrap().to_string();
    let mut length = 0;
    loop {
        line.clear();
        if reader.read_line(&mut line).unwrap_or(0) == 0 { return; }
        if line == "\r\n" { break; }
        if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            length = value.trim().parse().unwrap();
        }
    }
    let mut bytes = vec![0; length];
    if reader.read_exact(&mut bytes).is_err() { return; }
    let body: Value = if bytes.is_empty() { json!({}) } else { serde_json::from_slice(&bytes).unwrap() };
    state.seen.lock().unwrap().push(Seen { path: path.clone(), body: body.clone() });
    let pid = body["projectId"].as_str().unwrap_or("");
    let (code, response) = if path.ends_with("/resolve") {
        if state.network_failure.load(Ordering::SeqCst)
            || state.unavailable_identity.lock().unwrap().as_deref() == body["identityKey"].as_str() {
            (503, json!({"error":{"message":"temporary network/provider failure"}}))
        } else {
            let material = if body.get("clientScope").is_some() { body.to_string() } else { body["identityKey"].as_str().unwrap().to_string() };
            let project = nova_client::identity::project_id_for(&material);
            (200, json!({"projectId":project,"routingVersion":1}))
        }
    } else if path.ends_with("batch-upload") {
        let mut accepted = Vec::new();
        let mut projects = state.files.lock().unwrap();
        let files = projects.entry(pid.to_string()).or_default();
        for blob in body["blobs"].as_array().unwrap() {
            accepted.push(blob["blobHash"].clone());
            files.insert(blob["path"].as_str().unwrap().to_string(), blob["blobHash"].as_str().unwrap().to_string());
        }
        (200, json!({"accepted":accepted,"skipped":[],"report":null,"indexingDeferred":true}))
    } else if path.contains("/status/") {
        let failed = state.failed.load(Ordering::SeqCst);
        let ready = state.ready.load(Ordering::SeqCst);
        (200, json!({"pendingJobs":if ready && !failed {0} else {1},
            "indexProgress":{"state":if failed {"failed"} else if ready {"done"} else {"running"},
                "error":if failed {Some("fake provider unavailable")} else {None}},"skippedFiles":[]}))
    } else if path.ends_with("deletions") {
        if let Some(files) = state.files.lock().unwrap().get_mut(pid) {
            for path in body["paths"].as_array().unwrap() { files.remove(path.as_str().unwrap()); }
        }
        (200, json!({"indexingDeferred":true}))
    } else if path.ends_with("checkpoint") {
        (200, json!({"checkpointId":format!("cp_{pid}")}))
    } else if path.contains("/query/") {
        let files = state.files.lock().unwrap();
        let mut paths: Vec<_> = files.get(pid).into_iter().flat_map(|files| files.keys()).cloned().collect();
        paths.sort();
        let text = format!("project={pid} files={}", paths.join(","));
        if path.ends_with("/ask") { (200, json!({"status":"answered","answer":text})) }
        else { (200, json!({"markdown":text,"meta":{}})) }
    } else { (200, json!({})) };
    let response = response.to_string();
    let _ = write!(stream, "HTTP/1.1 {code} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}", response.len());
}

struct Mcp {
    child: Child,
    input: Option<ChildStdin>,
    frames: mpsc::Receiver<Value>,
    stash: Vec<Value>,
    logs: Arc<Mutex<Vec<String>>>,
}
impl Mcp {
    fn new(mock: &Mock, cache: &Path, workspace: Option<&Path>, config: &[(&str, &str)]) -> Self {
        Self::in_cwd(mock, cache, workspace, config, cache)
    }
    fn in_cwd(mock: &Mock, cache: &Path, workspace: Option<&Path>, config: &[(&str, &str)], cwd: &Path) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_nova-client"));
        command.current_dir(cwd).env("NOVA_BASE_URL", &mock.url).env("NOVA_CLIENT_CACHE", cache)
            .env_remove("NOVA_API_TOKEN").env_remove("CLAUDE_PROJECT_DIR").env_remove("NOVA_PROJECT_DIR")
            .env_remove("NOVA_AUTO_WARMUP").env_remove("NOVA_NON_GIT_INDEX")
            .env_remove("NOVA_WORKSPACE_ISOLATION").env_remove("NOVA_DEVICE_ID")
            .env("NOVA_WARMUP_TIMEOUT_SECS", "8").env("NOVA_WARMUP_READY_TTL_SECS", "60")
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        if let Some(root) = workspace { command.env("CLAUDE_PROJECT_DIR", root); }
        for (key, value) in config { command.env(key, value); }
        let mut child = command.spawn().unwrap();
        let input = child.stdin.take();
        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        let logs = Arc::new(Mutex::new(Vec::new()));
        let log_writer = logs.clone();
        thread::spawn(move || { for line in BufReader::new(stderr).lines() {
            match line { Ok(line) => log_writer.lock().unwrap().push(line), Err(_) => break }
        } });
        let (sender, frames) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let frame: Value = serde_json::from_str(&line.unwrap()).expect("stdout must contain JSON-RPC only");
                assert_eq!(frame["jsonrpc"], "2.0");
                if sender.send(frame).is_err() { break; }
            }
        });
        Self { child, input, frames, stash: Vec::new(), logs }
    }
    fn send(&mut self, value: Value) {
        writeln!(self.input.as_mut().unwrap(), "{value}").unwrap();
        self.input.as_mut().unwrap().flush().unwrap();
    }
    fn response(&mut self, id: i64, timeout: Duration) -> Value {
        let started = Instant::now();
        loop {
            if let Some(position) = self.stash.iter().position(|frame| frame["id"] == id) { return self.stash.remove(position); }
            let remaining = timeout.checked_sub(started.elapsed()).expect("response deadline exceeded");
            self.stash.push(self.frames.recv_timeout(remaining).expect("missing MCP reply"));
        }
    }
    fn initialize(&mut self) {
        self.send(json!({"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}));
        assert!(self.response(0, Duration::from_secs(2)).get("result").is_some());
    }
    fn initialized(&mut self) { self.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"})); }
    fn call(&mut self, id: i64, root: &Path, ask: bool) {
        let mut arguments = json!({"project_root":root.to_string_lossy()});
        arguments[if ask { "question" } else { "query" }] = json!("run guide");
        self.send(json!({"jsonrpc":"2.0","id":id,"method":"tools/call","params":{
            "name":if ask {"ask_project"} else {"search_context"},"arguments":arguments}}));
    }
    fn success(&mut self, id: i64) -> String {
        let frame = self.response(id, Duration::from_secs(8));
        assert!(frame["result"]["isError"] != true, "tool failed: {frame}");
        assert!(frame.get("error").is_none(), "RPC error: {frame}");
        frame["result"]["content"][0]["text"].as_str().unwrap().to_string()
    }
    fn kill(&mut self) { let _ = self.child.kill(); let _ = self.child.wait(); }
}
impl Drop for Mcp { fn drop(&mut self) { self.kill(); } }
fn wait(condition: impl Fn() -> bool) {
    let started = Instant::now();
    while !condition() {
        assert!(started.elapsed() < Duration::from_secs(5), "fixture condition timed out");
        thread::sleep(Duration::from_millis(5));
    }
}
fn document_repo() -> tempfile::TempDir {
    let repo = tempfile::tempdir().unwrap();
    std::fs::write(repo.path().join("guide.md"), "# run guide\n").unwrap();
    repo
}
fn git(root: &Path, args: &[&str]) {
    assert!(Command::new("git").arg("-C").arg(root).args(args).status().unwrap().success());
}

#[test]
fn verified_git_cwd_warms_without_claude_environment() {
    let mock = Mock::new(true);
    let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    git(repo.path(), &["init", "-q"]);
    std::fs::write(repo.path().join("main.py"), "print('git cwd')\n").unwrap();
    let mut mcp = Mcp::in_cwd(&mock, cache.path(), None, &[], repo.path());
    mcp.initialize();
    assert_eq!(mock.count("resolve"), 0);
    mcp.initialized();
    wait(|| mock.count("checkpoint") == 1);
    assert!(mock.uploads().iter().any(|upload| upload["blobs"].as_array().unwrap().iter().any(|blob| blob["path"] == "main.py")));
    assert!(mcp.logs.lock().unwrap().iter().any(|line| line.contains("workspace_source=cwd_git")));
}

#[test]
fn verified_document_cwd_only_uploads_markdown() {
    let mock = Mock::new(true);
    let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    std::fs::write(repo.path().join("README.md"), "project docs\n").unwrap();
    std::fs::write(repo.path().join("main.py"), "must not upload\n").unwrap();
    std::fs::create_dir(repo.path().join(".agent")).unwrap();
    std::fs::write(repo.path().join(".agent/rules.md"), "project rules\n").unwrap();
    let mut mcp = Mcp::in_cwd(&mock, cache.path(), None, &[], repo.path());
    mcp.initialize(); mcp.initialized();
    wait(|| mock.count("checkpoint") == 1);
    let uploads = mock.uploads();
    let paths: Vec<_> = uploads.iter().flat_map(|upload| upload["blobs"].as_array().unwrap()).map(|blob| blob["path"].as_str().unwrap()).collect();
    assert!(paths.contains(&".agent/rules.md"));
    assert!(!paths.contains(&"main.py"));
}

#[test]
fn nova_project_dir_overrides_claude_and_cwd() {
    let mock = Mock::new(true);
    let selected = tempfile::tempdir().unwrap();
    let other = document_repo(); let cache = tempfile::tempdir().unwrap();
    std::fs::write(selected.path().join("selected.md"), "selected workspace\n").unwrap();
    git(other.path(), &["init", "-q"]);
    let mut mcp = Mcp::in_cwd(&mock, cache.path(), Some(other.path()), &[("NOVA_PROJECT_DIR", selected.path().to_str().unwrap())], other.path());
    mcp.initialize(); mcp.initialized();
    wait(|| mock.count("checkpoint") == 1);
    let uploads = mock.uploads();
    let paths: Vec<_> = uploads.iter().flat_map(|upload| upload["blobs"].as_array().unwrap()).map(|blob| blob["path"].as_str().unwrap()).collect();
    assert_eq!(paths, vec!["selected.md"]);
}

#[test]
fn home_and_plugin_cache_cwd_do_not_make_network_requests() {
    let root = tempfile::tempdir().unwrap();
    let plugin = root.path().join(".claude/plugins/cache/plugin");
    std::fs::create_dir_all(&plugin).unwrap();
    for path in [root.path(), plugin.as_path()] {
        git(path, &["init", "-q"]);
        std::fs::write(path.join("README.md"), "must not automatically upload\n").unwrap();
        let mock = Mock::new(true); let cache = tempfile::tempdir().unwrap();
        let mut mcp = Mcp::in_cwd(&mock, cache.path(), None, &[("HOME", root.path().to_str().unwrap())], path);
        mcp.initialize(); mcp.initialized();
        wait(|| mcp.logs.lock().unwrap().iter().any(|line| line.contains("auto_warmup=skipped")));
        assert_eq!(mock.count("resolve"), 0);
        mcp.send(json!({"jsonrpc":"2.0","id":1,"method":"ping"}));
        assert!(mcp.response(1, Duration::from_secs(1)).get("result").is_some());
    }
}

#[test]
fn automatic_warmup_is_post_handshake_and_requests_remain_responsive() {
    let mock = Mock::new(false);
    let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    let mut mcp = Mcp::new(&mock, cache.path(), Some(repo.path()), &[]);
    mcp.initialize();
    thread::sleep(Duration::from_millis(50));
    assert_eq!(mock.count("resolve"), 0, "initialize alone must not trigger indexing");
    mcp.initialized(); mcp.initialized();
    wait(|| mock.count("/status/") > 0);
    mcp.call(1, repo.path(), false); mcp.call(2, repo.path(), true);
    let started = Instant::now();
    mcp.send(json!({"jsonrpc":"2.0","id":3,"method":"ping"}));
    mcp.send(json!({"jsonrpc":"2.0","id":4,"method":"tools/list"}));
    assert_eq!(mcp.response(3, Duration::from_secs(1))["result"], json!({}));
    assert_eq!(mcp.response(4, Duration::from_secs(1))["result"]["tools"].as_array().unwrap().len(), 2);
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(mock.count("/query/"), 0);
    mock.state.ready.store(true, Ordering::SeqCst);
    mcp.success(1); mcp.success(2);
    assert_eq!(mock.count("resolve"), 1); assert_eq!(mock.count("batch-upload"), 1);
    assert_eq!(mock.count("/flush"), 1); assert_eq!(mock.count("checkpoint"), 1);
    mcp.call(5, repo.path(), false); mcp.success(5);
    assert_eq!(mock.count("resolve"), 1, "completed warmup must be reused");
}

#[test]
fn two_real_processes_share_one_index_task() {
    let mock = Mock::new(false); let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    let mut first = Mcp::new(&mock, cache.path(), Some(repo.path()), &[]);
    let mut second = Mcp::new(&mock, cache.path(), Some(repo.path()), &[]);
    first.initialize(); second.initialize(); first.initialized(); second.initialized();
    first.call(1, repo.path(), false); second.call(1, repo.path(), false);
    wait(|| mock.count("/status/") > 0);
    thread::sleep(Duration::from_millis(100));
    assert_eq!(mock.count("resolve"), 1);
    mock.state.ready.store(true, Ordering::SeqCst);
    first.success(1); second.success(1);
    assert_eq!(mock.count("batch-upload"), 1); assert_eq!(mock.count("checkpoint"), 1);
    assert_eq!(mock.count("/query/search"), 2);
}

#[test]
fn cancelling_a_waiter_does_not_cancel_shared_warmup() {
    let mock = Mock::new(false); let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    let mut mcp = Mcp::new(&mock, cache.path(), Some(repo.path()), &[]);
    mcp.initialize(); mcp.initialized(); wait(|| mock.count("/status/") > 0);
    mcp.call(1, repo.path(), false);
    thread::sleep(Duration::from_millis(50));
    mcp.send(json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}));
    mcp.send(json!({"jsonrpc":"2.0","id":2,"method":"ping"}));
    mcp.response(2, Duration::from_secs(1));
    mock.state.ready.store(true, Ordering::SeqCst);
    mcp.call(3, repo.path(), false); mcp.success(3);
    assert_eq!(mock.count("resolve"), 1); assert_eq!(mock.count("batch-upload"), 1);
    assert_eq!(mock.count("/query/search"), 1);
}

#[test]
fn killed_owner_releases_lock_and_surviving_process_recovers_cached_upload() {
    let mock = Mock::new(false); let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    let mut first = Mcp::new(&mock, cache.path(), Some(repo.path()), &[]);
    first.initialize(); first.initialized(); wait(|| mock.count("/status/") > 0);
    let mut second = Mcp::new(&mock, cache.path(), Some(repo.path()), &[]);
    second.initialize(); second.initialized(); second.call(1, repo.path(), false);
    first.kill(); mock.state.ready.store(true, Ordering::SeqCst);
    second.success(1);
    assert_eq!(mock.count("batch-upload"), 1, "durable accepted cache must survive owner exit");
    assert_eq!(mock.count("resolve"), 2); assert_eq!(mock.count("checkpoint"), 1);
}

#[test]
fn failed_index_and_network_failure_are_retryable_without_duplicate_upload() {
    let mock = Mock::new(true); let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    mock.state.network_failure.store(true, Ordering::SeqCst);
    let mut mcp = Mcp::new(&mock, cache.path(), None, &[]);
    mcp.call(1, repo.path(), false);
    assert_eq!(mcp.response(1, Duration::from_secs(3))["result"]["isError"], true);
    assert_eq!(mock.count("batch-upload"), 0);
    mock.state.network_failure.store(false, Ordering::SeqCst);
    mock.state.failed.store(true, Ordering::SeqCst);
    mcp.call(2, repo.path(), false);
    assert_eq!(mcp.response(2, Duration::from_secs(3))["result"]["isError"], true);
    assert_eq!(mock.count("/query/"), 0);
    mock.state.failed.store(false, Ordering::SeqCst);
    mcp.call(3, repo.path(), false); mcp.success(3);
    assert_eq!(mock.count("batch-upload"), 1); assert_eq!(mock.count("/query/search"), 1);
}

#[test]
fn total_timeout_does_not_poison_retry_or_block_ping() {
    let mock = Mock::new(false); let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    let mut mcp = Mcp::new(&mock, cache.path(), None, &[("NOVA_WARMUP_TIMEOUT_SECS", "1")]);
    mcp.call(1, repo.path(), false);
    let frame = mcp.response(1, Duration::from_secs(3));
    assert_eq!(frame["result"]["isError"], true);
    assert!(frame["result"]["content"][0]["text"].as_str().unwrap().contains("超时"));
    mcp.send(json!({"jsonrpc":"2.0","id":2,"method":"ping"}));
    mcp.response(2, Duration::from_secs(1));
    mock.state.ready.store(true, Ordering::SeqCst);
    mcp.call(3, repo.path(), false); mcp.success(3);
    assert_eq!(mock.count("batch-upload"), 1);
}

#[test]
fn absent_or_disabled_workspace_does_not_guess_a_scan_root() {
    let mock = Mock::new(true); let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    for (root, config) in [(None, vec![]), (Some(repo.path()), vec![("NOVA_AUTO_WARMUP", "off")])] {
        let mut mcp = Mcp::new(&mock, cache.path(), root, &config);
        mcp.initialize(); mcp.initialized();
        mcp.send(json!({"jsonrpc":"2.0","id":1,"method":"ping"}));
        mcp.response(1, Duration::from_secs(1));
    }
    assert_eq!(mock.count("resolve"), 0);
}

#[test]
fn mixed_workspace_fuses_code_and_hidden_markdown_without_double_indexing() {
    let mock = Mock::new(true); let root = tempfile::tempdir().unwrap(); let cache = tempfile::tempdir().unwrap();
    let repo = root.path().join("code"); std::fs::create_dir(&repo).unwrap(); git(&repo, &["init", "-q"]);
    std::fs::write(repo.join("run.py"), "def run(): return 1\n").unwrap();
    std::fs::write(repo.join("README.md"), "# code guide\n").unwrap();
    std::fs::create_dir(root.path().join(".claude")).unwrap();
    let doc = root.path().join(".claude/guide.markdown"); std::fs::write(&doc, "# external guide\n").unwrap();
    std::fs::write(root.path().join("secret.env"), "secret").unwrap();
    let mut mcp = Mcp::new(&mock, cache.path(), Some(root.path()), &[]);
    mcp.initialize(); mcp.initialized(); mcp.call(1, root.path(), false);
    let text = mcp.success(1);
    assert!(text.contains("run.py") && text.contains(".claude/guide.markdown"));
    assert!(text.contains("来源：Git code") && text.contains("来源：工作区仓库外文档"));
    let uploads = mock.uploads(); assert_eq!(uploads.len(), 2);
    let all_paths: Vec<_> = uploads.iter().flat_map(|body| body["blobs"].as_array().unwrap())
        .map(|blob| blob["path"].as_str().unwrap()).collect();
    assert_eq!(all_paths.iter().filter(|path| **path == "README.md").count(), 1);
    assert!(!all_paths.contains(&"code/README.md") && !all_paths.contains(&"secret.env"));
    mcp.call(2, root.path(), true); let answer = mcp.success(2);
    assert!(answer.contains("run.py") && answer.contains(".claude/guide.markdown"));
    // `/query/ask` 是子串匹配，也会命中统一问答端点：混合工作区只允许一次 ask-workspace，
    // 不允许对每个项目各发一次 /ask（那会成倍调用总结模型）。
    assert_eq!(mock.count("/query/ask-workspace"), 1);
    assert_eq!(mock.count("/query/ask"), 1, "mixed ask must not multiply answer-model calls");
    assert_eq!(mock.count("resolve"), 2);
    std::fs::remove_file(doc).unwrap();
    mcp.call(3, root.path(), false); let text = mcp.success(3);
    assert!(!text.contains(".claude/guide.markdown"), "deleted last document must not remain as evidence");
    assert_eq!(mock.count("deletions"), 1); assert_eq!(mock.count("batch-upload"), 2);
}

#[test]
fn non_git_full_index_requires_explicit_configuration() {
    let mock = Mock::new(true); let root = tempfile::tempdir().unwrap(); let cache = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("run.py"), "def run(): pass\n").unwrap();
    let mut mcp = Mcp::new(&mock, cache.path(), None, &[]);
    mcp.call(1, root.path(), false);
    assert_eq!(mcp.response(1, Duration::from_secs(3))["result"]["isError"], true);
    assert_eq!(mock.count("batch-upload"), 0);
    drop(mcp);
    let mut mcp = Mcp::new(&mock, cache.path(), None, &[("NOVA_NON_GIT_INDEX", "full")]);
    mcp.call(1, root.path(), false); assert!(mcp.success(1).contains("run.py"));
    assert_eq!(mock.count("batch-upload"), 1);
    drop(mcp);
    let mut docs = Mcp::new(&mock, cache.path(), None, &[]);
    docs.call(1, root.path(), false);
    assert_eq!(docs.response(1, Duration::from_secs(3))["result"]["isError"], true);
    assert_eq!(mock.count("deletions"), 1, "switching back to Markdown must remove old code");
}

#[test]
fn same_remote_branch_checkouts_do_not_share_writable_index() {
    let mock = Mock::new(true); let cache = tempfile::tempdir().unwrap();
    let first = tempfile::tempdir().unwrap(); let second = tempfile::tempdir().unwrap();
    for root in [first.path(), second.path()] {
        git(root, &["init", "-q", "-b", "main"]);
        git(root, &["remote", "add", "origin", "https://example.com/nova-test.git"]);
    }
    std::fs::write(first.path().join("first.py"), "def first(): pass\n").unwrap();
    std::fs::write(second.path().join("second.py"), "def second(): pass\n").unwrap();
    let mut a = Mcp::new(&mock, cache.path(), None, &[]);
    let mut b = Mcp::new(&mock, cache.path(), None, &[]);
    a.call(1, first.path(), false); a.success(1);
    b.call(1, second.path(), false); b.success(1);
    let uploads = mock.uploads(); assert_eq!(uploads.len(), 2);
    assert_ne!(uploads[0]["projectId"], uploads[1]["projectId"]);
    let legacy = nova_client::identity::repo_identity(first.path()).project_id;
    assert_eq!(uploads[0]["projectId"], legacy, "primary checkout must preserve historical ID");
    a.call(2, first.path(), false); let text = a.success(2);
    assert!(text.contains("first.py") && !text.contains("second.py"));
}

#[test]
fn normal_eof_cancels_background_work_and_releases_process_lock() {
    let mock = Mock::new(false); let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    let mut owner = Mcp::new(&mock, cache.path(), Some(repo.path()), &[]);
    owner.initialize(); owner.initialized(); wait(|| mock.count("/status/") > 0);
    owner.call(1, repo.path(), false);
    owner.input.take();
    let started = Instant::now();
    loop {
        if let Some(status) = owner.child.try_wait().unwrap() { assert!(status.success()); break; }
        assert!(started.elapsed() < Duration::from_secs(4), "EOF must not wait for the 8-second index deadline");
        thread::sleep(Duration::from_millis(10));
    }
    mock.state.ready.store(true, Ordering::SeqCst);
    let mut survivor = Mcp::new(&mock, cache.path(), None, &[]);
    survivor.call(1, repo.path(), false); survivor.success(1);
    assert_eq!(mock.count("batch-upload"), 1);
}

#[test]
fn isolated_checkout_ready_result_binds_final_device_route() {
    let mock = Mock::new(true); let cache = tempfile::tempdir().unwrap();
    let primary = tempfile::tempdir().unwrap(); let other = tempfile::tempdir().unwrap();
    for root in [primary.path(), other.path()] {
        git(root, &["init", "-q", "-b", "main"]);
        git(root, &["remote", "add", "origin", "https://example.com/device-route-test.git"]);
        std::fs::write(root.join("run.py"), "def run(): pass\n").unwrap();
    }
    let mut mcp = Mcp::new(&mock, cache.path(), None, &[]);
    mcp.call(1, primary.path(), false); mcp.success(1);
    for device in ["device-x", "device-y"] {
        let mut mcp = Mcp::new(&mock, cache.path(), None, &[("NOVA_DEVICE_ID", device)]);
        mcp.call(1, other.path(), false); mcp.success(1);
    }
    let uploads = mock.uploads(); assert_eq!(uploads.len(), 3);
    assert_ne!(uploads[1]["projectId"], uploads[2]["projectId"], "ready cache must not reuse an obsolete device route");
}

#[test]
fn worktree_branches_and_detached_head_keep_historical_identity_and_reuse_on_return() {
    let mock = Mock::new(true); let cache = tempfile::tempdir().unwrap();
    let main = tempfile::tempdir().unwrap(); let trees = tempfile::tempdir().unwrap();
    git(main.path(), &["init", "-q", "-b", "main"]);
    git(main.path(), &["remote", "add", "origin", "https://example.com/worktree-test.git"]);
    std::fs::write(main.path().join("run.py"), "def run(): pass\n").unwrap();
    git(main.path(), &["add", "."]);
    git(main.path(), &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"]);
    let a = trees.path().join("a"); let b = trees.path().join("b");
    git(main.path(), &["worktree", "add", "-q", "-b", "agent-a", a.to_str().unwrap()]);
    git(main.path(), &["worktree", "add", "-q", "-b", "agent-b", b.to_str().unwrap()]);
    let mut mcp = Mcp::new(&mock, cache.path(), None, &[]);
    for (id, root) in [(1, main.path()), (2, a.as_path()), (3, b.as_path())] {
        mcp.call(id, root, false); mcp.success(id);
    }
    let uploads = mock.uploads(); assert_eq!(uploads.len(), 3);
    for (upload, root) in uploads.iter().zip([main.path(), a.as_path(), b.as_path()]) {
        assert_eq!(upload["projectId"], nova_client::identity::repo_identity(root).project_id);
    }
    git(&a, &["checkout", "-q", "--detach"]);
    mcp.call(4, &a, false); mcp.success(4);
    assert_eq!(mock.uploads()[3]["projectId"], nova_client::identity::repo_identity(&a).project_id);
    git(&a, &["checkout", "-q", "agent-a"]);
    mcp.call(5, &a, false); mcp.success(5);
    assert_eq!(mock.count("batch-upload"), 4, "returning to an indexed branch must not reupload");
}

#[test]
fn partial_workspace_failure_is_visible_without_discarding_document_results() {
    let mock = Mock::new(true); let root = tempfile::tempdir().unwrap(); let cache = tempfile::tempdir().unwrap();
    let repo = root.path().join("unavailable"); std::fs::create_dir(&repo).unwrap(); git(&repo, &["init", "-q"]);
    std::fs::write(repo.join("run.py"), "def run(): pass\n").unwrap();
    std::fs::write(root.path().join("guide.md"), "# fallback guide\n").unwrap();
    *mock.state.unavailable_identity.lock().unwrap() = Some(nova_client::identity::repo_identity(&repo).identity_key);
    let mut mcp = Mcp::new(&mock, cache.path(), None, &[]);
    mcp.call(1, root.path(), false); let text = mcp.success(1);
    assert!(text.contains("覆盖缺口：Git unavailable") && text.contains("guide.md"));
}

#[test]
#[ignore = "local MCP readiness benchmark; run with --ignored --nocapture --test-threads=1"]
fn benchmark_mcp_readiness_while_indexing_is_blocked() {
    let mock = Mock::new(false); let repo = document_repo(); let cache = tempfile::tempdir().unwrap();
    git(repo.path(), &["init", "-q"]);
    let mut owner = Mcp::in_cwd(&mock, cache.path(), None, &[], repo.path());
    owner.initialize(); owner.initialized();
    wait(|| mock.count("/status/") > 0); // Actual in-flight indexing during every sample.
    let mut init = Vec::new(); let mut heartbeat = Vec::new();
    for _ in 0..20 {
        let started = Instant::now();
        let mut mcp = Mcp::in_cwd(&mock, cache.path(), None, &[], repo.path());
        mcp.initialize(); init.push(started.elapsed().as_secs_f64() * 1000.0);
        mcp.initialized();
        let started = Instant::now();
        mcp.send(json!({"jsonrpc":"2.0","id":1,"method":"ping"}));
        mcp.send(json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}));
        mcp.response(1, Duration::from_secs(1)); mcp.response(2, Duration::from_secs(1));
        heartbeat.push(started.elapsed().as_secs_f64() * 1000.0);
    }
    init.sort_by(f64::total_cmp); heartbeat.sort_by(f64::total_cmp);
    println!("MCP_READY samples=20 build=debug init_p50_ms={:.3} init_p95_ms={:.3} ping_and_tools_p50_ms={:.3} ping_and_tools_p95_ms={:.3}", init[10], init[18], heartbeat[10], heartbeat[18]);
    assert!(init[18] < 1000.0 && heartbeat[18] < 1000.0);
}
