"""索引流水线：首次 / 增量 / 删除 / 幂等 / 指纹 / 解析失败 / R1 抬升（TASK-007）。

DoD 覆盖：首次 ingest 行数、改 1 个函数只嵌 1 个 chunk、改注释 0 次嵌入、删文件清向量与
spec 引用 stale、同变更集二次 ingest 零成本、指纹两档失效、语法错误不中断 ingest。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import pytest
from zace_core.chunking import PARSER_CONFIG_KEY, embedding_text, split_file
from zace_core.interfaces import EmbeddingProfile
from zace_core.parsing.registry import get_parser
from zace_core.pipeline import LANGUAGES_KEY, DirectorySource, Indexer, IngestReport
from zace_core.pipeline.embedding_sink import (
    DEFAULT_EMBED_WINDOW,
    MAX_EMBED_WINDOW,
    embed_window_size,
)
from zace_core.storage import Store
from zace_core.text import segment
from zace_core.types import ChangeSet, ParsedFile
from zace_core.vectors import VectorStore

from .conftest import DOC_MD, PY_MODULE, TEST_PROFILE, CountingEmbedding, write_repo

ChangeSetFactory = Callable[..., ChangeSet]


def _parse(path: str, source: str) -> ParsedFile:
    language = path.rsplit(".", 1)[-1]
    parser_language = {"py": "python", "md": "markdown", "c": "c", "cpp": "cpp", "h": "c"}[
        language
    ]
    return get_parser(parser_language).parse(path, source)


def _chunk_count(path: str, source: str) -> int:
    return len(split_file(_parse(path, source), source))


def _ingest_files(
    indexer: Indexer, changes: ChangeSetFactory, files: dict[str, str], repo: Path | None = None
) -> IngestReport:
    """写盘 + 按变更集索引（磁盘副本是全量重解析/reembed 的输入，贴近 CLI 真实形态）。"""
    if repo is not None:
        write_repo(repo, files)
    return indexer.ingest(changes(added=files))


# ---------------------------------------------------------------------------
# 首次 ingest
# ---------------------------------------------------------------------------


def test_first_ingest_populates_all_indexes(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
    vectors: VectorStore,
    repo: Path,
) -> None:
    report = _ingest_files(
        indexer, change_set, {"pkg/mod.py": PY_MODULE, "docs/design.md": DOC_MD}, repo
    )
    expected_chunks = _chunk_count("pkg/mod.py", PY_MODULE) + _chunk_count(
        "docs/design.md", DOC_MD
    )
    counts = store.counts()

    assert (report.added, report.modified, report.deleted) == (2, 0, 0)
    assert report.files_parsed == 2
    assert counts["files"] == 2
    assert counts["chunks"] == expected_chunks
    assert report.chunks_new == counts["chunks"]
    assert report.chunks_reused == 0
    assert vectors.count() == counts["chunks"]
    assert report.vectors_upserted == counts["chunks"]
    # FTS 可检索（jieba 预分词 + 中文/标识符混合）
    assert store.fts_search(segment("helper"))
    assert store.fts_search(segment("设计"))
    # 二阶段解析：Service.run 里的裸名调用 helper 已 fqn 化
    assert any(edge.target == "helper" for edge in store.edges_for("Service.run"))
    # spec_references 已从 markdown 的 mentioned 建立
    assert report.spec_refs > 0
    assert counts["edges"] > 0


def test_languages_are_persisted_for_repo_level_uplift(
    indexer: Indexer, change_set: ChangeSetFactory, store: Store
) -> None:
    report = _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE})

    assert report.languages == ("python",)
    assert indexer.languages == ("python",)
    assert store.get_config(LANGUAGES_KEY) == '["python"]'


# ---------------------------------------------------------------------------
# 增量
# ---------------------------------------------------------------------------


def test_changing_one_function_embeds_exactly_one_chunk(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    embedding: CountingEmbedding,
    store: Store,
    vectors: VectorStore,
) -> None:
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE})
    before_calls = embedding.calls

    updated = PY_MODULE.replace("return value + 1", "return value + 2")
    report = indexer.ingest(change_set(modified={"pkg/mod.py": updated}))

    assert report.chunks_new == 1
    assert report.chunks_reused > 0
    assert embedding.calls == before_calls + 1
    assert len(embedding.batches[-1]) == 1
    assert "value + 2" in embedding.batches[-1][0]
    assert vectors.count() == store.counts()["chunks"]


def test_comment_change_outside_any_chunk_costs_zero_embeddings(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    embedding: CountingEmbedding,
    store: Store,
) -> None:
    """注释落在类级空隙（不属于任何 chunk 区间）→ 文件 hash 变但零嵌入。"""
    source = "class C:\n    def m(self): return 1\n    # note v1\n    def n(self): return 2\n"
    _ingest_files(indexer, change_set, {"pkg/c.py": source})
    before = (embedding.calls, dict(store.counts()))

    report = indexer.ingest(change_set(modified={"pkg/c.py": source.replace("v1", "v2")}))

    assert report.chunks_new == 0
    assert embedding.calls == before[0]
    assert dict(store.counts()) == before[1]


def test_removing_a_function_deletes_its_vector_without_embedding(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    embedding: CountingEmbedding,
    store: Store,
    vectors: VectorStore,
) -> None:
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE})
    helper_id = next(
        chunk.id
        for chunk in split_file(_parse("pkg/mod.py", PY_MODULE), PY_MODULE)
        if chunk.symbol_fqn == "helper"
    )
    assert vectors.get_hashes([helper_id])

    helper = '''def helper(value: int) -> int:
    """返回 +1。"""
    return value + 1


'''
    report = indexer.ingest(change_set(modified={"pkg/mod.py": PY_MODULE.replace(helper, "")}))

    # 内容没有变化，只是 helper 消失 + 后续 chunk 行号漂移（D-04：id 含 start_line）
    assert report.chunks_new == 0
    assert report.chunks_removed == 3
    assert report.vectors_deleted == 3
    assert vectors.get_hashes([helper_id]) == {}  # 该 chunk 的向量已删
    assert vectors.count() == store.counts()["chunks"]
    assert embedding.calls > 0  # 漂移后的新 id 需要重建向量（见执行记录的口径说明）


def test_repeated_change_set_is_idempotent(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    embedding: CountingEmbedding,
    store: Store,
    vectors: VectorStore,
) -> None:
    changes = change_set(added={"pkg/mod.py": PY_MODULE, "docs/design.md": DOC_MD})
    indexer.ingest(changes)
    before_counts = dict(store.counts())
    before_calls = embedding.calls
    before_vectors = vectors.count()

    report = indexer.ingest(changes)

    assert report.chunks_new == 0
    assert report.chunks_reused == before_counts["chunks"]
    assert embedding.calls == before_calls
    assert dict(store.counts()) == before_counts
    assert vectors.count() == before_vectors


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_file_clears_rows_vectors_and_marks_spec_refs_stale(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
    vectors: VectorStore,
) -> None:
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE, "docs/design.md": DOC_MD})
    block = _parse("docs/design.md", DOC_MD).spec_blocks[0]
    spec_block_id = f"docs/design.md:{block.heading_path}:{block.start_line}"
    assert any(not row.stale for row in store.spec_refs_for_spec(spec_block_id))

    report = indexer.ingest(change_set(deleted=["pkg/mod.py"]))

    counts = store.counts()
    assert (report.deleted, counts["files"]) == (1, 1)
    assert counts["symbols"] == 0  # 只剩 markdown（无符号行）
    assert report.vectors_deleted > 0
    assert vectors.count() == counts["chunks"]
    # 被删符号的 spec 引用置 stale=1（行保留，供 MissingEvidence 警告）
    assert all(row.stale for row in store.spec_refs_for_spec(spec_block_id))
    # 代码文件的 FTS 行已清，残留命中均来自 markdown 文档
    assert all(hit.startswith("docs/design.md") for hit, _ in store.fts_search(segment("helper")))
    assert store.chunk_by_id("pkg/mod.py:Service:16") is None


def test_delete_unknown_file_reports_orphans(
    indexer: Indexer, change_set: ChangeSetFactory
) -> None:
    """本进程未见过该文件的 chunk id → 记入 orphan_files（诚实标注，不假装清干净）。"""
    report = indexer.ingest(change_set(deleted=["not/indexed.py"]))

    assert report.deleted == 1
    assert report.orphan_files == ("not/indexed.py",)


# ---------------------------------------------------------------------------
# 配置指纹（D-07）
# ---------------------------------------------------------------------------


def test_tampered_parser_hash_triggers_full_reparse(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
    vectors: VectorStore,
    repo: Path,
) -> None:
    _ingest_files(
        indexer, change_set, {"pkg/mod.py": PY_MODULE, "docs/design.md": DOC_MD}, repo
    )
    store.set_config(PARSER_CONFIG_KEY, "tampered")

    report = indexer.ingest(ChangeSet())

    assert report.invalidation.value == "full_reparse"
    assert report.files_parsed == 2  # 遍历 provider 全量
    assert vectors.count() == store.counts()["chunks"]
    # 指纹已刷新 → 紧接的增量不再全量
    assert indexer.ingest(ChangeSet()).invalidation.value == "none"


def test_embedding_profile_change_only_reembeds(
    store: Store, repo: Path, change_set: ChangeSetFactory
) -> None:
    vectors = VectorStore.open(store.project_dir, dim=TEST_PROFILE.dim)
    try:
        indexer = Indexer(store, CountingEmbedding(), vectors, DirectorySource(repo))
        _ingest_files(
            indexer, change_set, {"pkg/mod.py": PY_MODULE, "docs/design.md": DOC_MD}, repo
        )
        counts_before = dict(store.counts())

        switched = CountingEmbedding(
            EmbeddingProfile(model_id="local:other", dim=TEST_PROFILE.dim, max_input_tokens=512)
        )
        reporter = Indexer(store, switched, vectors, DirectorySource(repo))
        report = reporter.ingest(ChangeSet())

        assert report.invalidation.value == "reembed"
        assert report.files_parsed == 0  # 不重解析、不写 SQLite
        assert dict(store.counts()) == counts_before
        assert switched.calls == 1
        assert report.vectors_upserted == counts_before["chunks"]
        assert vectors.count() == counts_before["chunks"]
        # 指纹已对齐 → 常规增量
        assert reporter.ingest(ChangeSet()).invalidation.value == "none"
    finally:
        vectors.close()


def test_reembed_does_not_write_sqlite_rows(
    store: Store,
    repo: Path,
    change_set: ChangeSetFactory,
    embedding: CountingEmbedding,
    vectors: VectorStore,
) -> None:
    indexer = Indexer(store, embedding, vectors, DirectorySource(repo))
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE}, repo)
    counts_before = dict(store.counts())

    report = indexer.reembed()

    assert report.invalidation.value == "reembed"
    assert dict(store.counts()) == counts_before
    assert report.vectors_upserted == counts_before["chunks"]


# ---------------------------------------------------------------------------
# 解析失败与语言抬升（R1）
# ---------------------------------------------------------------------------


def test_syntax_error_does_not_abort_ingest(
    indexer: Indexer, change_set: ChangeSetFactory, store: Store
) -> None:
    broken = "def broken(:\n    pass\n"
    report = indexer.ingest(
        change_set(added={"pkg/broken.py": broken, "pkg/mod.py": PY_MODULE})
    )

    assert report.errors and report.errors[0].startswith("pkg/broken.py")
    assert report.added == 2
    assert store.chunk_by_id("pkg/broken.py:(module):1") is not None
    assert store.counts()["symbols"] > 0  # 另一个文件照常入库


def test_apply_failure_is_isolated_per_file(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-018 §C：单文件落库失败不中断 ingest，只写 report.errors 并跳过该文件。"""
    original = store.apply_file_change

    def failing(parsed, chunks, file_content_hash, commit=None, *, generated=False):
        if parsed.path == "pkg/bad.py":
            raise ValueError("模拟落库失败")
        return original(parsed, chunks, file_content_hash, commit, generated=generated)

    monkeypatch.setattr(store, "apply_file_change", failing)

    report = indexer.ingest(change_set(added={"pkg/bad.py": PY_MODULE, "pkg/good.py": PY_MODULE}))

    assert report.errors == ("pkg/bad.py: ValueError: 模拟落库失败",)
    assert (report.added, report.modified) == (1, 0)  # 失败文件不计入 added/modified
    assert store.counts()["files"] == 1  # 另一个文件照常入库


