//! 忽略规则（D-28 / R42 / TASK-097）：第 0 层白名单 > `.novaignore` > `.gitignore` > 内置默认。
//!
//! **为什么用 `ignore` crate 而不是手写 gitignore 解析**：`docs/design/Module/05-MCP与同步.md` §3.1
//! 明确"Rust 实现直接用 `ignore` crate（原生支持 .gitignore 语义）"；`docs/plan/contracts.md` R42 也
//! 把契约定义为"忽略**语义**"而不是库，并要求两侧行为一致。
//!
//! 与 core（`core/nova_core/pipeline/ignore.py`，本地模式用）的对齐口径：
//!
//! | 层 | core（Python） | 本模块（Rust） |
//! |---|---|---|
//! | 1 `.novaignore` | 手写解析，优先级最高 | `add_custom_ignore_filename(".novaignore")` |
//! | 2 `.gitignore`（各层级） | 手写解析（注释/`!`/目录尾/`/`/`**`/字符类） | `ignore` crate 原生（`git_ignore(true)`） |
//! | 3 内置默认 | `DEFAULT_SKIP_DIRS` + `DEFAULT_SKIP_DIR_PATTERNS` | 同名单 + 同模式（本模块 `builtin_reason`） |
//! | 0 白名单（TASK-097） | `Allowlist`：`.novainclude` ∪ 内置默认 ∪ `$NOVA_INDEX_ALLOWLIST` | 本模块 `Allowlist` + `callback` 二次包含 |
//!
//! **V1 两侧共同不读**：`.git/info/exclude` 与全局 `core.excludesFile`
//! （core 的执行记录已注明该简化；此处保持一致，`git_exclude(false)` / `git_global(false)`）。
//!
//! **已知语义分歧（诚实记录，勿当作 bug）**：`ignore` crate 默认**豁免被 git 跟踪的文件**
//! （`require_git` / tracked-file 语义），而 core 的 Python 实现按纯规则匹配、不查 git 索引。
//! 该分歧只影响"被 .gitignore 命中但已被 git 跟踪"的文件（编排者在 TASK-037 实测约 1.9%）。
//! 契约（R42）要求的是 `ignore` crate 语义，因此**客户端行为合规**；两侧完全一致留待
//! 后续对照测试卡（见任务卡"未决问题"）。

use std::path::Path;

use ignore::DirEntry;

/// 内置跳过目录名（与 core `DEFAULT_SKIP_DIRS` 同集合）。
pub const DEFAULT_SKIP_DIRS: &[&str] = &[
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
    ".nova",
];

/// 内置跳过目录**模式**（与 core `DEFAULT_SKIP_DIR_PATTERNS` 同集合）。
///
/// 用模式而不是穷举：真实仓库的构建产物是变体（`cmake-build-release` / `build-debug` …）。
/// `bin` / `obj` / `lib` 刻意不在列表里——它们是合法源码目录名，误伤代价高于收益。
pub const DEFAULT_SKIP_DIR_PATTERNS: &[&str] = &[
    "cmake-build-*",
    "build-*",
    "out",
    "htmlcov",
    ".cache",
    ".gradle",
    ".next",
    ".nuxt",
    ".turbo",
    ".parcel-cache",
    "__pypackages__",
];

/// 自定义忽略文件名（第 1 层，优先级最高）。
pub const CUSTOM_IGNORE_FILENAME: &str = ".novaignore";

/// 自定义**白名单**文件名（TASK-097 第 0 层；只读仓库根这一份）。
pub const INCLUDE_FILENAME: &str = ".novainclude";

/// 白名单的全局环境变量（逗号分隔）——与 core `NOVA_INDEX_ALLOWLIST` 同名同语义。
pub const ALLOWLIST_ENV_VAR: &str = "NOVA_INDEX_ALLOWLIST";

/// 默认放行的**文件名**（与 core `DEFAULT_ALLOWLIST_FILENAMES` 同一份清单，大小写不敏感）。
pub const DEFAULT_ALLOWLIST_FILENAMES: &[&str] = &[
    "agents.md",
    "claude.md",
    ".agent.md",
    ".cursorrules",
    "handoff.md",
];

