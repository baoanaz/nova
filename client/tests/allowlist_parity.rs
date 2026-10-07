//! TASK-097 §B：core ↔ client **两侧对照**测试。
//!
//! R42 要求"契约是忽略**语义**而不是库"，因此两侧必须能用同一份仓库夹具得出**同一份清单**。
//! 本文件的 `PARITY_REPO` / `PARITY_EXPECTED` 与 core 侧
//! `core/tests/pipeline/test_allowlist.py::test_parity_fixture_matches_expected_list`
//! 是**逐字相同**的两份拷贝（夹具形状 + 期望清单）。任何一侧语义漂移都会让其中一边失败。
//!
//! 夹具覆盖三类关键情形（卡内 §A-3 点名"必须为这三种情况都写测试"）：
//!
//! | 路径 | 期望 | 理由 |
//! |---|---|---|
//! | `hacks/skills/SKILL.md`（被 `.gitignore` 排除） | **索引** | 白名单要救回的真实技能文档 |
//! | `node_modules/pkg/skills/a.py` | **不索引** | 依赖目录里的同名结构（最易写错） |
//! | `.git/objects/ab/cdef` | **不索引** | 版本控制元数据 |

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

/// 与 core 侧同名的夹具仓库（`.gitignore` 排除 `hacks/`、`logs/`、`.claude/`、`AI-notes.tmp`）。
const PARITY_REPO: &[(&str, &str)] = &[
    (
        ".gitignore",
        "hacks/\nlogs/\nAI-notes.tmp\n.claude/\n.codex/\n",
    ),
    (".novainclude", "my-notes\n"),
    ("hacks/skills/SKILL.md", "# 技能\n"),
    ("hacks/skills/learned/helper.py", "HELPER = 1\n"),
    (".claude/settings.json", "{}\n"),
    (".claude/skills/review/SKILL.md", "# 评审技能\n"),
    (".claude/skills/review/reference.md", "# 参考\n"),
    (".codex/config.toml", "model = \"x\"\n"),
    (".pi/agent/skills/learned/SKILL.md", "# pi 技能\n"),
    ("AGENTS.md", "# 项目指令\n"),
    ("HANDOFF.md", "# 交接\n"),
    ("AI-notes.tmp", "笔记\n"),
    ("node_modules/pkg/skills/a.py", "x = 1\n"),
    (".git/objects/ab/cdef", "binary\n"),
    ("logs/deep/nested/z.txt", "z\n"),
    ("src/core.py", "def run(): pass\n"),
    ("README.md", "# 读我\n"),
    ("my-notes/todo.md", "# 待办\n"),
];

/// 两侧共同的期望清单（排序后逐字比较）。
const PARITY_EXPECTED: &[&str] = &[
    ".claude/skills/review/SKILL.md",
    ".claude/skills/review/reference.md",
    ".gitignore",
    ".novainclude",
    ".pi/agent/skills/learned/SKILL.md",
    "AGENTS.md",
    "HANDOFF.md",
    "README.md",
    "hacks/skills/SKILL.md",
    "hacks/skills/learned/helper.py",
    "my-notes/todo.md",
    "src/core.py",
];

fn build_parity_repo(root: &Path) {
    for (path, content) in PARITY_REPO {
        let target = root.join(path);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent).expect("parent dir");
        }
        fs::write(target, content).expect("file");
    }
}

/// 与 `IndexManager::scan()` 同口径的清单（walker 并集 + 文件判定 + 去重排序）。
fn scan_files(root: &Path) -> Vec<String> {
    let rules = nova_client::ignore::IgnoreRules::load(root);
    let mut files = BTreeSet::new();
    for entry in rules.walk_union() {
        let Ok(entry) = entry else { continue };
        if !entry.file_type().is_some_and(|kind| kind.is_file()) {
            continue;
        }
        files.insert(
            entry
                .path()
                .strip_prefix(root)
                .expect("entry below root")
                .to_string_lossy()
                .replace('\\', "/"),
        );
    }
    files.into_iter().collect()
}

#[test]
fn parity_fixture_matches_expected_list() {
    let project = tempfile::tempdir().expect("temp dir");
    build_parity_repo(project.path());

    let files = scan_files(project.path());

    assert_eq!(files, PARITY_EXPECTED, "client 侧清单必须与 core 侧逐字一致");
}

#[test]
fn parity_expected_list_encodes_the_three_critical_cases() {
    // 把"最关键的三条"单独断言，便于失败时一眼看出是哪一类语义漂了（而不是只看到 diff）。
    let project = tempfile::tempdir().expect("temp dir");
    build_parity_repo(project.path());
    let files = scan_files(project.path());

    assert!(files.iter().any(|path| path == "hacks/skills/SKILL.md"));
    assert!(files.iter().any(|path| path == ".claude/skills/review/SKILL.md"));
    assert!(!files.iter().any(|path| path.starts_with("node_modules/")));
    assert!(!files.iter().any(|path| path.starts_with(".git/")));
    assert!(!files.iter().any(|path| path.starts_with("logs/")));
    assert!(!files.iter().any(|path| path == "AI-notes.tmp"));
}