def test_split_failure_from_duplicate_ids_is_isolated(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-018 §B+§C 组合：切分抛出的重复 id ValueError 被单文件隔离，不进 sqlite。"""
    from zace_core.pipeline import indexer as indexer_module

    real_split = indexer_module.split_file

    def failing_split(parsed, content):
        if parsed.path == "pkg/dup.py":
            raise ValueError("pkg/dup.py: 切分产物出现重复 chunk id（禁止静默去重/丢弃）：dup × 2")
        return real_split(parsed, content)

    monkeypatch.setattr(indexer_module, "split_file", failing_split)

    report = indexer.ingest(change_set(added={"pkg/dup.py": PY_MODULE, "pkg/mod.py": PY_MODULE}))

    assert len(report.errors) == 1 and report.errors[0].startswith("pkg/dup.py: ValueError")
    assert (report.added, report.modified) == (1, 0)
    assert store.counts()["files"] == 1
    assert store.chunks_by_ids(["pkg/dup.py:(module):1"]) == []


def test_full_reparse_isolates_split_failure(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-018 §C：``--full`` 的存量枚举（``_rebuild_vectors``）同样按单文件隔离。"""
    from zace_core.pipeline import indexer as indexer_module

    files = {"pkg/bad.py": PY_MODULE, "pkg/mod.py": PY_MODULE}
    write_repo(repo, files)
    indexer.ingest(change_set(added=files))

    real_split = indexer_module.split_file

    def failing_split(parsed, content):
        if parsed.path == "pkg/bad.py":
            raise ValueError("模拟切分失败")
        return real_split(parsed, content)

    monkeypatch.setattr(indexer_module, "split_file", failing_split)
    report = indexer.full_reparse()

    # bad.py 在“写库”与“存量枚举”两个环节各报一次，其余文件照常处理
    assert [error.split(":", 1)[0] for error in report.errors] == ["pkg/bad.py", "pkg/bad.py"]
    assert report.added + report.modified == 1
    assert store.counts()["files"] == 2  # SQLite 行不变（重建不写库）


def test_binary_files_are_skipped(
    indexer: Indexer, change_set: ChangeSetFactory, store: Store
) -> None:
    report = indexer.ingest(
        change_set(added={"assets/logo.bin": "\x00\x01\x02", "pkg/mod.py": PY_MODULE})
    )

    assert report.skipped_files == ("assets/logo.bin",)
    assert report.added == 1
    assert store.counts()["files"] == 1


CPP_SOURCE = """class Vec {
public:
    int size() const;
};

int Vec::size() const {
    return 1;
}
"""

HEADER_SOURCE = """#pragma once

class Widget {
public:
    int size() const { return 1; }
};
"""


def test_h_files_follow_repo_language(
    indexer: Indexer, change_set: ChangeSetFactory, store: Store
) -> None:
    """R1：仓库出现 C++ 扩展名后，``.h`` 按 C++ 解析。"""
    header = "include/widget.h"
    indexer.ingest(change_set(added={header: HEADER_SOURCE}))
    assert store.exact_symbols("size") == []  # 仅 .h 的 C 仓库：C 抽取器不认类方法

    report = indexer.ingest(
        change_set(added={"src/vec.cpp": CPP_SOURCE}, modified={header: HEADER_SOURCE})
    )

    assert "cpp" in report.languages
    fqns = {row.fqn for row in store.exact_symbols("size")}
    assert "Widget::size" in fqns  # .h 已按 C++ 解析
    assert "Vec::size" in fqns
    assert report.invalidation.value == "none"


def test_c_only_repo_keeps_h_as_c(
    indexer: Indexer, change_set: ChangeSetFactory
) -> None:
    report = indexer.ingest(change_set(added={"src/a.c": "int add(int a) { return a; }\n"}))

    assert report.languages == ("c",)
    assert indexer.languages == ("c",)


# ---------------------------------------------------------------------------
# 输入边界
# ---------------------------------------------------------------------------


def test_change_set_blob_does_not_need_disk_file(
    indexer: Indexer, change_set: ChangeSetFactory, store: Store, repo: Path
) -> None:
    """Phase 2 service 形态：内容来自上传的 blob，磁盘上没有该文件。"""
    report = indexer.ingest(change_set(added={"virtual/mod.py": PY_MODULE}))

    assert report.added == 1
    assert store.counts()["files"] == 1
    assert not (repo / "virtual/mod.py").exists()


def test_directory_source_lists_and_reads_repo_files(repo: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "junk.py").write_text("junk\n", encoding="utf-8")
    source = DirectorySource(repo)

    assert source.list_files() == ("src/a.py",)
    assert source.read("src/a.py") == b"x = 1\n"
    with pytest.raises(Exception, match="非法仓库相对路径"):
        source.read("../outside.py")


def test_embedding_inputs_match_embedding_text(
    indexer: Indexer, change_set: ChangeSetFactory, embedding: CountingEmbedding
) -> None:
    """索引侧 embedding 输入 = ``embedding_text(chunk)``，且一律走 ``embed()``（R2）。"""
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE})
    expected = {
        embedding_text(chunk) for chunk in split_file(_parse("pkg/mod.py", PY_MODULE), PY_MODULE)
    }

    assert set(embedding.texts) == expected
    assert any("def helper" in text for text in embedding.texts)
    assert any("class Service" in text for text in embedding.texts)