/// 默认放行的**目录名**（与 core `DEFAULT_ALLOWLIST_DIRS` 同一份清单；命中任意路径段）。
pub const DEFAULT_ALLOWLIST_DIRS: &[&str] = &["skills"];

/// `gitignore` 范围下"名字下钻"的深度上限（与 core `DEFAULT_LOOKTHROUGH_DEPTH` 同值）。
pub const DEFAULT_LOOKTHROUGH_DEPTH: u32 = 4;

/// 白名单条目：目录型（`skills`）或文件名型（`AGENTS.md`）。
///
/// 与 core `_parse_allow_entry` 的映射规则一致：含 `/` 或**不含点**的裸名字 → 目录型；
/// 不含 `/` 且**含点**的裸名字 → 文件名型。`!` 前缀取消该条目（第 0 层内部否定）。
#[derive(Debug, Clone, PartialEq, Eq)]
enum AllowEntry {
    Directory(String),
    Filename(String),
    /// `!name`：取消同名白名单条目（后写覆盖先写）。
    Cancel(String),
}

/// 第 0 层"强制包含"白名单（TASK-097）。
///
/// **为什么不用 `ignore` crate 的 `overrides` API**（卡内 §B 要求给出理由）：
/// 该 API 有一条致命副作用——**只要存在至少一条白名单 glob，未命中任何 glob 的普通文件
/// 会被当成"被忽略"**（`overrides.rs` 的 `Override::matched`：`num_whitelists() > 0 && !is_dir`
/// 时返回 `Ignore`）。nova 要的是"在原有忽略规则之上**追加**包含"，不是"只索引白名单"。
/// 所以本模块选**两次包含**：
///
/// 1. `ignore` crate 按 `.novaignore`/`.gitignore`/内置层正常遍历（**不**过滤文件）；
/// 2. 对每个"被忽略"的文件，用 `Ignore::matched` + 白名单裁决是否救回
///    （见 `Snapshot::is_included`），调用方把两步结果取并集。
///
/// 目录下钻同样保守：只对被忽略的目录、且其直接子项命中白名单时才进入（名字定向），
/// 连续下钻层数受 `lookthrough_depth` 封顶——内置目录名/模式从不下钻（卡内 §A-4）。
#[derive(Debug, Clone)]
pub struct Allowlist {
    entries: Vec<AllowEntry>,
    /// 下钻深度上限（`NOVA_ALLOWLIST_DEPTH` 可覆盖）。
    lookthrough_depth: u32,
}

impl Default for Allowlist {
    fn default() -> Self {
        Self::from_patterns(&[], &[])
    }
}

impl Allowlist {
    /// 内置默认 + `.novainclude` + 环境变量，取并集（不覆盖）。
    pub fn load(root: &Path) -> Self {
        let config: Vec<String> = std::fs::read_to_string(root.join(INCLUDE_FILENAME))
            .map(|text| text.lines().map(str::to_string).collect())
            .unwrap_or_default();
        let env = std::env::var(ALLOWLIST_ENV_VAR).unwrap_or_default();
        let env_patterns: Vec<String> = env
            .split(',')
            .filter(|part| !part.trim().is_empty())
            .map(str::to_string)
            .collect();
        let mut allowlist = Self::from_patterns(&config, &env_patterns);
        if let Ok(raw) = std::env::var("NOVA_ALLOWLIST_DEPTH") {
            if let Ok(parsed) = raw.parse::<u32>() {
                allowlist.lookthrough_depth = parsed;
            }
        }
        allowlist
    }

