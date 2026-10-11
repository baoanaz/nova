//! A workspace can be one historical Git project or a container of Git projects and documents.
use std::path::{Path, PathBuf};

use anyhow::Result;

use crate::identity::repo_identity;
use crate::ignore::IgnoreRules;
use crate::policy::WorkspacePolicy;
use crate::remote::validate_project_root;

pub(crate) struct Part {
    pub root: PathBuf,
    pub label: String,
}

#[cfg(test)]
pub(crate) fn plan(project_root: &str, cache: &Path) -> Result<Vec<Part>> {
    plan_with_cancel(project_root, cache, &std::sync::atomic::AtomicBool::new(false))
}

pub(crate) fn plan_with_cancel(
    project_root: &str, cache: &Path, cancelled: &std::sync::atomic::AtomicBool,
) -> Result<Vec<Part>> {
    if cancelled.load(std::sync::atomic::Ordering::Acquire) { anyhow::bail!("工作区规划已取消"); }
    let root = std::fs::canonicalize(validate_project_root(project_root)?)?;
    let identity = repo_identity(&root);
    let policy = WorkspacePolicy::detect(&root, cache, identity.remote_url.is_some())?;
    if policy.kind != "directory" {
        return Ok(vec![Part { root, label: identity.display_name }]);
    }
    let rules = IgnoreRules::load(&root);
    let repositories = rules.repository_roots(cancelled)?;
    let mut parts = Vec::new();
    for repository in repositories {
        if cancelled.load(std::sync::atomic::Ordering::Acquire) { anyhow::bail!("工作区规划已取消"); }
        let repository = std::fs::canonicalize(repository)?;
        let identity = repo_identity(&repository);
        WorkspacePolicy::detect(&repository, cache, identity.remote_url.is_some())?;
        let relative = repository.strip_prefix(&root)?.to_string_lossy().replace('\\', "/");
        parts.push(Part { root: repository, label: format!("Git {relative} ({})", identity.display_name) });
    }
    parts.push(Part { root, label: "工作区仓库外文档".into() });
    Ok(parts)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::IndexManager;
    use crate::policy::FilePolicy;

    fn git_init(root: &Path) {
        std::fs::create_dir_all(root).unwrap();
        assert!(std::process::Command::new("git").arg("-C").arg(root)
            .args(["init", "-q"]).status().unwrap().success());
    }

    #[test]
    fn mixed_workspace_separates_git_and_hidden_documents() {
        let workspace = tempfile::tempdir().unwrap();
        let cache = tempfile::tempdir().unwrap();
        for name in ["repo-a", "nested/repo-b"] {
            let root = workspace.path().join(name);
            git_init(&root);
            std::fs::write(root.join("main.py"), "def run(): pass\n").unwrap();
            std::fs::write(root.join("README.md"), "# in Git\n").unwrap();
        }
        std::fs::create_dir(workspace.path().join(".agent")).unwrap();
        std::fs::write(workspace.path().join(".agent/guide.markdown"), "# agent guide\n").unwrap();
        std::fs::write(workspace.path().join("README.md"), "# workspace\n").unwrap();
        std::fs::write(workspace.path().join("secret.env"), "TOKEN=secret").unwrap();
        let parts = plan(workspace.path().to_str().unwrap(), cache.path()).unwrap();
        assert_eq!(parts.len(), 3);
        let manager = IndexManager::new(workspace.path().to_path_buf(), "docs".into(), cache.path().to_path_buf(), "http://test")
            .with_file_policy(FilePolicy::Markdown).excluding_repositories(true);
        let scan = manager.scan().unwrap();
        assert_eq!(scan.index.entries.keys().cloned().collect::<Vec<_>>(),
            vec![".agent/guide.markdown", "README.md"]);
    }

    #[test]
    fn git_root_keeps_original_single_project_scope() {
        let workspace = tempfile::tempdir().unwrap();
        let cache = tempfile::tempdir().unwrap();
        git_init(workspace.path());
        let parts = plan(workspace.path().to_str().unwrap(), cache.path()).unwrap();
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0].root, std::fs::canonicalize(workspace.path()).unwrap());
    }
}