# ---------------------------------------------------------------------------
# 向量阶段分窗（内存护栏）
# ---------------------------------------------------------------------------


class WindowedEmbedding(CountingEmbedding):
    """带批量参数的替身：让"分窗嵌入"可被观测（窗口 = ``batch_size × concurrency``）。"""

    batch_size = 2
    concurrency = 3


def _many_functions(count: int) -> str:
    """生成 ``count`` 个顶层函数的模块（每个函数≈1 chunk，用来撑过窗口）。"""
    return "\n\n\n".join(
        f'def f{index}(value: int) -> int:\n    """函数 {index}。"""\n    return value + {index}'
        for index in range(count)
    )


def test_embed_upsert_windows_batch_size_times_concurrency(
    store: Store, vectors: VectorStore, repo: Path, change_set: ChangeSetFactory
) -> None:
    """向量阶段按 ``批大小 × 并发`` 分窗：超过一个窗口的仓库必须分多次 embed + upsert。

    背景（2026-09-15）：原来把**整仓** chunk 一次性交给 ``embed()``，而 1024 维向量在 CPython
    里约 33 KB/条（24 B/float + 8 B/指针 + 列表头），20k chunk 的仓库仅向量就常驻 ~1 GB——
    在 2 GiB VPS 上实测把机器拖到失联。本用例锁住分窗行为，防止回归。
    """
    embedding = WindowedEmbedding()
    indexer = Indexer(store, embedding, vectors, DirectorySource(repo))
    source = _many_functions(20)
    report = _ingest_files(indexer, change_set, {"pkg/many.py": source}, repo)

    total = _chunk_count("pkg/many.py", source)
    window = WindowedEmbedding.batch_size * WindowedEmbedding.concurrency
    assert total > window  # 用例前提：chunk 数确实超过一个窗口
    assert embedding.calls == math.ceil(total / window)
    assert max(len(batch) for batch in embedding.batches) <= window
    # 分窗不改变结果：所有 chunk 都有向量
    assert report.vectors_upserted == total
    assert vectors.count() == total