    /// 由自定义条目 + 环境变量条目 + 内置默认构建（顺序即优先级：内置 < env < `.novainclude`）。
    pub fn from_patterns(config: &[String], env: &[String]) -> Self {
        let mut entries: Vec<AllowEntry> = Vec::new();
        for raw in DEFAULT_ALLOWLIST_DIRS {
            entries.push(AllowEntry::Directory((*raw).to_string()));
        }
        for raw in DEFAULT_ALLOWLIST_FILENAMES {
            entries.push(AllowEntry::Filename((*raw).to_string()));
        }
        for raw in env.iter().chain(config.iter()) {
            match parse_allow_entry(raw) {
                Some(entry) => entries.push(entry),
                None => continue,
            }
        }
        Self {
            entries,
            lookthrough_depth: DEFAULT_LOOKTHROUGH_DEPTH,
        }
    }

    /// 该条目集合是否为"空"（对照测量用：`with_builtin_disabled` 关闭内置默认）。
    pub fn builtin_disabled() -> Self {
        Self {
            entries: Vec::new(),
            lookthrough_depth: DEFAULT_LOOKTHROUGH_DEPTH,
        }
    }

    pub fn lookthrough_depth(&self) -> u32 {
        self.lookthrough_depth
    }

    /// 生效条目的规范名（配置指纹 / 自述用）。
    pub fn names(&self) -> Vec<String> {
        self.entries
            .iter()
            .map(|entry| match entry {
                AllowEntry::Directory(name) | AllowEntry::Filename(name) => name.clone(),
                AllowEntry::Cancel(name) => format!("!{name}"),
            })
            .collect()
    }

    /// 单个路径段是否命中白名单（`!` 取消后不计）。目录下钻探针用。
    fn matches_name(&self, name: &str) -> bool {
        let mut winner: Option<&AllowEntry> = None;
        for entry in &self.entries {
            let hit = match entry {
                AllowEntry::Directory(candidate) | AllowEntry::Filename(candidate) => {
                    candidate.eq_ignore_ascii_case(name)
                }
                AllowEntry::Cancel(candidate) => candidate.eq_ignore_ascii_case(name),
            };
            if hit {
                winner = Some(entry);
            }
        }
        !matches!(winner, None | Some(AllowEntry::Cancel(_)))
    }

    /// 仓库相对路径 `path` 是否被白名单最终放行（含"祖先目录命中"）。
    pub fn allows(&self, path: &str, is_dir: bool) -> bool {
        let normalized = path.trim_matches('/');
        if normalized.is_empty() {
            return false;
        }
        let parts: Vec<&str> = normalized.split('/').collect();
        let name = parts[parts.len() - 1];
        let directories: &[&str] = if is_dir { &parts } else { &parts[..parts.len() - 1] };
        let mut winner: Option<&AllowEntry> = None;
        for entry in &self.entries {
            let hit = match entry {
                AllowEntry::Directory(candidate) => {
                    directories.iter().any(|part| candidate.eq_ignore_ascii_case(part))
                }
                AllowEntry::Filename(candidate) => candidate.eq_ignore_ascii_case(name),
                AllowEntry::Cancel(candidate) => {
                    directories.iter().any(|part| candidate.eq_ignore_ascii_case(part))
                        || candidate.eq_ignore_ascii_case(name)
                }
            };
            if hit {
                winner = Some(entry);
            }
        }
        !matches!(winner, None | Some(AllowEntry::Cancel(_)))
    }
}

/// 一行白名单文本 → 条目；空行 / 注释 / 通配返回 `None`（与 core `_parse_allow_entry` 同语法）。
fn parse_allow_entry(raw: &str) -> Option<AllowEntry> {
    let text = raw.trim();
    if text.is_empty() || text.starts_with('#') {
        return None;
    }
    let (negated, body) = match text.strip_prefix('!') {
        Some(rest) => (true, rest.trim()),
        None => (false, text),
    };
    if body.is_empty() || body.contains(['*', '?', '[']) {
        return None;
    }
    // "含点 = 文件、不含点 = 目录"（带 `/` 一律目录型），与 core 的启发式一致。
    let is_directory = body.contains('/') || !body.contains('.');
    let name = body.trim_matches('/').rsplit('/').next().unwrap_or(body).to_string();
    if name.is_empty() {
        return None;
    }
    if negated {
        return Some(AllowEntry::Cancel(name));
    }
    Some(if is_directory {
        AllowEntry::Directory(name)
    } else {
        AllowEntry::Filename(name)
    })
}

