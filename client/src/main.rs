//! `nova-client` 命令行入口（MCP stdio server；由编辑器作为子进程拉起）。
//!
//! ```bash
//! # 编辑器配置（Claude Code / Codex 等 stdio harness）：
//! NOVA_BASE_URL=http://127.0.0.1:8787 nova-client
//! # 云端：NOVA_BASE_URL=https://nova.example.com NOVA_API_TOKEN=<token>
//! ```

use clap::Parser;

/// nova MCP stdio 客户端（本地扫描/哈希/上传 → 远端 nova-service 检索）。
#[derive(Parser, Debug)]
#[command(name = "nova-client", version, about)]
struct Args {
    /// nova-service 基础地址。
    #[arg(long, env = "NOVA_BASE_URL", value_name = "URL")]
    base_url: String,

    /// 远端 API token（M2c 鉴权；当前服务端未启用，可省略）。
    #[arg(
        long,
        env = "NOVA_API_TOKEN",
        value_name = "TOKEN",
        hide_env_values = true
    )]
    token: Option<String>,

    /// 本地缓存根（默认 `~/.cache/nova`）。
    #[arg(long, env = "NOVA_CLIENT_CACHE", value_name = "DIR")]
    cache_root: Option<std::path::PathBuf>,
}

fn main() {
    let args = Args::parse();
    let cache_root = args
        .cache_root
        .unwrap_or_else(nova_client::default_cache_root);
    let runtime = tokio::runtime::Builder::new_multi_thread().enable_all().build()
        .expect("failed to start MCP runtime");
    let result = runtime.block_on(nova_client::run(&args.base_url, args.token, cache_root));
    // Dropped async workers cannot publish cache/index results; don't wait forever for a local
    // filesystem scan in spawn_blocking when the Agent has already disconnected.
    runtime.shutdown_timeout(std::time::Duration::from_secs(1));
    if let Err(error) = result {
        // stdout 只出 JSON-RPC 帧；诊断一律 stderr。
        eprintln!("nova-client: {error:#}");
        std::process::exit(1);
    }
}