def test_embed_window_size_falls_back_and_caps() -> None:
    """窗口 = 批大小 × 并发；provider 未声明批量能力时兜底，极端配置封顶。"""

    class Anonymous:
        """自定义 provider：既不声明 ``batch_size`` 也不声明 ``concurrency``。"""

    class LocalLike:
        """本地 ONNX provider 形态：只有 ``batch_size``（无并发）。"""

        batch_size = 16

    class VoyageLike:
        """Voyage 默认形态：批 500 × 并发 8。"""

        batch_size = 500
        concurrency = 8

    class Extreme:
        """把批与并发都调到极端：必须被上限削掉，否则又回到 GB 级内存。"""

        batch_size = 1000
        concurrency = 32

    assert embed_window_size(Anonymous()) == DEFAULT_EMBED_WINDOW
    assert embed_window_size(LocalLike()) == 16
    assert embed_window_size(VoyageLike()) == 4_000
    assert embed_window_size(Extreme()) == MAX_EMBED_WINDOW
    assert MAX_EMBED_WINDOW < 1000 * 32


# ---------------------------------------------------------------------------
# TASK-111：内容寻址复用（R4“复用键是 hash 不是 id”的落地）
# ---------------------------------------------------------------------------


def test_line_shift_reuses_vectors_without_rembedding(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    embedding: CountingEmbedding,
    store: Store,
    vectors: VectorStore,
) -> None:
    """文件顶部插入一行 → chunk_id 全部漂移，但**内容未变的 chunk 不得重嵌**。

    这是实测浪费的根因：``chunk_id = {path}:{fqn}:{start_line}`` 含行号，
    插入一行会让后续所有 chunk 换 id；旧实现按 id 比对 hash，于是整文件重嵌。

    注意 ``(module)`` 块的内容**真的变了**（新注释进了它），所以允许恰好 1 次嵌入；
    关键是后面的 ``helper`` / ``Service`` / ``Service.run`` 三个未变 chunk 不得出现在嵌入列表里。
    """
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE})
    calls_before = embedding.calls
    vectors_before = vectors.count()

    shifted = "# 新增注释行\n" + PY_MODULE
    report = indexer.ingest(change_set(modified={"pkg/mod.py": shifted}))

    embedded = [text for batch in embedding.batches[calls_before:] for text in batch]
    assert all("def helper" not in text for text in embedded), "未变的符号不应重嵌"
    assert all("class Service" not in text for text in embedded), "未变的类不应重嵌"
    assert len(embedded) == 1, f"只有 (module) 块内容变了，应恰好嵌 1 个，实际 {len(embedded)}"
    assert report.chunks_reused > 0
    # 向量行随 id 漂移搬运：总数不变（旧 id 被删、新 id 被插入），关键是与 chunk 数一致。
    assert vectors.count() == vectors_before
    assert vectors.count() == store.counts()["chunks"]