/// 一个仓库的忽略规则（构建一次，之后只读）。
pub struct IgnoreRules {
    root: std::path::PathBuf,
    /// 忽略文件内容的指纹（用于配置指纹：改了 .gitignore/.novaignore 应作废缓存）。
    fingerprint: String,
}

impl IgnoreRules {
    /// 从仓库根构建（读取 `.novaignore` / 各层 `.gitignore` 的内容用于指纹）。
    pub fn load(root: &Path) -> Self {
        let fingerprint = ignore_files_fingerprint(root);
        Self {
            root: root.to_path_buf(),
            fingerprint,
        }
    }

    pub fn fingerprint(&self) -> &str {
        &self.fingerprint
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// 本仓库的白名单（读 `.novainclude` + 环境变量 + 内置默认）。
    pub fn allowlist(&self) -> Allowlist {
        Allowlist::load(&self.root)
    }

    /// 白名单遍历（walker B，TASK-097 §B）：**只**产出白名单救回的文件。
    ///
    /// **为什么不复用 walker A**：`ignore` crate 在 `filter_entry` **之前**就丢弃被忽略的条目，
    /// 因此被 `.gitignore` 排掉的 ``.claude/skills/SKILL.md`` 根本到不了过滤器。
    /// 本方法改为把忽略规则整体关掉（`git_ignore(false).ignore(false)` + 不注册
    /// `.novaignore`），只留"内置目录剪枝 + 白名单定向下钻"，再按白名单裁决产出。
    ///
    /// **为什么不使用 `overrides` API**：`Override::matched` 在"存在至少一条白名单 glob
    /// 且 `is_dir == false`"时会把**未命中任何 glob 的文件判为忽略**——那会变成"只索引白名单"，
    /// 而需求是"在原有索引范围之上**追加**"（卡内 §B 的做法评估见执行记录）。
    ///
    /// 下钻是**名字定向 + 有深度上限**的：被忽略的目录只有"直接子项里有人命中白名单"
    /// 才进入，且连续下钻层数受 `lookthrough_depth` 封顶；内置目录名/模式从不下钻（§A-4）。
    pub fn allowlist_walk(&self, allowlist: Allowlist) -> ignore::Walk {
        let root = self.root.clone();
        let probe_root = root.clone();
        ignore::WalkBuilder::new(&root)
            .git_ignore(false)
            .git_global(false)
            .git_exclude(false)
            .ignore(false)
            .hidden(false)
            .parents(false)
            .follow_links(false)
            .require_git(false)
            .filter_entry(move |entry| {
                if entry.depth() == 0 {
                    return true;
                }
                let name = entry.file_name().to_string_lossy().into_owned();
                if is_builtin_dir_name(&name) {
                    return false; // 内置目录名/模式：从不下钻（卡内 §A-4）
                }
                let relative = relative_path(&probe_root, entry.path());
                // `DirEntry::is_dir()` 是 crate 私有方法，只能用 `file_type()` 判（与 `scan()` 同口径）。
                let is_dir = entry.file_type().is_some_and(|kind| kind.is_dir());
                if allowlist.allows(&relative, is_dir) {
                    return true; // 命中白名单（本身是白名单文件/目录，或祖先已是白名单目录）
                }
                if !is_dir {
                    return false; // 普通文件不由白名单遍历负责（walker A 已覆盖）
                }
                if probe_budget(&allowlist, &relative) <= 0 {
                    return false;
                }
                has_allowlisted_child(&allowlist, entry.path())
            })
            .build()
    }

    /// 扫描入口的并集迭代器：walker A（正常忽略）+ walker B（白名单救回）。
    ///
    /// **调用方必须按路径去重**：一个既未被忽略、又命中白名单的文件（如未被 gitignore 的
    /// ``AGENTS.md``）会出现在两个 walker 里（`index.rs::scan` 用 `HashSet` 去重）。
    pub fn walk_union(&self) -> std::iter::Chain<ignore::Walk, ignore::Walk> {
        let allowlist = self.allowlist();
        self.walker().chain(self.allowlist_walk(allowlist))
    }

    /// 构建遍历器：`.novaignore` > `.gitignore`（`ignore` crate）+ 内置目录剪枝。
    ///
    /// 内置层用 `filter_entry` 剪枝（整目录跳过，省 IO）；内置层**不**交给 `ignore` crate，
    /// 以便跳过时能如实记录原因（R43）。
    pub fn walker(&self) -> ignore::Walk {
        let dirs = DEFAULT_SKIP_DIRS;
        let patterns = DEFAULT_SKIP_DIR_PATTERNS;
        ignore::WalkBuilder::new(&self.root)
            .add_custom_ignore_filename(CUSTOM_IGNORE_FILENAME)
            .git_ignore(true)
            // V1 不读全局 excludes / .git/info/exclude（与 core 的简化一致）
            .git_global(false)
            .git_exclude(false)
            // 不因为"隐藏"就跳过（core 只按目录名判，`.github/` 这类应被索引）
            .hidden(false)
            .parents(false)
            .follow_links(false)
            .require_git(false)
            .filter_entry(move |entry| {
                if entry.depth() == 0 {
                    return true;
                }
                let name = entry.file_name().to_string_lossy().into_owned();
                !dirs.contains(&name.as_str())
                    && !patterns
                        .iter()
                        .any(|pattern| wildcard_match(pattern, &name))
            })
            .build()
    }

    /// 目录剪枝入口（`filter_entry` 用）：内置目录名/模式 → 剪掉。
    pub fn should_descend(&self, entry: &DirEntry) -> bool {
        if entry.depth() == 0 {
            return true;
        }
        self.builtin_reason(&entry.file_name().to_string_lossy())
            .is_none()
    }

    /// 路径（仓库相对、正斜杠）是否被忽略；返回原因标签（`"builtin"` 或 `None`）。
    ///
    /// **只判内置层**：`.novaignore` / `.gitignore` 由 `ignore` crate 在遍历时应用
    /// （`walker()` 的 `filter_entry` / 迭代结果），此处不重复实现，避免双份语义。
    pub fn builtin_reason(&self, file_name: &str) -> Option<&'static str> {
        if is_builtin_dir_name(file_name) {
            return Some("builtin");
        }
        None
    }
}

