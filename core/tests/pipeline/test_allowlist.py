"""TASK-097 §A：第 0 层"索引白名单"（强制包含）回归。

**这一份测试的重点不是"白名单能救回文件"**（那是最容易写对的一半），而是它的**三条硬边界**：

1. 白名单**不得突破内置目录剪枝**——``node_modules/pkg/skills/a.py`` 与
   ``.git/objects/**`` 必须仍然进不来（卡内 §A-3/§A-4 点名此处最易写错）；
2. 白名单**不改变**既有三层的行为——未命中白名单的路径，忽略判定必须
   **逐字不变**；
3. ``skills/`` 是**目录名**规则（任意层级），但下钻必须是**有界 + 名字定向**的，
   否则一个被 ``.gitignore`` 排除的 ``vendor/``（几十万文件）会被整棵枚举，
   推翻 TASK-037 的性能约束。

夹具 ``allowlist_repo`` 直接照抄卡内 §A-3 的最小复现仓库（``.gitignore: hacks/`` +
``hacks/skills/SKILL.md`` + ``node_modules/pkg/skills/a.py``），并补上真实仓库里同时存在的形态
（``.claude/skills``、``.pi/agent/skills``、``AGENTS.md``、被排除的 ``logs/``）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from nova_core.pipeline import Allowlist, DirectorySource, IgnoreRules
from nova_core.pipeline.ignore import (
    ALLOWLIST_ENV_VAR,
    ALLOWLIST_SCOPE_ENV_VAR,
    ALLOWLIST_SCOPES,
    DEFAULT_ALLOWLIST,
    DEFAULT_ALLOWLIST_DIRS,
    DEFAULT_ALLOWLIST_FILENAMES,
    DEFAULT_LOOKTHROUGH_DEPTH,
)

from .conftest import write_repo

# ---------------------------------------------------------------------------
# 夹具：卡内 §A-3 的最小复现仓库 + 真实仓库形态
# ---------------------------------------------------------------------------

ALLOWLIST_REPO = {
    # 卡内 §A-3 的实测仓库：``hacks/`` 被 gitignore 排除，技能文档住在里面。
    ".gitignore": "hacks/\nlogs/\nAI-notes.tmp\n.claude/\n",
    "hacks/skills/SKILL.md": "# 技能：被 gitignore 排除的技能文档\n",
    "hacks/skills/learned/helper.py": "HELPER = 1\n",
    # 真实仓库形态：``.claude/`` 被排除，技能住在 ``.claude/skills/<分类>/<技能>/SKILL.md``
    ".claude/settings.json": "{}\n",
    ".claude/skills/review/SKILL.md": "# 代码评审技能\n",
    ".claude/skills/review/reference.md": "# 参考\n",
    # 用户明确点名的默认白名单文件
    "AGENTS.md": "# 项目指令\n",
    "CLAUDE.md": "# Claude 指令\n",
    ".agent.md": "# agent 变体\n",
    ".cursorrules": "rule\n",
    "HANDOFF.md": "# 交接\n",
    "docs/HANDOFF.md": "# 子目录里的交接\n",
    # 要救回的 gitignore 文件（白名单文件名规则的直接证据）
    "AI-notes.tmp": "笔记\n",
    # 绝不能救回：依赖目录里的同名结构（卡内 §A-3 点名）
    "node_modules/pkg/skills/a.py": "x = 1\n",
    # 绝不能救回：版本控制元数据（卡内 §A-3 点名）
    ".git/objects/ab/cdef": "\x00\x01binary\n",
    # 不该被无界下钻的被排除目录（性能守门）：里面**没有**白名单命中项。
    # 卡内 §A-4 的硬约束（TASK-037）：被忽略的目录**整体剪枝**，不该为它去 scandir。
    "logs/deep/nested/z.txt": "z\n",
    # 既有三层的行为基线（这些断言必须逐字不变）
    "src/core.py": "def run() -> int:\n    return 1\n",
    "other/app.log": "keep\n",
}


@pytest.fixture
def allowlist_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    write_repo(root, ALLOWLIST_REPO)
    return root


@pytest.fixture
def listed(allowlist_root: Path) -> tuple[str, ...]:
    return DirectorySource(allowlist_root).list_files()


# ---------------------------------------------------------------------------
# §A-3 三种关键情形（卡内明文要求"必须为这三种情况都写测试"）
# ---------------------------------------------------------------------------


def test_gitignored_skills_dir_is_rescued(allowlist_root: Path, listed: tuple[str, ...]) -> None:
    """要救的：被 ``.gitignore`` 排除的 ``hacks/skills/**`` 整体进入清单。

    这一条同时验证了**下钻**（``hacks/`` 自己被排除，不进去就永远发现不了 ``skills/``）
    与**子树打开**（``hacks/skills/learned/helper.py`` 不是白名单文件名，靠目录规则进来）。
    """
    assert "hacks/skills/SKILL.md" in listed
    assert "hacks/skills/learned/helper.py" in listed
    rules = IgnoreRules.from_root(allowlist_root)
    assert not rules.is_ignored("hacks/skills/SKILL.md", is_dir=False)


def test_hidden_ai_dir_skills_are_rescued(allowlist_root: Path, listed: tuple[str, ...]) -> None:
    """真实形态：``.claude/skills/<分类>/<技能>/SKILL.md`` 与同目录辅助文件都在。

    这是用户提出需求时的原话场景（``.gitignore`` 里写了 ``.claude/``，里面的 SKILL.md
    就索引不到）。nova 本来就不因"隐藏"跳过（``.hidden(false)``），所以这一条测的纯粹是
    "被 gitignore 排除"这条通路。
    """
    assert ".claude/skills/review/SKILL.md" in listed
    assert ".claude/skills/review/reference.md" in listed, "目录规则不限文件名"
    assert ".claude/settings.json" not in listed, "不命中白名单的仍按 gitignore 排除"


def test_dependency_dir_skills_are_never_rescued(
    allowlist_root: Path, listed: tuple[str, ...]
) -> None:
    """**绝不能救的（1/2）**：``node_modules/pkg/skills/a.py`` 不得出现。

    这是卡内点名"最容易写错"的守护：``skills`` 是通用目录名，若白名单在目录剪枝**之前**
    生效（或下钻不先判内置目录名），依赖包里的同名结构就会被整片拽进索引。
    """
    assert "node_modules/pkg/skills/a.py" not in listed
    assert not any(path.startswith("node_modules/") for path in listed)
    rules = IgnoreRules.from_root(allowlist_root)
    assert rules.is_ignored("node_modules/pkg/skills/a.py", is_dir=False)
    assert rules.reason_for("node_modules/pkg/skills/a.py") == "builtin"


def test_git_metadata_is_never_rescued(allowlist_root: Path, listed: tuple[str, ...]) -> None:
    """**绝不能救的（2/2）**：``.git/objects/**`` 不得进入（二进制 + 毫无意义）。"""
    assert not any(path.startswith(".git/") for path in listed)
    rules = IgnoreRules.from_root(allowlist_root)
    assert rules.is_ignored(".git/objects/ab/cdef", is_dir=False)
    assert rules.reason_for(".git/objects/ab/cdef") == "builtin"


# ---------------------------------------------------------------------------
# §A-3/§A-4 下钻有界：一个被排除的大目录不该被整棵枚举
# ---------------------------------------------------------------------------


def test_lookthrough_does_not_enter_ignored_dirs_without_a_match(
    allowlist_root: Path, listed: tuple[str, ...]
) -> None:
    """``logs/``（被 gitignore 排除、里面没有任何白名单命中项）**不被下钻**。

    若实现改成"凡是被忽略的目录都进去看看"，这一条会失败，同时 TASK-037 的性能约束
    （``cmake-build-release/`` 下几万文件、``lib/libcv.a`` 308MB）会被静默推翻。
    """
    assert not any(path.startswith("logs/") for path in listed)
    assert not any(path.startswith("node_modules/") for path in listed)


def test_lookthrough_does_enter_ignored_dirs_with_a_matching_child(
    allowlist_root: Path, listed: tuple[str, ...]
) -> None:
    """反向断言：**有**白名单命中子项的被忽略目录（``hacks/`` → ``skills``）必须进得去。

    与上一条构成一对：只测其中一边都会让"永远不进"或"永远都进"的错实现通过。
    """
    rules = IgnoreRules.from_root(allowlist_root)
    source = DirectorySource(allowlist_root)
    assert source.ignore is not None
    assert rules.is_ignored("hacks", is_dir=True), "hacks/ 自身确实被 gitignore 命中"
    assert "hacks/skills/SKILL.md" in listed


def test_lookthrough_depth_is_bounded(tmp_path: Path) -> None:
    """下钻是**有界**的：比上限更深的 ``skills/`` 不会被发现（``shallow`` 为 0 层，最直观）。

    这一条同时锁定了 ``NOVA_ALLOWLIST_SCOPE=shallow`` 的语义：白名单只压 ``.gitignore`` 的
    **文件级**规则，不改变目录剪枝。
    """
    root = tmp_path / "repo"
    write_repo(
        root,
        {
            ".gitignore": "hacks/\n",
            "hacks/skills/SKILL.md": "# 深处\n",  # 需要下钻 2 层（``hacks`` → ``skills``）
            "AGENTS.md": "# 指令\n",  # 文件级，shallow 下仍必须救回
        },
    )
    deep = DirectorySource(root, ignore=IgnoreRules.from_root(root, allowlist=Allowlist()))
    assert "hacks/skills/SKILL.md" in deep.list_files()

    shallow_rules = IgnoreRules.from_root(
        root, allowlist=Allowlist(scope="shallow")
    )
    shallow = DirectorySource(root, ignore=shallow_rules).list_files()
    assert "AGENTS.md" in shallow, "shallow 仍压文件级 gitignore 规则"
    assert "hacks/skills/SKILL.md" not in shallow, "shallow 不穿透被排除的目录"


def test_gitignored_scope_only_rescues_dirs_named_by_the_allowlist(tmp_path: Path) -> None:
    """``gitignored`` 范围：只为"**自己**被白名单点名的目录"放行，不为被排除的目录下钻。

    两个子情形的对照（这是最容易把范围语义写反的地方）：

    - ``.gitignore: skills/`` + 白名单 ``skills`` → 目录自身命中，整棵子树放行；
    - ``.gitignore: hacks/`` + ``hacks/skills/`` → 需要为 ``hacks/`` 下钻，``gitignored``
      **救不到**（得用默认的 ``deep``）。
    """
    self_named = tmp_path / "self-named"
    write_repo(
        self_named,
        {".gitignore": "skills/\n", "skills/learned/SKILL.md": "# s\n", "skills/a.py": "a\n"},
    )
    rules = IgnoreRules.from_root(self_named, allowlist=Allowlist(scope="gitignored"))
    listed = DirectorySource(self_named, ignore=rules).list_files()
    assert "skills/learned/SKILL.md" in listed
    assert "skills/a.py" in listed, "目录自身命中 → 整棵子树放行"

    nested = tmp_path / "nested"
    write_repo(nested, {".gitignore": "hacks/\n", "hacks/skills/SKILL.md": "# s\n"})
    gitignored = IgnoreRules.from_root(nested, allowlist=Allowlist(scope="gitignored"))
    deep = IgnoreRules.from_root(nested, allowlist=Allowlist(scope="deep"))
    assert "hacks/skills/SKILL.md" not in DirectorySource(nested, ignore=gitignored).list_files()
    assert "hacks/skills/SKILL.md" in DirectorySource(nested, ignore=deep).list_files(), (
        "同样的仓库在 deep 范围下必须救得到（范围语义差异是真实存在的）"
    )


def test_gitignored_scope_does_not_change_builtin_pruning(tmp_path: Path) -> None:
    """任何范围下白名单都不突破内置剪枝（``node_modules`` 里的同名结构永远进不来）。"""
    root = tmp_path / "repo"
    write_repo(
        root,
        {".gitignore": "x/\n", "node_modules/pkg/skills/a.py": "a\n", ".git/objects/x": "b\n"},
    )
    for scope in sorted(ALLOWLIST_SCOPES):
        rules = IgnoreRules.from_root(root, allowlist=Allowlist(scope=scope))
        listed = DirectorySource(root, ignore=rules).list_files()
        assert not any(path.startswith("node_modules/") for path in listed), scope
        assert not any(path.startswith(".git/") for path in listed), scope


def test_lookthrough_depth_env_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``NOVA_ALLOWLIST_SCOPE`` / ``NOVA_ALLOWLIST_DEPTH`` 可直接配置，非法值回落默认。

    非法范围取值必须回落 ``deep``（默认），不能意外变成 ``none``（那会让白名单静默失效）。
    """
    root = tmp_path / "repo"
    write_repo(root, {".gitignore": "hacks/\n", "hacks/skills/SKILL.md": "# 深处\n"})

    monkeypatch.setenv(ALLOWLIST_SCOPE_ENV_VAR, "shallow")
    assert DirectorySource(root).list_files() == (".gitignore",)

    monkeypatch.delenv(ALLOWLIST_SCOPE_ENV_VAR)
    assert "hacks/skills/SKILL.md" in DirectorySource(root).list_files()

    # 非法取值回落默认（一个打错的环境变量不该让索引起不来，也不该意外放宽）
    monkeypatch.setenv(ALLOWLIST_SCOPE_ENV_VAR, "not-a-scope")
    assert "hacks/skills/SKILL.md" in DirectorySource(root).list_files()


def test_empty_allowlist_restores_task_037_behaviour(tmp_path: Path) -> None:
    """``Allowlist(builtin=())`` 关闭白名单：清单回到 TASK-037 的三层结果（对照组的基准）。"""
    root = tmp_path / "repo"
    write_repo(
        root,
        {
            ".gitignore": "hacks/\nAGENTS.md\n",
            "hacks/skills/SKILL.md": "# 技能\n",
            "AGENTS.md": "# 指令\n",
            "src/core.py": "x = 1\n",
        },
    )
    off = IgnoreRules.from_root(root, allowlist=Allowlist(builtin=()))
    assert off.is_ignored("AGENTS.md", is_dir=False)
    assert off.is_ignored("hacks/skills/SKILL.md", is_dir=False)
    assert DirectorySource(root, ignore=off).list_files() == (".gitignore", "src/core.py")
    assert off.allowlist_names() == ()


# ---------------------------------------------------------------------------
# §A-2 配置方式：.novainclude / 环境变量 / 默认清单
# ---------------------------------------------------------------------------


def test_default_allowlist_contents_are_locked() -> None:
    """默认清单受测试锁定：用户按手册"删掉不想要的项"时，基线是可核对的。"""
    assert DEFAULT_ALLOWLIST_DIRS == ("skills",)
    assert DEFAULT_ALLOWLIST_FILENAMES == (
        "agents.md",
        "claude.md",
        ".agent.md",
        ".cursorrules",
        "handoff.md",
    )
    assert DEFAULT_ALLOWLIST == DEFAULT_ALLOWLIST_DIRS + DEFAULT_ALLOWLIST_FILENAMES
    assert DEFAULT_LOOKTHROUGH_DEPTH >= 2, "至少能覆盖 ``hacks/skills/x`` 这种两层形态"


def test_novainclude_adds_custom_patterns(tmp_path: Path) -> None:
    """``.novainclude`` 里自定义的模式生效（卡内 DoD 的 ``my-notes/*.md`` 形态）。"""
    root = tmp_path / "repo"
    write_repo(
        root,
        {
            ".gitignore": "my-notes/\n",
            ".novainclude": "# 我的笔记也要索引\nmy-notes\n",
            "my-notes/todo.md": "# 待办\n",
            "other/skip.md": "# 无关\n",
        },
    )
    listed = DirectorySource(root).list_files()
    assert "my-notes/todo.md" in listed
    assert "other/skip.md" in listed, "未命中白名单的普通文件照旧（本来就不被忽略）"


def test_novainclude_can_cancel_a_builtin_entry(tmp_path: Path) -> None:
    """``!`` 前缀取消内置项：白名单是"包含"清单，用户必须能关掉不需要的（```!skills```）。"""
    root = tmp_path / "repo"
    write_repo(
        root,
        {
            ".gitignore": "hacks/\n",
            ".novainclude": "!skills\n",
            "hacks/skills/SKILL.md": "# 不要它\n",
            "AGENTS.md": "# 仍旧要\n",
        },
    )
    listed = DirectorySource(root).list_files()
    assert "hacks/skills/SKILL.md" not in listed
    assert "AGENTS.md" in listed, "只取消 skills，不影响其他内置项"


def test_env_allowlist_is_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``NOVA_INDEX_ALLOWLIST``（逗号分隔）与内置、``.novainclude`` **取并集**。"""
    root = tmp_path / "repo"
    write_repo(
        root,
        {
            ".gitignore": "env-notes/\ninc-notes/\n",
            "env-notes/a.md": "# env\n",
            "inc-notes/b.md": "# include\n",
        },
    )
    monkeypatch.setenv(ALLOWLIST_ENV_VAR, "env-notes, ")
    listed = DirectorySource(root).list_files()
    assert "env-notes/a.md" in listed

    monkeypatch.delenv(ALLOWLIST_ENV_VAR)
    write_repo(root, {".novainclude": "inc-notes\n"})
    assert "inc-notes/b.md" in DirectorySource(root).list_files()
    assert "env-notes/a.md" not in DirectorySource(root).list_files(), "env 撤掉后不该还在"


def test_missing_novainclude_is_not_an_error(tmp_path: Path) -> None:
    """``.novainclude`` 不存在 → 用内置默认，不报错（与忽略文件同口径）。"""
    root = tmp_path / "repo"
    write_repo(root, {"AGENTS.md": "# a\n", ".gitignore": "AGENTS.md\n"})
    assert DirectorySource(root).list_files() == (".gitignore", "AGENTS.md")


def test_scope_names_are_closed_set() -> None:
    """范围取值是封闭集合（配置来自环境变量，非法值必须能被识别并回落）。"""
    assert ALLOWLIST_SCOPES == {"deep", "gitignored", "shallow"}


# ---------------------------------------------------------------------------
# 既有三层行为不变（卡内 DoD：未命中白名单的路径行为逐字不变）
# ---------------------------------------------------------------------------


def test_non_allowlisted_paths_behave_unchanged(allowlist_root: Path) -> None:
    """白名单只影响"被排除但命中白名单"的路径；其余忽略判定与 TASK-037 完全一致。"""
    rules = IgnoreRules.from_root(allowlist_root)
    # 命中 gitignore 且**不**命中白名单 → 依旧被忽略（原因标签也不变）
    assert rules.is_ignored("logs/deep/nested/z.txt", is_dir=False)
    assert rules.is_ignored("AI-notes.tmp", is_dir=False)
    assert rules.reason_for("AI-notes.tmp") == "gitignore"
    # 未被排除的普通文件不受影响
    assert not rules.is_ignored("src/core.py", is_dir=False)
    assert not rules.is_ignored("other/app.log", is_dir=False)


def test_no_allowlist_matches_task_037_behaviour(allowlist_root: Path) -> None:
    """``Allowlist(builtin=())``（等价于关闭白名单）时，清单回到 TASK-037 的三层结果。

    这是"白名单没有意外放宽范围"的对照组：两份清单的差集必须**只有**白名单救回的那些文件。
    """
    disabled_rules = IgnoreRules.from_root(allowlist_root, allowlist=Allowlist(builtin=()))
    disabled = DirectorySource(allowlist_root, ignore=disabled_rules).list_files()
    enabled = DirectorySource(allowlist_root).list_files()

    assert "hacks/skills/SKILL.md" not in disabled
    assert ".claude/skills/review/SKILL.md" not in disabled
    assert not any(path.startswith("node_modules/") for path in disabled)
    assert set(disabled) < set(enabled), "启用白名单只会增加条目，不会移除"

    # 文件级白名单（AGENTS.md 被 .gitignore 排除时）在对照组里仍是排除的
    write_repo(allowlist_root, {".gitignore": "hacks/\nAI-notes.tmp\nAGENTS.md\n"})
    rules = IgnoreRules.from_root(allowlist_root, allowlist=Allowlist(builtin=()))
    assert rules.is_ignored("AGENTS.md", is_dir=False), "对照组：无白名单则 AGENTS.md 被排除"
    assert not IgnoreRules.from_root(allowlist_root).is_ignored("AGENTS.md", is_dir=False)


def test_case_insensitive_matching(allowlist_root: Path) -> None:
    """大小写不敏感（卡内 DoD：``agents.md`` 与 ``AGENTS.md`` 等价）。"""
    write_repo(
        allowlist_root,
        {
            ".gitignore": "agents.md\nclaude.md\nskills/\n",
            "AGENTS.md": "# 大写\n",
            "CLAUDE.md": "# 大写\n",
            "Skills/local/test.py": "t = 1\n",
        },
    )
    listed = DirectorySource(allowlist_root).list_files()
    assert "AGENTS.md" in listed
    assert "CLAUDE.md" in listed
    assert "Skills/local/test.py" in listed, "目录名也大小写不敏感"


def test_allowlist_applies_to_novaignore_too(tmp_path: Path) -> None:
    """白名单压过**第 1 层** ``.novaignore``（不只是 ``.gitignore``）——否则"强制包含"名不副实。"""
    root = tmp_path / "repo"
    write_repo(
        root,
        {
            ".novaignore": "AGENTS.md\n",
            "AGENTS.md": "# 指令\n",
            "other.md": "# 其他\n",
            ".gitignore": "other.md\n",
        },
    )
    listed = DirectorySource(root).list_files()
    assert "AGENTS.md" in listed
    assert "other.md" not in listed, "未命中白名单的 .novaignore 条目照旧生效"


def test_allowlist_does_not_bypass_size_or_binary_thresholds(tmp_path: Path) -> None:
    """白名单只影响**索引范围（忽略规则）**，不影响 §B 阈值：超大/二进制仍被跳过并带原因。

    这一条防止"白名单"被误解为"无论如何都要索引"——R43 的 ``skipped_files`` 诚实性不变。
    """
    from nova_core.pipeline import IndexScope

    scope = IndexScope(max_bytes=16)
    ok, reason = scope.should_read("hacks/skills/SKILL.md", 32)
    assert ok is False and reason == "oversize:32"


def test_listed_files_are_stable_and_sorted(allowlist_root: Path) -> None:
    """白名单不破坏 ``list_files`` 的稳定排序契约（增量对账依赖它）。"""
    first = DirectorySource(allowlist_root).list_files()
    second = DirectorySource(allowlist_root).list_files()
    assert first == second == tuple(sorted(first))


def test_allowlist_names_exposes_effective_entries(allowlist_root: Path) -> None:
    """生效条目可自述（手册/服务端要用它告诉用户"你现在强制索引了什么"）。"""
    rules = IgnoreRules.from_root(allowlist_root)
    names = rules.allowlist_names()
    assert "skills" in names
    assert "agents.md" in names
    assert rules.allowlist_policy("hacks") == "by-name"
    assert rules.allowlist_dir_match("hacks/skills") is True


def test_read_still_works_for_rescued_paths(allowlist_root: Path) -> None:
    """剪枝/下钻只影响列举；救回的文件 ``read()`` 必须照常可用（否则索引会拿到空内容）。"""
    source = DirectorySource(allowlist_root)
    assert source.read("hacks/skills/SKILL.md").startswith(b"# \xe6\x8a\x80\xe8\x83\xbd")


def test_walk_does_not_enumerate_pruned_dirs(
    allowlist_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """性能守门：``node_modules`` 与无命中项的 ``logs/`` **不被 scandir**。"""
    scanned: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path: os.PathLike[str] | str) -> Iterator[object]:
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    DirectorySource(allowlist_root).list_files()

    assert not any("node_modules" in path for path in scanned), f"依赖目录被遍历：{scanned}"
    # ``repo/logs`` 本身会被 scandir（判断"是否值得下钻"必须看一眼它的子项名字），
    # 但它的**子目录** ``logs/deep`` 不得被进入——被忽略的目录整体剪枝。
    assert not any("logs/deep" in path for path in scanned), (
        f"无白名单命中的被排除目录被下钻：{scanned}"
    )


# ---------------------------------------------------------------------------
# §B 两侧对照：与 client 的同一份夹具 / 同一份期望清单
# ---------------------------------------------------------------------------

#: 与 `client/tests/allowlist_parity.rs` 的 `PARITY_REPO` **逐字相同**的夹具
#: （两边各自持有一份拷贝——core 是 Python、client 是 Rust，没有共享夹具机制；
#: 语义漂移会让其中一边的期望清单失败，这正是对照测试要抓的）。
PARITY_REPO = {
    ".gitignore": "hacks/\nlogs/\nAI-notes.tmp\n.claude/\n.codex/\n",
    ".novainclude": "my-notes\n",
    "hacks/skills/SKILL.md": "# 技能\n",
    "hacks/skills/learned/helper.py": "HELPER = 1\n",
    ".claude/settings.json": "{}\n",
    ".claude/skills/review/SKILL.md": "# 评审技能\n",
    ".claude/skills/review/reference.md": "# 参考\n",
    ".codex/config.toml": 'model = "x"\n',
    ".pi/agent/skills/learned/SKILL.md": "# pi 技能\n",
    "AGENTS.md": "# 项目指令\n",
    "HANDOFF.md": "# 交接\n",
    "AI-notes.tmp": "笔记\n",
    "node_modules/pkg/skills/a.py": "x = 1\n",
    ".git/objects/ab/cdef": "binary\n",
    "logs/deep/nested/z.txt": "z\n",
    "src/core.py": "def run(): pass\n",
    "README.md": "# 读我\n",
    "my-notes/todo.md": "# 待办\n",
}

#: 两侧共同的期望清单（与 `PARITY_EXPECTED` 逐字相同）。
PARITY_EXPECTED = (
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
)


def test_parity_fixture_matches_expected_list(tmp_path: Path) -> None:
    """core 侧清单 == ``PARITY_EXPECTED``（client 侧有同名同内容的断言）。

    两侧都过 → R42 的"两侧语义一致"在这一组关键形态上成立；任一侧漂移 → 该侧失败。
    """
    root = tmp_path / "repo"
    write_repo(root, PARITY_REPO)
    assert DirectorySource(root).list_files() == PARITY_EXPECTED


def test_parity_expected_list_encodes_the_three_critical_cases(tmp_path: Path) -> None:
    """把最关键的三条单独断言，失败时一眼看出是哪一类语义漂了。"""
    root = tmp_path / "repo"
    write_repo(root, PARITY_REPO)
    listed = DirectorySource(root).list_files()

    assert "hacks/skills/SKILL.md" in listed, "被 gitignore 排除的 skills/ 必须救回"
    assert ".claude/skills/review/SKILL.md" in listed
    assert not any(path.startswith("node_modules/") for path in listed)
    assert not any(path.startswith(".git/") for path in listed)
    assert not any(path.startswith("logs/") for path in listed)
    assert "AI-notes.tmp" not in listed