def test_identical_content_in_new_file_reuses_existing_vector(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    embedding: CountingEmbedding,
    store: Store,
    vectors: VectorStore,
) -> None:
    """同一内容出现在新路径（复制文件）→ 复用已有向量，不重嵌。"""
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE})
    calls_before = embedding.calls

    report = indexer.ingest(change_set(added={"pkg/copy.py": PY_MODULE}))

    assert embedding.calls == calls_before, "相同内容换个路径不应重嵌"
    # TASK-114 起口径拆开：``chunks_reused`` 只统计**文件级对账**（hash 未变）；
    # "新文件命中既有向量"属向量阶段的按内容复用，记在 ``chunks_deduped``。
    assert report.chunks_reused == 0
    assert report.chunks_deduped > 0
    assert vectors.count() == store.counts()["chunks"]


def test_changed_content_still_embeds(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    embedding: CountingEmbedding,
) -> None:
    """内容真变了必须重嵌（复用不得吞掉变更）——防“优化过头”静默返回旧向量。"""
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE})
    calls_before = embedding.calls

    updated = PY_MODULE.replace("return value + 1", "return value + 99")
    report = indexer.ingest(change_set(modified={"pkg/mod.py": updated}))

    assert embedding.calls == calls_before + 1
    assert report.chunks_new == 1
    assert any("value + 99" in text for text in embedding.batches[-1])