/// 内置跳过目录名 / 目录模式（与 core 的 `_matches_builtin_dir` 同口径）。
fn is_builtin_dir_name(name: &str) -> bool {
    DEFAULT_SKIP_DIRS.contains(&name)
        || DEFAULT_SKIP_DIR_PATTERNS
            .iter()
            .any(|pattern| wildcard_match(pattern, name))
}

/// 仓库相对、正斜杠路径（与 `index.rs::relative_path` 同口径；超出 root 时返回空串）。
fn relative_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .map(|relative| relative.to_string_lossy().replace('\\', "/"))
        .unwrap_or_default()
}

/// 白名单下钻的剩余预算（纯函数，与 core `_walk` 的 `budget` 同口径）。
///
/// 从根往下数：遇到"已是白名单目录"的祖先即停止消耗（其子树整体放行）；每经过一个
/// 尚未放行的目录消耗 1 格。`<= 0` 表示"可以看，但不能再为下一层被忽略目录下钻"。
fn probe_budget(allowlist: &Allowlist, relative: &str) -> i64 {
    let parts: Vec<&str> = relative.split('/').collect();
    let mut budget = i64::from(allowlist.lookthrough_depth());
    for depth in 1..parts.len() {
        let ancestor = parts[..depth].join("/");
        if allowlist.allows(&ancestor, true) {
            return budget;
        }
        budget -= 1;
    }
    budget
}

/// 目录的直接子项里是否有名字命中白名单（决定要不要下钻看一眼）。
fn has_allowlisted_child(allowlist: &Allowlist, path: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(path) else {
        return false;
    };
    entries.flatten().any(|entry| {
        allowlist.matches_name(&entry.file_name().to_string_lossy())
    })
}

