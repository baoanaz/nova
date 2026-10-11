//! Automatic workspace selection. Capturing inputs is cheap; validation runs after the handshake.
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

use crate::policy::git;

#[derive(Clone)]
pub(crate) struct WorkspaceResolver {
    root: PathBuf,
    source: &'static str,
    home: Option<PathBuf>,
    excluded: Vec<PathBuf>,
}

#[derive(Debug)]
pub(crate) struct ResolvedWorkspace {
    pub root: PathBuf,
    pub source: &'static str,
}

impl WorkspaceResolver {
    pub fn from_environment() -> Option<Self> {
        let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")).map(PathBuf::from);
        let mut excluded = vec![crate::default_cache_root()];
        for name in ["CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "NPM_CONFIG_CACHE", "npm_config_cache", "CARGO_HOME", "RUSTUP_HOME"] {
            if let Some(path) = std::env::var_os(name).filter(|value| !value.is_empty()) {
                excluded.push(PathBuf::from(path));
            }
        }
        if let Some(home) = &home {
            excluded.extend([home.join(".codex"), home.join(".claude/plugins"), home.join(".pi/agent")]);
        }
        Self::select(
            std::env::var_os("NOVA_PROJECT_DIR").map(PathBuf::from),
            std::env::var_os("CLAUDE_PROJECT_DIR").map(PathBuf::from),
            std::env::current_dir().ok(), home, excluded,
        )
    }

    fn select(nova: Option<PathBuf>, claude: Option<PathBuf>, cwd: Option<PathBuf>, home: Option<PathBuf>, excluded: Vec<PathBuf>) -> Option<Self> {
        let (root, source) = if let Some(root) = nova {
            (root, "nova_project_dir")
        } else if let Some(root) = claude {
            (root, "claude_project_dir")
        } else {
            (cwd?, "cwd")
        };
        Some(Self { root, source, home, excluded })
    }

    pub fn explicit(root: PathBuf) -> Self {
        Self { root, source: "injected", home: None, excluded: Vec::new() }
    }

    pub fn resolve(self) -> Result<Option<ResolvedWorkspace>> {
        if !self.root.is_absolute() { bail!("自动预热工作区必须是绝对路径"); }
        let root = self.root.canonicalize().context("自动预热工作区不存在或不可访问")?;
        if !root.is_dir() { bail!("自动预热工作区不是目录"); }
        let home = self.home.and_then(|home| home.canonicalize().ok());
        check_location(&root, home.as_deref())?;
        if cache_path(&root) || self.excluded.iter().any(|excluded| {
            excluded.canonicalize().is_ok_and(|excluded| root.starts_with(excluded))
        }) {
            bail!("拒绝把 Agent/插件/依赖缓存目录作为自动预热工作区");
        }
        // Explicit scopes need no project marker, but still obey automatic-upload exclusions.
        // An invalid explicit path (including an empty value) never falls back to another scope.
        if self.source != "cwd" {
            return Ok(Some(ResolvedWorkspace { root, source: self.source }));
        }
        if let Some(top) = git(&root, &["rev-parse", "--show-toplevel"])? {
            let top = PathBuf::from(top).canonicalize()?;
            check_location(&top, home.as_deref())?;
            if cache_path(&top) { bail!("Git 根位于缓存目录，跳过自动预热"); }
            return Ok(Some(ResolvedWorkspace { root, source: "cwd_git" }));
        }
        let markers = ["AGENTS.md", "CLAUDE.md", "README.md", "README.markdown", "Cargo.toml", "package.json", "pyproject.toml", "go.mod", "CMakeLists.txt", ".nova-workspace"];
        if markers.iter().any(|name| root.join(name).symlink_metadata().is_ok_and(|metadata| metadata.is_file())) {
            return Ok(Some(ResolvedWorkspace { root, source: "cwd_project" }));
        }
        // A small, shallow probe recognizes mixed workspaces without scanning an untrusted tree.
        for entry in std::fs::read_dir(&root)?.take(64) {
            let entry = entry?;
            if !entry.file_type()?.is_dir() || cache_path(&entry.path()) { continue; }
            if entry.path().join(".git").symlink_metadata().is_ok()
                && git(&entry.path(), &["rev-parse", "--show-toplevel"])?.is_some() {
                return Ok(Some(ResolvedWorkspace { root, source: "cwd_git_container" }));
            }
        }
        Ok(None)
    }
}

fn check_location(root: &Path, home: Option<&Path>) -> Result<()> {
    if root.parent().is_none() || home == Some(root) {
        bail!("自动预热拒绝文件系统根或用户主目录");
    }
    #[cfg(unix)]
    if ["/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/proc", "/sys", "/dev", "/run", "/var/cache", "/var/lib", "/var/log"]
        .iter().any(|prefix| root.starts_with(prefix)) || root == Path::new("/var") || root == Path::new("/opt") {
        bail!("自动预热拒绝系统目录");
    }
    #[cfg(windows)]
    for name in ["SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"] {
        if std::env::var_os(name).is_some_and(|path| root.starts_with(PathBuf::from(path))) {
            bail!("自动预热拒绝系统目录");
        }
    }
    Ok(())
}