# ---------------------------------------------------------------------------
# TASK-REVIEW-RUNTIME P2-5：generated 信号落库
# ---------------------------------------------------------------------------


def test_generated_flag_is_written_from_name_convention(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
) -> None:
    """文件名约定命中 → ``files.generated=1``（此前该列硬编码为 0）。"""
    _ingest_files(indexer, change_set, {"src/api.pb.cc": "int f() { return 1; }\n"})

    assert store.generated_files() == frozenset({"src/api.pb.cc"})


def test_generated_flag_is_written_from_content_banner(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
) -> None:
    """内容 banner 命中 → generated=1（文件名代理做不到的增量能力）。

    设计文档 Module/01 §6 待决项 2 定的 V1 口径是"文件名约定 + 内容 banner"，
    而旧实现只落了前者（且恒为 0）。
    """
    banner = "// DO NOT EDIT. Generated by protoc.\nint f() { return 1; }\n"
    _ingest_files(indexer, change_set, {"src/ordinary_name.cc": banner})

    assert store.generated_files() == frozenset({"src/ordinary_name.cc"})


def test_handwritten_file_is_not_marked_generated(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
) -> None:
    """手写文件不得被误判（误判会降权真实证据，代价不对称）。"""
    _ingest_files(indexer, change_set, {"pkg/mod.py": PY_MODULE})

    assert store.generated_files() == frozenset()