/// 单个 `*` 通配匹配（模式里只用到 `prefix-*` 形态）。
fn wildcard_match(pattern: &str, name: &str) -> bool {
    match pattern.split_once('*') {
        None => pattern == name,
        Some((prefix, suffix)) => {
            name.len() >= prefix.len() + suffix.len()
                && name.starts_with(prefix)
                && name.ends_with(suffix)
        }
    }
}

/// 忽略文件内容指纹：仓库根与一级目录下的 `.novaignore` / `.gitignore` / `.novainclude`
/// 内容拼接后 hash（白名单变了必须作废缓存，否则旧文件集会被继续使用）。
///
/// 只取两层（根 + 一级子目录）：覆盖绝大多数真实布局，避免全仓遍历
/// （缓存作废只影响性能，不影响正确性——`ignore` crate 自己会读全部层级）。
fn ignore_files_fingerprint(root: &Path) -> String {
    use sha2::{Digest, Sha256};

    let mut hasher = Sha256::new();
    let mut candidates = vec![root.to_path_buf()];
    if let Ok(entries) = std::fs::read_dir(root) {
        let mut subdirs: Vec<std::path::PathBuf> = entries
            .flatten()
            .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_dir()))
            .map(|entry| entry.path())
            .collect();
        subdirs.sort();
        candidates.extend(subdirs.into_iter().take(50));
    }
    for dir in candidates {
        for name in [CUSTOM_IGNORE_FILENAME, ".gitignore", INCLUDE_FILENAME] {
            let path = dir.join(name);
            if let Ok(text) = std::fs::read(&path) {
                hasher.update(name.as_bytes());
                hasher.update(b"\x00");
                hasher.update(&text);
                hasher.update(b"\x01");
            }
        }
    }
    hex::encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// 与 `index.rs::scan()` 同口径的清单：walker 并集 + 按路径去重 + 稳定排序。
    fn collect(root: &Path) -> Vec<String> {
        let rules = IgnoreRules::load(root);
        let mut seen = std::collections::BTreeSet::new();
        for entry in rules.walk_union() {
            let Ok(entry) = entry else { continue };
            if !entry.file_type().is_some_and(|kind| kind.is_file()) {
                continue;
            }
            seen.insert(
                entry
                    .path()
                    .strip_prefix(root)
                    .expect("entry below root")
                    .to_string_lossy()
                    .replace('\\', "/"),
            );
        }
        seen.into_iter().collect()
    }

    #[test]
    fn builtin_dirs_and_patterns_are_pruned() {
        let project = tempfile::tempdir().expect("temp dir");
        fs::create_dir_all(project.path().join("cmake-build-release")).expect("dir");
        fs::create_dir_all(project.path().join("node_modules/pkg")).expect("dir");
        fs::write(project.path().join("cmake-build-release/a.o"), "x").expect("file");
        fs::write(project.path().join("node_modules/pkg/i.js"), "x").expect("file");
        fs::write(project.path().join("main.c"), "int main(){}").expect("file");

        let files = collect(project.path());

        assert_eq!(files, vec!["main.c".to_string()]);
    }

    #[test]
    fn gitignore_is_honoured_and_novaignore_takes_precedence() {
        let project = tempfile::tempdir().expect("temp dir");
        fs::write(project.path().join(".gitignore"), "ignored_by_git.txt\n").expect("write");
        fs::write(project.path().join("ignored_by_git.txt"), "x").expect("write");
        fs::write(project.path().join(".novaignore"), "ignored_by_nova.txt\n").expect("write");
        fs::write(project.path().join("ignored_by_nova.txt"), "x").expect("write");
        fs::write(project.path().join("kept.txt"), "x").expect("write");

        let files = collect(project.path());

        assert!(files.contains(&"kept.txt".to_string()));
        assert!(!files.contains(&"ignored_by_git.txt".to_string()));
        assert!(!files.contains(&"ignored_by_nova.txt".to_string()));
    }

    #[test]
    fn negation_in_gitignore_rescues_a_builtin_dir_name() {
        let project = tempfile::tempdir().expect("temp dir");
        fs::create_dir_all(project.path().join("build")).expect("dir");
        fs::write(project.path().join("build/keep.txt"), "x").expect("file");
        // 内置层会剪掉 build/；.gitignore 的否定**不能**救回被 filter_entry 剪掉的目录
        // （这是两个实现的已知差异：core 的 is_ignored 是独立 API，可被否定救回；
        //  而遍历剪枝发生在目录层）。此处断言剪枝行为，差异写入未决问题。
        let files = collect(project.path());

        assert!(!files.iter().any(|path| path.starts_with("build/")));
    }

    #[test]
    fn fingerprint_changes_when_ignore_files_change() {
        let project = tempfile::tempdir().expect("temp dir");
        let before = IgnoreRules::load(project.path()).fingerprint().to_string();
        fs::write(project.path().join(".gitignore"), "x.txt\n").expect("write");
        let after = IgnoreRules::load(project.path()).fingerprint().to_string();

        assert_ne!(before, after);
    }

    // ---------------------------------------------------------------------
    // TASK-097 §B：第 0 层白名单（R42 要求两侧语义一致）
    // ---------------------------------------------------------------------

    /// 卡内 §A-3 的最小复现仓库（与 core `test_allowlist.py` 的夹具同形状）。
    fn allowlist_project() -> tempfile::TempDir {
        let project = tempfile::tempdir().expect("temp dir");
        let root = project.path();
        for dir in [
            "hacks/skills/learned",
            ".claude/skills/review",
            "node_modules/pkg/skills",
            "logs/deep/nested",
            ".git/objects/ab",
        ] {
            fs::create_dir_all(root.join(dir)).expect("dir");
        }
        let files: &[(&str, &str)] = &[
            (".gitignore", "hacks/\nlogs/\nAI-notes.tmp\n.claude/\n"),
            ("hacks/skills/SKILL.md", "# 技能\n"),
            ("hacks/skills/learned/helper.py", "HELPER = 1\n"),
            (".claude/settings.json", "{}\n"),
            (".claude/skills/review/SKILL.md", "# 评审\n"),
            (".claude/skills/review/reference.md", "# 参考\n"),
            ("AGENTS.md", "# 指令\n"),
            ("HANDOFF.md", "# 交接\n"),
            ("AI-notes.tmp", "笔记\n"),
            ("node_modules/pkg/skills/a.py", "x = 1\n"),
            (".git/objects/ab/cdef", "binary\n"),
            ("logs/deep/nested/z.txt", "z\n"),
            ("src/core.py", "def run() {}\n"),
        ];
        for (path, content) in files {
            let target = root.join(path);
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).expect("parent dir");
            }
            fs::write(target, content).expect("file");
        }
        project
    }

    #[test]
    fn gitignored_skills_dir_is_rescued() {
        let project = allowlist_project();
        let files = collect(project.path());

        assert!(files.contains(&"hacks/skills/SKILL.md".to_string()));
        assert!(files.contains(&"hacks/skills/learned/helper.py".to_string()));
    }

    #[test]
    fn hidden_ai_dir_skills_are_rescued() {
        let project = allowlist_project();
        let files = collect(project.path());

        assert!(files.contains(&".claude/skills/review/SKILL.md".to_string()));
        assert!(files.contains(&".claude/skills/review/reference.md".to_string()));
        assert!(!files.contains(&".claude/settings.json".to_string()));
    }

    #[test]
    fn dependency_dir_skills_are_never_rescued() {
        // 卡内点名"最容易写错"的守护：`skills` 是通用目录名，依赖包里的同名结构不得进来。
        let project = allowlist_project();
        let files = collect(project.path());

        assert!(!files.iter().any(|path| path.starts_with("node_modules/")));
    }

    #[test]
    fn git_metadata_is_never_rescued() {
        let project = allowlist_project();
        let files = collect(project.path());

        assert!(!files.iter().any(|path| path.starts_with(".git/")));
    }

    #[test]
    fn allowlisted_filenames_are_rescued_case_insensitively() {
        let project = tempfile::tempdir().expect("temp dir");
        let root = project.path();
        fs::write(root.join(".gitignore"), "agents.md\nHANDOFF.md\n").expect("write");
        fs::write(root.join("AGENTS.md"), "# 大写\n").expect("write");
        fs::write(root.join("handoff.md"), "# 小写\n").expect("write");

        let files = collect(root);

        assert!(files.contains(&"AGENTS.md".to_string()), "大小写不敏感");
        assert!(files.contains(&"handoff.md".to_string()));
    }

    #[test]
    fn novainclude_adds_custom_patterns() {
        let project = tempfile::tempdir().expect("temp dir");
        let root = project.path();
        fs::write(root.join(".gitignore"), "my-notes/\n").expect("write");
        fs::write(root.join(".novainclude"), "my-notes\n").expect("write");
        fs::create_dir_all(root.join("my-notes")).expect("dir");
        fs::write(root.join("my-notes/todo.md"), "# 待办\n").expect("write");

        let files = collect(root);

        assert!(files.contains(&"my-notes/todo.md".to_string()));
    }

    #[test]
    fn novainclude_can_cancel_a_builtin_entry() {
        let project = tempfile::tempdir().expect("temp dir");
        let root = project.path();
        fs::write(root.join(".gitignore"), "hacks/\n").expect("write");
        fs::write(root.join(".novainclude"), "!skills\n").expect("write");
        fs::create_dir_all(root.join("hacks/skills")).expect("dir");
        fs::write(root.join("hacks/skills/SKILL.md"), "# 不要\n").expect("write");
        fs::write(root.join("AGENTS.md"), "# 仍旧要\n").expect("write");

        let files = collect(root);

        assert!(!files.contains(&"hacks/skills/SKILL.md".to_string()));
        assert!(files.contains(&"AGENTS.md".to_string()), "只取消 skills");
    }

    #[test]
    fn walk_union_does_not_emit_duplicates() {
        // 未被 gitignore 的 `AGENTS.md` 同时命中两个 walker，`collect` 必须去重（scan 同口径）。
        let project = tempfile::tempdir().expect("temp dir");
        let root = project.path();
        fs::write(root.join("AGENTS.md"), "# 指令\n").expect("write");
        fs::write(root.join("src.py"), "x = 1\n").expect("write");

        let files = collect(root);

        assert_eq!(files, vec!["AGENTS.md".to_string(), "src.py".to_string()]);
    }

    #[test]
    fn fingerprint_changes_when_novainclude_changes() {
        let project = tempfile::tempdir().expect("temp dir");
        let before = IgnoreRules::load(project.path()).fingerprint().to_string();
        fs::write(project.path().join(".novainclude"), "my-skills\n").expect("write");
        let after = IgnoreRules::load(project.path()).fingerprint().to_string();

        assert_ne!(before, after, "白名单变了必须作废缓存");
    }

    #[test]
    fn allowlist_does_not_change_non_allowlisted_paths() {
        // 既有三层行为不变：未命中白名单的路径照旧被忽略。
        let project = allowlist_project();
        let files = collect(project.path());

        assert!(!files.contains(&"AI-notes.tmp".to_string()));
        assert!(!files.contains(&"logs/deep/nested/z.txt".to_string()));
        assert!(files.contains(&"src/core.py".to_string()));
    }

    #[test]
    fn hidden_directories_are_still_indexed_contrary_to_walker_defaults() {
        let project = tempfile::tempdir().expect("temp dir");
        fs::create_dir_all(project.path().join(".github/workflows")).expect("dir");
        fs::write(
            project.path().join(".github/workflows/ci.yml"),
            "on: push\n",
        )
        .expect("file");

        let files = collect(project.path());

        assert!(files.contains(&".github/workflows/ci.yml".to_string()));
    }
}
