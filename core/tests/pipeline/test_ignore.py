"""TASK-037 §A/§B 回归：三层忽略规则 + 大小/二进制阈值。

**测试数据为什么长这样**（卡内 DoD 明文要求"每条都要有一个真实形状的 fixture，
不要只测自制玩具样例"）：``_REALISTIC_REPO`` 直接照抄 ``hello-agents`` / 本仓库实测到的
形态——``test_*.py`` 与 ``!test_tools.py`` 否定、``memory/`` 无斜杠目录规则、``lib/`` 这种
**不该**误伤的名字、子目录 ``.gitignore`` 的同名文件、``cmake-build-release/`` 变体目录
（TASK-036 §C.3 的直接输入）。下面每条规则都能在真实仓库里找到出处，注释里给了路径。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from nova_core.pipeline import DirectorySource, IgnoreRules, Indexer, IndexScope
from nova_core.pipeline.ignore import (
    DEFAULT_SKIP_DIRS,
    SKIP_REASON_BINARY,
    SKIP_REASON_OVERSIZE,
    oversize_reason,
    reason_from_entry,
)
from nova_core.storage import Store
from nova_core.types import BlobInput, ChangeSet
from nova_core.vectors import VectorStore

from .conftest import CountingEmbedding, write_repo

# ---------------------------------------------------------------------------
# 真实形状的仓库夹具
# ---------------------------------------------------------------------------

REPO_FILES = {
    # 该索引的真源码
    "src/core.py": "def run() -> int:\n    return 1\n",
    "src/util.py": "VALUE = 2\n",
    "README.md": "# 项目\n",
    "docs/guide.md": "# 指南\n",
    # ``test_*.py`` 被忽略，但 ``!test_tools.py`` 把它救回来（hello-agents/.gitignore:166-167）
    "code/chapter7/test_simple_agent.py": "def test_a():\n    assert True\n",
    "code/chapter7/test_tools.py": "def test_tool():\n    assert True\n",
    # ``memory/``（无斜杠目录规则）：git 语义是匹配**任意深度**的同名目录
    # （hello-agents 的 Co-creation-projects/*/memory/）
    "projects/x/memory/base.py": "class Base:\n    pass\n",
    # ``lib/`` 不在内置列表里：它是合法的源码目录名，误伤代价高于收益
    "lib/pure_python/helper.py": "def help_me() -> int:\n    return 1\n",
    # 构建产物变体目录：``build`` 挡不住，必须靠模式（TASK-036 §C.3 实测）
    "cmake-build-release/CMakeFiles/x.make": "all:\n\techo hi\n",
    "build-debug/out.json": "{}\n",
    # 内置目录名（.venv / __pycache__）
    ".venv/lib/python3.12/site-packages/pkg.py": "x = 1\n",
    "src/__pycache__/core.cpython-312.pyc": "\x00\x01",
    # 子目录 .gitignore **只作用于该目录**（本仓库/hello-agents 的多项目结构）
    "sub/.gitignore": "*.log\ngenerated/\n",
    "sub/app.log": "noise\n",
    "sub/app.py": "y = 3\n",
    "sub/generated/code.py": "z = 4\n",
    "other/app.log": "keep me\n",  # sub/.gitignore 不该管到这里
    # 根 .gitignore 的产物目录与秘密文件；.novaignore 救回后者（第 1 层 > 第 2 层）
    ".gitignore": "dist/\nsecret.env\nartifact.tmp\nmemory/\ntest_*.py\n!test_tools.py\n",
    ".novaignore": "!secret.env\n",
    "dist/bundle.js": "var a=1;\n",
    "secret.env": "TOKEN=abc\n",
    "artifact.tmp": "tmp\n",
}


@pytest.fixture
def ignore_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    write_repo(root, REPO_FILES)
    return root


@pytest.fixture
def rules(ignore_root: Path) -> IgnoreRules:
    return IgnoreRules.from_root(ignore_root)


# ---------------------------------------------------------------------------
# §A 三层优先级
# ---------------------------------------------------------------------------


def test_builtin_layer_skips_known_noise_dirs(rules: IgnoreRules) -> None:
    """第 3 层：内置目录名 / 目录模式 / 文件名模式。"""
    assert rules.is_ignored(".venv/lib/python3.12/site-packages/pkg.py", is_dir=False)
    assert rules.is_ignored("cmake-build-release", is_dir=True)
    assert rules.is_ignored("build-debug", is_dir=True)
    assert rules.is_ignored("src/__pycache__", is_dir=True)
    assert rules.reason_for("cmake-build-release") == "builtin"


def test_builtin_does_not_swallow_legitimate_source_dirs(rules: IgnoreRules) -> None:
    """反向断言：``lib/`` 是合法源码目录，**不得**被内置规则误伤。"""
    assert not rules.is_ignored("lib/pure_python/helper.py", is_dir=False)
    assert not rules.is_ignored("lib", is_dir=True)
    assert not rules.is_ignored("src/core.py", is_dir=False)


def test_gitignore_layer_is_real_parsed(rules: IgnoreRules) -> None:
    """第 2 层：真实解析 ``.gitignore``（这是 D-28 相对"只按目录名跳过"的核心改进）。"""
    assert rules.is_ignored("artifact.tmp", is_dir=False)
    assert rules.reason_for("artifact.tmp") == "gitignore"
    assert rules.is_ignored("dist", is_dir=True)


def test_novaignore_beats_gitignore(rules: IgnoreRules) -> None:
    """第 1 层最高优先级：``.novaignore`` 的 ``!secret.env`` 能救回 ``.gitignore`` 的忽略。"""
    assert not rules.is_ignored("secret.env", is_dir=False), "第 1 层必须能覆盖第 2 层"
    assert rules.is_ignored("artifact.tmp", is_dir=False), "未被救回的同类规则仍生效"


def test_nested_gitignore_scoped_to_its_directory(ignore_root: Path) -> None:
    """嵌套 ``.gitignore`` 只作用于自己所在目录（git 的按目录作用域语义）。"""
    rules = IgnoreRules.from_root(ignore_root)
    assert rules.is_ignored("sub/app.log", is_dir=False)
    assert rules.is_ignored("sub/generated", is_dir=True)
    assert not rules.is_ignored("other/app.log", is_dir=False), "子目录规则不得越界"
    assert not rules.is_ignored("other", is_dir=True)


def test_negation_can_rescue_file(rules: IgnoreRules) -> None:
    """``!pattern`` 否定：救命规则（hello-agents 的 ``test_*.py`` + ``!test_tools.py``）。"""
    assert rules.is_ignored("code/chapter7/test_simple_agent.py", is_dir=False)
    assert not rules.is_ignored("code/chapter7/test_tools.py", is_dir=False), "! 必须救回"


def test_directory_only_pattern_matches_any_depth(rules: IgnoreRules) -> None:
    """尾 ``/`` + 无斜杠：匹配**任意深度**的同名目录（``memory/`` 形态，真实仓库常见）。"""
    assert rules.is_ignored("projects/x/memory", is_dir=True)
    assert rules.is_ignored("projects/x/memory/base.py", is_dir=False)


def test_directory_only_pattern_does_not_match_plain_file(tmp_path: Path) -> None:
    """尾 ``/`` 只匹配目录：同名的**普通文件**不受影响（git 语义）。"""
    root = tmp_path / "r"
    write_repo(root, {".gitignore": "cache/\n", "cache": "this is a file\n"})
    rules = IgnoreRules.from_root(root)
    assert rules.is_ignored("cache", is_dir=True)
    assert not rules.is_ignored("cache", is_dir=False)


def test_double_star_crosses_directories(tmp_path: Path) -> None:
    """``**`` 跨目录（TASK-037 §A 明文要求）。"""
    root = tmp_path / "r"
    write_repo(
        root,
        {
            ".gitignore": "**/generated/*.py\nlogs/**\n",
            "a/b/c/generated/x.py": "x = 1\n",
            "a/generated/y.py": "y = 2\n",
            "logs/deep/nested/z.txt": "z\n",
        },
    )
    rules = IgnoreRules.from_root(root)
    assert rules.is_ignored("a/b/c/generated/x.py", is_dir=False)
    assert rules.is_ignored("a/generated/y.py", is_dir=False)
    assert rules.is_ignored("logs/deep/nested/z.txt", is_dir=False)


def test_leading_slash_anchors_to_root(tmp_path: Path) -> None:
    """前导 ``/`` 锚定仓库根：``/vendor`` 只管根下的 ``vendor``，不管子目录同名目录。

    （用 ``vendor`` 而不是 ``build``：``build`` 同时是内置跳过名，会掩盖锚定语义。）
    """
    root = tmp_path / "r"
    write_repo(
        root,
        {
            ".gitignore": "/vendor/\n",
            "vendor/lib.c": "int a;\n",
            "nested/vendor/b.c": "int b;\n",
        },
    )
    rules = IgnoreRules.from_root(root)
    assert rules.is_ignored("vendor", is_dir=True)
    assert not rules.is_ignored("nested/vendor", is_dir=True), "前导 / 不得匹配子目录"
    assert not rules.is_ignored("nested/vendor/b.c", is_dir=False)


def test_character_class_matching(tmp_path: Path) -> None:
    """``[]`` 字符类与 ``?`` 单字符通配（TASK-037 §A 明文要求）。"""
    root = tmp_path / "r"
    write_repo(
        root,
        {
            ".gitignore": "file[0-9].txt\nlog?.log\n",
            "file1.txt": "1\n",
            "file9.txt": "9\n",
            "fileA.txt": "A\n",
            "log1.log": "1\n",
            "log12.log": "12\n",
        },
    )
    rules = IgnoreRules.from_root(root)
    assert rules.is_ignored("file1.txt", is_dir=False)
    assert rules.is_ignored("file9.txt", is_dir=False)
    assert not rules.is_ignored("fileA.txt", is_dir=False)
    assert rules.is_ignored("log1.log", is_dir=False)
    assert not rules.is_ignored("log12.log", is_dir=False), "? 只吃一个字符"


def test_comments_and_blank_lines_ignored(tmp_path: Path) -> None:
    """注释（``#``）与空行不产生规则；``\\#`` 是字面量 ``#``。"""
    root = tmp_path / "r"
    write_repo(
        root,
        {
            ".gitignore": "# 这是注释\n\n   \nreal.txt\n\\#literal.txt\n",
            "real.txt": "r\n",
            "#literal.txt": "l\n",
        },
    )
    rules = IgnoreRules.from_root(root)
    assert rules.is_ignored("real.txt", is_dir=False)
    assert rules.is_ignored("#literal.txt", is_dir=False), r"\# 是字面量 #"