def test_generated_flag_updates_on_modification(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
) -> None:
    """文件从生成改为手写（或反之）→ 列必须跟着更新，不是只在插入时算一次。"""
    banner = "// DO NOT EDIT. Generated by protoc.\nint f() { return 1; }\n"
    _ingest_files(indexer, change_set, {"pkg/mod.cc": banner})
    assert store.generated_files() == frozenset({"pkg/mod.cc"})

    handwritten = "int f() { return 2; }\n"
    indexer.ingest(change_set(modified={"pkg/mod.cc": handwritten}))

    assert store.generated_files() == frozenset()


# ---------------------------------------------------------------------------
# TASK-REVIEW-RUNTIME P1-2：跨 Indexer 实例删除不留孤儿向量
# ---------------------------------------------------------------------------


def test_delete_across_indexer_instances_clears_vectors(
    store: Store,
    embedding: CountingEmbedding,
    vectors: VectorStore,
    repo: Path,
    change_set: ChangeSetFactory,
) -> None:
    """跨实例删除必须清向量（旧行为只删 SQLite，留孤儿向量）。

    为什么这是真实场景：``Engine`` 每次 ingest 都新建 ``Indexer``，进程内的
    ``_known_chunks`` 因此永远是空的——旧实现在这种调用下一条向量都不删。
    """
    from zace_core.pipeline import DirectorySource, Indexer

    _ingest_files(Indexer(store, embedding, vectors, DirectorySource(repo)),
                  change_set, {"pkg/mod.py": PY_MODULE}, repo)
    indexed_vectors = vectors.count()
    assert indexed_vectors > 0

    # 全新实例（模拟 Engine 的下一次 ingest 调用）：内存里没有任何已知 chunk id。
    fresh = Indexer(store, embedding, vectors, DirectorySource(repo))
    report = fresh.ingest(change_set(deleted=["pkg/mod.py"]))

    assert report.vectors_deleted == indexed_vectors
    assert vectors.count() == 0
    # ``orphan_files`` 仍如实标注“本进程没见过”，但那已不影响向量清理。
    assert report.orphan_files == ("pkg/mod.py",)


def test_natural_language_mention_does_not_trigger_generated(
    indexer: Indexer,
    change_set: ChangeSetFactory,
    store: Store,
) -> None:
    """自然语言注释里提到 "generated by" 不得判为生成文件（真实误报回归）。

    实测误报：leveldb ``table/block.cc`` 的注释写着
    "Decodes the blocks generated by block_builder.cc"——这是**核心手写文件**。
    宽松的 banner 关键词会把它降权，直接伤害真实证据。本测试锁住这个边界。
    """
    handwritten = (
        "// Copyright (c) 2011 The LevelDB Authors. All rights reserved.\n"
        "// Use of this source code is governed by a BSD-style license.\n"
        "//\n"
        "// Decodes the blocks generated by block_builder.cc.\n"
        "int DecodeBlock() { return 1; }\n"
    )
    _ingest_files(indexer, change_set, {"table/block.cc": handwritten})

    assert store.generated_files() == frozenset()
