//! 手工核对工具：打印某个仓库的 D-29 身份（identityKey / projectId）。
//!
//! 用途：云端形态下"我的仓库会映射到服务端的哪个 projectId"是排查一切同步问题的第一步；
//! 同时也是**跨语言一致性**的手工对照入口（与
//! `uv run python -c "from nova_core.engine import repo_identity; ..."` 的输出比对）。
//!
//! ```bash
//! cargo run --example identity -- /path/to/repo
//! ```

fn main() {
    let Some(root) = std::env::args().nth(1) else {
        eprintln!("用法：cargo run --example identity -- <项目根绝对路径>");
        std::process::exit(2);
    };
    let identity = nova_client::identity::repo_identity(std::path::Path::new(&root));
    println!("path         = {root}");
    println!("display_name = {}", identity.display_name);
    println!("remote_url   = {:?}", identity.remote_url);
    println!("git_root     = {:?}", identity.git_root);
    println!("repo_path    = {:?}", identity.repo_path);
    println!("identityKey  = {}", identity.identity_key);
    println!("projectId    = {}", identity.project_id);
    println!("cache_dir    = ~/.cache/nova/{}", identity.project_id);
}