def test_missing_gitignore_behaves_unchanged(tmp_path: Path) -> None:
    """``.gitignore`` 不存在 → 不报错，行为等同"只有内置默认层"。"""
    root = tmp_path / "r"
    write_repo(root, {"src/a.py": "a = 1\n", ".venv/x.py": "x\n"})
    rules = IgnoreRules.from_root(root)
    assert not rules.is_ignored("src/a.py", is_dir=False)
    assert rules.is_ignored(".venv", is_dir=True)
    assert rules.reason_for("src/a.py") is None


def test_negation_cannot_rescue_file_inside_excluded_dir(tmp_path: Path) -> None:
    """git 语义：父目录被排除后，``!`` 不能再救回其中的文件（hello-agents 实测形态）。"""
    root = tmp_path / "r"
    write_repo(
        root,
        {
            ".gitignore": "venv/\ntest_*.py\n!test_tools.py\n",
            "venv/strategies/tests/test_tools.py": "import x\n",
        },
    )
    rules = IgnoreRules.from_root(root)
    assert rules.is_ignored("venv/strategies/tests/test_tools.py", is_dir=False)


# ---------------------------------------------------------------------------
# §A 目录剪枝（性能关键）
# ---------------------------------------------------------------------------


def test_ignored_directory_is_not_traversed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被忽略的目录**不被遍历**（卡内 DoD；``cmake-build-release/`` 下有几万个文件）。

    为什么用"计数 ``os.scandir`` 调用"而不是"断言耗时上界"：耗时受机器负载影响、不稳定，
    而且无法证明"是剪枝而不是碰巧快"。数调用次数能直接证明剪枝发生过。
    """
    import os as os_module

    root = tmp_path / "r"
    write_repo(
        root,
        {
            "src/a.py": "a = 1\n",
            "cmake-build-release/CMakeFiles/x.make": "all:\n",
        },
    )
    # 在构建产物目录下塞大量文件（真实形态：构建目录是大头）
    big = root / "cmake-build-release" / "many"
    big.mkdir(parents=True)
    for index in range(50):
        (big / f"f{index}.o").write_text("obj\n", encoding="utf-8")

    scanned: list[str] = []
    real_scandir = os_module.scandir

    def counting_scandir(path: str) -> Iterator[object]:
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os_module, "scandir", counting_scandir)
    files = DirectorySource(root).list_files()

    assert files == ("src/a.py",)
    assert not any("cmake-build-release" in path for path in scanned), (
        f"被忽略的目录被遍历了：{scanned}"
    )


# ---------------------------------------------------------------------------
# §B 阈值
# ---------------------------------------------------------------------------


def test_scope_rejects_oversize_file() -> None:
    """``> 128 KB`` 跳过，原因带真实字节数（R43 的 ``"oversize:134217728"`` 形态）。"""
    scope = IndexScope()
    size = 128 * 1024 + 1
    ok, reason = scope.should_read("big.bin", size)
    assert ok is False
    assert reason == oversize_reason(size)
    assert reason == "oversize:131073"
    # 边界：正好 128 KB 不跳（判据是 ">" 而不是 ">="）
    assert scope.should_read("ok.bin", 128 * 1024)[0] is True


def test_scope_rejects_binary_by_unprintable_ratio() -> None:
    """前 8 KB 中不可打印字符 ``> 10%`` 判二进制（Module/05 §3.1 同口径）。"""
    scope = IndexScope()
    # 真实二进制形状：ELF 头 + 控制字符（比"自制玩具"更接近 .a/.so）
    elf_like = b"\x7fELF\x02\x01\x01\x00" + bytes(range(0, 32)) * 20
    ok, reason = scope.check_bytes(elf_like)
    assert ok is False
    assert reason == SKIP_REASON_BINARY

    # NUL 一票否决（文本文件不含 NUL）
    assert scope.check_bytes(b"text\x00more") == (False, SKIP_REASON_BINARY)


def test_scope_accepts_utf8_and_tabs() -> None:
    """反向断言：中文文档 / 制表符 / 换行**不得**被误判为二进制（防止"一律跳过"的假通过）。"""
    scope = IndexScope()
    assert scope.check_bytes("# 中文标题\n\n\t缩进内容\n".encode())[0] is True
    assert scope.check_bytes(b"def f():\n\treturn 1\n")[0] is True
    assert scope.check_bytes(b"")[0] is True


def test_scope_probe_does_not_read_beyond_window() -> None:
    """二进制探测只看前 8 KB：**后面**的不可打印字节不该让一个文本文件被跳过。"""
    scope = IndexScope()
    text_head = b"x" * (8 * 1024)
    binary_tail = bytes(range(0, 32)) * 500
    assert scope.check_bytes(text_head + binary_tail)[0] is True


def test_scope_is_configurable_from_env() -> None:
    """阈值可配置（R43 明文要求）；非法值回落默认而不是让索引起不来。"""
    assert IndexScope.from_env({"NOVA_MAX_FILE_BYTES": "1024"}).max_bytes == 1024
    assert IndexScope.from_env({"NOVA_MAX_FILE_BYTES": "0"}).max_bytes == 128 * 1024
    assert IndexScope.from_env({"NOVA_MAX_FILE_BYTES": "abc"}).max_bytes == 128 * 1024
    assert IndexScope.from_env({}).max_bytes == 128 * 1024
    strict = IndexScope.from_env({"NOVA_BINARY_RATIO": "0.0"})
    assert strict.check_bytes(b"a\x01b")[0] is False


def test_scope_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        IndexScope(max_bytes=0)
    with pytest.raises(ValueError):
        IndexScope(binary_ratio=1.5)


def test_oversize_file_reports_reason_in_ingest(
    tmp_path: Path, store: Store, vectors: VectorStore, embedding: CountingEmbedding
) -> None:
    """回归 §B 的落地：超限文件进 ``skipped_files``（裸路径）+ ``skip_reasons``（带原因）。"""
    root = tmp_path / "repo"
    write_repo(root, {"src/ok.py": "ok = 1\n"})
    huge = root / "assets" / "big.bin"
    huge.parent.mkdir(parents=True)
    huge.write_bytes(b"x" * (200 * 1024))

    indexer = Indexer(
        store, embedding, vectors, DirectorySource(root), scope=IndexScope(max_bytes=128 * 1024)
    )
    report = indexer.full_reparse()

    assert report.skipped_files == ("assets/big.bin",), "裸路径语义不变（service 契约）"
    assert report.skip_reasons == ("assets/big.bin:oversize:204800",)
    assert report.files_parsed == 1
    assert store.counts()["files"] == 1


def test_binary_file_reports_reason_in_ingest(
    tmp_path: Path, store: Store, vectors: VectorStore, embedding: CountingEmbedding
) -> None:
    """二进制文件的原因标签是 ``binary``（与旧行为一致，只是现在带原因）。"""
    root = tmp_path / "repo"
    write_repo(root, {"src/ok.py": "ok = 1\n"})
    blob = root / "assets" / "libcv.a"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"\x7fELF\x02\x01\x01\x00" + bytes(range(1, 32)) * 100)

    indexer = Indexer(store, embedding, vectors, DirectorySource(root))
    report = indexer.full_reparse()

    assert report.skipped_files == ("assets/libcv.a",)
    assert report.skip_reasons == ("assets/libcv.a:binary",)


def test_skipped_files_and_reasons_stay_aligned(
    tmp_path: Path, store: Store, vectors: VectorStore, embedding: CountingEmbedding
) -> None:
    """两个字段必须等长同序（消费方按 zip 读；长度漂移是静默错位）。"""
    root = tmp_path / "repo"
    write_repo(root, {"src/ok.py": "ok = 1\n", "assets/logo.png": "\x00\x01\x02"})
    indexer = Indexer(store, embedding, vectors, DirectorySource(root))
    report = indexer.full_reparse()

    assert len(report.skipped_files) == len(report.skip_reasons)
    for path, entry in zip(report.skipped_files, report.skip_reasons, strict=True):
        assert entry.startswith(f"{path}:")


def test_reason_from_entry_handles_legacy_bare_paths() -> None:
    """兼容旧报告里的裸路径（TASK-007 的既有语义：全部是二进制跳过）。"""
    assert reason_from_entry("a/b.png") == SKIP_REASON_BINARY
    assert reason_from_entry("a/b.png:oversize:2048") == SKIP_REASON_OVERSIZE
    assert reason_from_entry("a/b.c:binary") == SKIP_REASON_BINARY


def test_change_set_blob_also_respects_threshold(
    store: Store, vectors: VectorStore, embedding: CountingEmbedding
) -> None:
    """service 上传路径（内容在 ChangeSet 里、磁盘上没有）走**同一**阈值（R43 同口径）。"""
    indexer = Indexer(
        store,
        embedding,
        vectors,
        DirectorySource(Path("/nonexistent")),
        scope=IndexScope(max_bytes=1024),
    )
    data = b"y" * 2048
    report = indexer.ingest(
        ChangeSet(
            added=(
                BlobInput(
                    path="uploaded/big.txt",
                    content=data,
                    blob_hash=hashlib.sha256(data).hexdigest(),
                ),
            )
        )
    )
    assert report.skipped_files == ("uploaded/big.txt",), "磁盘没有该文件也要走阈值"
    assert report.skip_reasons == (f"uploaded/big.txt:oversize:{len(data)}",)
    assert report.added == 0, "被跳过的文件不得计入 added"


# ---------------------------------------------------------------------------
# DirectorySource 行为
# ---------------------------------------------------------------------------


def test_directory_source_respects_ignore_files(ignore_root: Path) -> None:
    """``DirectorySource`` 默认启用三层忽略；``respect_ignore_files=False`` 退回旧行为。"""
    filtered = DirectorySource(ignore_root).list_files()
    raw = DirectorySource(ignore_root, respect_ignore_files=False).list_files()

    assert "src/core.py" in filtered
    assert "secret.env" in filtered, ".novaignore 的 ! 救回"
    assert "sub/app.log" not in filtered
    assert "other/app.log" in filtered, "sub/.gitignore 不得越界"
    assert "code/chapter7/test_tools.py" in filtered, "! 救回的文件要在清单里"
    assert "code/chapter7/test_simple_agent.py" not in filtered
    assert not any(".venv" in path for path in filtered)

    # respect_ignore_files=False 退回“忽略规则引入前”的行为：只按内置目录名剪枝，
    # 因此 .venv 仍被跳过（这是旧行为本身），但 .gitignore/.novaignore 不再生效。
    assert not any(".venv" in path for path in raw)
    assert "code/chapter7/test_simple_agent.py" in raw
    assert "artifact.tmp" in raw, "无忽略规则时 .gitignore 条目不再生效"


def test_directory_source_prunes_but_read_still_works(ignore_root: Path) -> None:
    """剪枝只影响列举；``read()`` 对已列举路径照常可用（不引入新的读失败）。"""
    source = DirectorySource(ignore_root)
    assert source.read("src/core.py") == b"def run() -> int:\n    return 1\n"


def test_ignore_rules_handles_missing_root(tmp_path: Path) -> None:
    """仓库目录不存在时不抛异常（空清单），与既有行为一致。"""
    source = DirectorySource(tmp_path / "not-there")
    assert source.list_files() == ()
    assert IgnoreRules.from_root(tmp_path / "not-there").is_ignored("a.py", is_dir=False) is False


def test_walk_is_not_slower_than_baseline(ignore_root: Path) -> None:
    """冒烟：剪枝后的列举不得比"全量列举"慢一个数量级（防止实现引入 O(n²)）。"""
    source = DirectorySource(ignore_root)
    ignored = source.ignore
    assert ignored is not None
    source.list_files()  # warm-up（首次会解析 .gitignore）
    started = time.perf_counter()
    for _ in range(5):
        source.list_files()
    pruned = time.perf_counter() - started

    started = time.perf_counter()
    for _ in range(5):
        DirectorySource(ignore_root, respect_ignore_files=False).list_files()
    baseline = time.perf_counter() - started
    assert pruned < baseline * 10 + 1.0, f"剪枝路径过慢：{pruned:.3f}s vs {baseline:.3f}s"


def test_default_skip_dirs_is_reachable_from_new_home() -> None:
    """``DEFAULT_SKIP_DIRS`` 从 ``source`` 迁到 ``ignore`` 后导出面必须不变（兼容导入）。"""
    from nova_core.pipeline import DEFAULT_SKIP_DIRS as exported

    assert exported is DEFAULT_SKIP_DIRS
    assert "node_modules" in exported