fn cache_path(root: &Path) -> bool {
    let parts: Vec<_> = root.components().map(|part| part.as_os_str().to_string_lossy().to_ascii_lowercase()).collect();
    parts.iter().any(|part| matches!(part.as_str(), ".cache" | ".npm" | ".npx" | ".cargo" | ".rustup" | ".codex" | "node_modules"))
        || parts.windows(2).any(|pair| matches!((pair[0].as_str(), pair[1].as_str()),
            (".claude", "plugins") | (".pi", "agent") | ("plugins", "cache")))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cwd(root: &Path) -> WorkspaceResolver {
        WorkspaceResolver::select(None, None, Some(root.to_path_buf()), None, Vec::new()).unwrap()
    }
    fn init_git(root: &Path) {
        std::fs::create_dir_all(root).unwrap();
        assert!(std::process::Command::new("git").args(["init", "-q"]).arg(root).status().unwrap().success());
    }

    #[test]
    fn explicit_priority_and_invalid_explicit_does_not_fall_back() {
        let nova = tempfile::tempdir().unwrap();
        let claude = tempfile::tempdir().unwrap();
        let resolver = WorkspaceResolver::select(Some(nova.path().into()), Some(claude.path().into()), Some(claude.path().into()), None, vec![]).unwrap();
        let resolved = resolver.resolve().unwrap().unwrap();
        assert_eq!(resolved.root, nova.path().canonicalize().unwrap());
        assert_eq!(resolved.source, "nova_project_dir");
        let resolver = WorkspaceResolver::select(Some(nova.path().join("missing")), Some(claude.path().into()), Some(claude.path().into()), None, vec![]).unwrap();
        assert!(resolver.resolve().is_err());
        let resolver = WorkspaceResolver::select(Some(PathBuf::new()), Some(claude.path().into()), Some(claude.path().into()), None, vec![]).unwrap();
        assert!(resolver.resolve().is_err());
    }

    #[test]
    fn claude_path_remains_supported() {
        let root = tempfile::tempdir().unwrap();
        let resolver = WorkspaceResolver::select(None, Some(root.path().into()), None, None, vec![]).unwrap();
        assert_eq!(resolver.resolve().unwrap().unwrap().source, "claude_project_dir");
    }

    #[test]
    fn git_init_cwd_is_verified_without_remote() {
        let root = tempfile::tempdir().unwrap();
        init_git(root.path());
        assert_eq!(cwd(root.path()).resolve().unwrap().unwrap().source, "cwd_git");
    }

    #[test]
    fn documentation_marker_is_required_for_unversioned_cwd() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("note.md"), "private note").unwrap();
        assert!(cwd(root.path()).resolve().unwrap().is_none());
        std::fs::write(root.path().join("README.md"), "project docs").unwrap();
        assert_eq!(cwd(root.path()).resolve().unwrap().unwrap().source, "cwd_project");
    }

    #[test]
    fn mixed_workspace_is_recognized_shallowly() {
        let root = tempfile::tempdir().unwrap();
        init_git(&root.path().join("repository"));
        assert_eq!(cwd(root.path()).resolve().unwrap().unwrap().source, "cwd_git_container");
    }

    #[test]
    fn home_and_root_are_rejected_even_when_git_exists() {
        let root = tempfile::tempdir().unwrap();
        init_git(root.path());
        let mut resolver = cwd(root.path());
        resolver.home = Some(root.path().into());
        assert!(resolver.resolve().is_err());
        #[cfg(unix)]
        assert!(cwd(Path::new("/")).resolve().is_err());
    }

    #[test]
    fn plugin_and_dependency_caches_are_rejected_even_when_git_exists() {
        let root = tempfile::tempdir().unwrap();
        for relative in ["node_modules/plugin", ".claude/plugins/cache/plugin", ".codex/plugins/plugin", ".pi/agent/packages/plugin"] {
            let path = root.path().join(relative);
            init_git(&path);
            assert!(cwd(&path).resolve().is_err(), "{relative}");
            assert!(WorkspaceResolver::explicit(path).resolve().is_err(), "explicit {relative}");
        }
    }

    #[test]
    fn custom_cache_root_is_not_a_workspace() {
        let root = tempfile::tempdir().unwrap();
        init_git(root.path());
        let mut resolver = cwd(root.path());
        resolver.excluded.push(root.path().into());
        assert!(resolver.resolve().is_err());
    }
}
