"""单文件预处理（``pipeline.prepare``）：同一个纯函数、固定解析栈、上传期后台预取。

不变量：预取只改变"计算何时发生"，不改变任何索引结果——有无预取、在哪个线程、从多深的
调用栈进入，SQLite 全部逻辑表与报告都逐项相同。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from nova_core.hashing import blob_hash
from nova_core.parsing.registry import get_parser
from nova_core.pipeline import DirectorySource, Indexer
from nova_core.pipeline.ignore import IndexScope
from nova_core.pipeline.prepare import (
    PreparedFile,
    PrepareSkip,
    UploadPrefetcher,
    parse_at_canonical_depth,
    prepare_file,
)
from nova_core.storage import Store
from nova_core.types import BlobInput, ChangeSet
from nova_core.vectors import VectorStore

from .conftest import TEST_DIM, CountingEmbedding

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCOPE = IndexScope()


def _write_corpus(root: Path) -> None:
    """真实源码 + 边界样本：二进制、超限、语法错误、中文 markdown、C++ 仓库里的 ``.h``。"""
    sources = sorted((_REPO_ROOT / "core" / "nova_core").rglob("*.py"))[:80]
    for source in sources:
        target = root / "src" / source.relative_to(_REPO_ROOT / "core")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    docs = root / "docs"
    docs.mkdir()
    (docs / "zh.md").write_text(
        "# 冷启动\n\n索引流水线把解析放进后台。AT&T C# c++ 混排。\n\n## 小节\n\n正文 café。\n",
        encoding="utf-8",
    )
    (root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01binary\x00" * 10)
    (root / "late_nul.py").write_bytes(b"x = 1\n" * 3000 + b"\x00")
    (root / "huge.py").write_text("x = 1\n" * 40_000, encoding="utf-8")
    (root / "native").mkdir()
    (root / "native" / "a.cpp").write_text("int add(int a, int b) { return a + b; }\n")
    (root / "native" / "a.h").write_text("class Box { public: int size() const; };\n")


def _nested_expression(depth: int) -> str:
    """Python 深层括号表达式（抽取器 ``_scan`` 递归；不随深度膨胀到超限）。"""
    return "x = " + "f(" * depth + "1" + ")" * depth + "\n"


def _nested_namespaces(depth: int) -> str:
    return "".join(f"namespace n{level} {{\n" for level in range(depth)) + "int f();\n" + (
        "}\n" * depth
    )


def _write_deep_corpus(root: Path) -> None:
    (root / "py").mkdir(parents=True)
    (root / "cc").mkdir()
    for depth in range(100, 1000, 25):
        (root / "py" / f"n{depth:04d}.py").write_text(_nested_expression(depth), encoding="utf-8")
        (root / "cc" / f"n{depth:04d}.cpp").write_text(
            _nested_namespaces(depth), encoding="utf-8"
        )


def _blobs(repo: Path) -> list[BlobInput]:
    blobs = []
    for file in sorted(repo.rglob("*")):
        if file.is_file():
            path = file.relative_to(repo).as_posix()
            data = file.read_bytes()
            blobs.append(BlobInput(path=path, content=data, blob_hash=blob_hash(path, data)))
    return blobs


def _dump(project: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(project / "index.db")
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'chunks_fts_%' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        dump: dict[str, list[tuple]] = {}
        for table in tables:
            cursor = conn.execute(f"SELECT rowid, * FROM {table}")  # noqa: S608 - 表名来自库本身
            columns = [d[0] for d in cursor.description]
            keep = [i for i, name in enumerate(columns) if name != "indexed_at"]
            dump[table] = sorted(
                (tuple(row[i] for i in keep) for row in cursor.fetchall()), key=repr
            )
        return dump
    finally:
        conn.close()


def _index(repo: Path, project: Path, *, prefetcher: UploadPrefetcher | None = None):
    """首次同步形态：全部文件作为 ``added`` 进一次 ingest（与 service flush 一致）。"""
    blobs = _blobs(repo)
    view = prefetcher.view("p", _SCOPE) if prefetcher is not None else None
    try:
        with Store.open(project) as store, VectorStore.open(project, dim=TEST_DIM) as vectors:
            # workers=1：向量阶段双消费者的跨窗口去重计数随时序抖动（见 embedding_sink），
            # 与预处理无关；固定单消费者让报告可逐字段比较。
            report = Indexer(
                store, CountingEmbedding(), vectors, DirectorySource(repo),
                scope=_SCOPE, workers=1, prefetch=view,
            ).ingest(ChangeSet(added=tuple(blobs)))
    finally:
        if view is not None:
            view.close()
    return report, _dump(project)


def _prefetch_all(prefetcher: UploadPrefetcher, repo: Path) -> None:
    prefetcher.submit("p", [(b.path, b.blob_hash, b.content) for b in _blobs(repo)], _SCOPE)
    prefetcher.wait_idle(timeout=120)


# ---------------------------------------------------------------------------
# 结果一致性
# ---------------------------------------------------------------------------


def test_prefetched_ingest_matches_inline_byte_for_byte(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_corpus(repo)
    inline_report, inline_dump = _index(repo, tmp_path / "inline")

    prefetcher = UploadPrefetcher(max_source_bytes=64 * 1024 * 1024)
    try:
        _prefetch_all(prefetcher, repo)
        report, dump = _index(repo, tmp_path / "prefetched", prefetcher=prefetcher)
        assert prefetcher.held_bytes == 0  # ingest 结束释放全部暂存
    finally:
        prefetcher.close()

    assert inline_report.files_parsed > 50
    # 跳过判定（二进制/超限）同样来自预取；唯一未命中的是 .h：C++ 仓库里按 cpp 解析，
    # 预取时按扩展名识别为 c → 语言不匹配，内联处理。
    assert report.prefetched == len(_blobs(repo)) - 1
    assert dump == inline_dump
    assert report == inline_report
    assert any("broken.py" in error for error in inline_report.errors)
    assert {"blob.bin", "late_nul.py", "huge.py"} <= set(inline_report.skipped_files)


def test_partial_prefetch_matches_inline(tmp_path: Path) -> None:
    """只预取了一部分（内存护栏截断）时，其余文件内联补齐，结果不变。"""
    repo = tmp_path / "repo"
    _write_corpus(repo)
    _, inline_dump = _index(repo, tmp_path / "inline")
    prefetcher = UploadPrefetcher(max_source_bytes=60_000)
    try:
        _prefetch_all(prefetcher, repo)
        report, dump = _index(repo, tmp_path / "partial", prefetcher=prefetcher)
    finally:
        prefetcher.close()
    assert 0 < report.prefetched < report.files_parsed
    assert dump == inline_dump


def test_parse_result_is_independent_of_caller_stack_depth(tmp_path: Path) -> None:
    """固定解析栈：深嵌套文件从浅/深调用栈、主进程/预取子进程进入，结果都一样。"""
    repo = tmp_path / "repo"
    _write_deep_corpus(repo)
    shallow_report, shallow_dump = _index(repo, tmp_path / "shallow")
    failures = [e for e in shallow_report.errors if "RecursionError" in e]
    assert failures and len(failures) < shallow_report.files_parsed, "样本必须跨过递归阈值"

    def deep(levels: int):
        return _index(repo, tmp_path / "deep") if levels == 0 else deep(levels - 1)

    deep_report, deep_dump = deep(60)
    assert deep_dump == shallow_dump and deep_report == shallow_report

    prefetcher = UploadPrefetcher(max_source_bytes=64 * 1024 * 1024)
    try:
        _prefetch_all(prefetcher, repo)
        thread_report, thread_dump = _index(repo, tmp_path / "thread", prefetcher=prefetcher)
    finally:
        prefetcher.close()
    assert thread_report.prefetched == len(_blobs(repo))
    assert thread_dump == shallow_dump and thread_report == shallow_report


# ---------------------------------------------------------------------------
# prepare_file 口径
# ---------------------------------------------------------------------------


def test_prepare_file_skip_and_error_semantics() -> None:
    assert prepare_file("a.bin", b"a\x00b", "python", _SCOPE) == PrepareSkip("a.bin", "binary")
    oversize = prepare_file("big.py", b"x" * (_SCOPE.max_bytes + 1), "python", _SCOPE)
    assert isinstance(oversize, PrepareSkip) and oversize.skip_reason.startswith("oversize:")
    unknown = prepare_file("a.xyz", b"text", "no-such-language", _SCOPE)
    assert isinstance(unknown, PreparedFile) and unknown.parsed.fallback
    assert unknown.errors and unknown.errors[0].startswith("a.xyz: ParserUnavailableError")
    ok = prepare_file("a.py", b"def f():\n    return 1\n", "python", _SCOPE)
    assert isinstance(ok, PreparedFile) and len(ok.segments) == len(ok.chunks) > 0


def test_split_failure_is_recorded_after_parse_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from nova_core.pipeline import prepare

    def boom(parsed, text):  # noqa: ANN001, ARG001
        raise ValueError("split boom")

    monkeypatch.setattr(prepare, "split_file", boom)
    result = prepare_file("bad.py", b"def broken(:\n", "python", _SCOPE)
    assert isinstance(result, PrepareSkip) and result.skip_reason is None
    assert result.errors[-1] == "bad.py: ValueError: split boom"
    assert len(result.errors) == 2  # 解析错误在前，切分失败在后（与内联口径一致）


# ---------------------------------------------------------------------------
# 预取器行为
# ---------------------------------------------------------------------------


def test_prefetch_respects_memory_cap_and_releases_on_ingest_end() -> None:
    prefetcher = UploadPrefetcher(max_source_bytes=100)
    try:
        accepted = prefetcher.submit(
            "p", [("a.py", "h1", b"x = 1\n" * 10), ("b.py", "h2", b"y = 2\n" * 20)], _SCOPE
        )
        assert accepted == 1  # 第二个超出护栏，不入队
        prefetcher.wait_idle(timeout=60)
        assert prefetcher.held_bytes == 60
        prefetcher.view("p", _SCOPE).close()  # 未被消费的结果在 ingest 结束时释放
        assert prefetcher.held_bytes == 0
    finally:
        prefetcher.close()


def test_unfinished_prefetch_is_not_awaited() -> None:
    """尚未算完的预取不等待（低优先级子进程，等它不如主线程直接算），并被取消。"""
    from concurrent.futures import Future

    prefetcher = UploadPrefetcher(max_source_bytes=1 << 20)
    try:
        pending: Future[bytes] = Future()
        key = ("p", "a.py", "h1", "python", (_SCOPE.max_bytes, _SCOPE.binary_ratio,
               _SCOPE.probe_bytes), __import__("sys").getrecursionlimit())
        prefetcher._entries[key] = (pending, 6, 0.0)  # noqa: SLF001 - 构造"未算完"
        prefetcher._held_bytes = 6  # noqa: SLF001
        view = prefetcher.view("p", _SCOPE)
        assert view.take("a.py", "h1", "python") is None
        assert pending.cancelled() and prefetcher.held_bytes == 0
        view.close()
    finally:
        prefetcher.close()


def test_take_requires_exact_key_and_discard_drops_project() -> None:
    prefetcher = UploadPrefetcher(max_source_bytes=1 << 20)
    try:
        prefetcher.submit("p", [("a.py", "h1", b"x = 1\n")], _SCOPE)
        prefetcher.submit("q", [("b.py", "h2", b"y = 2\n")], _SCOPE)
        prefetcher.wait_idle(timeout=60)
        view = prefetcher.view("p", _SCOPE)
        assert view.take("a.py", "other-hash", "python") is None
        assert view.take("a.py", "h1", "cpp") is None
        assert view.take("a.py", None, "python") is None
        assert isinstance(view.take("a.py", "h1", "python"), PreparedFile)
        view.close()
        prefetcher.discard("q")
        assert prefetcher.held_bytes == 0
    finally:
        prefetcher.close()


def test_closed_prefetcher_accepts_nothing() -> None:
    prefetcher = UploadPrefetcher(max_source_bytes=1 << 20)
    prefetcher.close()
    assert prefetcher.submit("p", [("a.py", "h1", b"x = 1\n")], _SCOPE) == 0


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_UPLOAD_PREFETCH", "0")
    assert UploadPrefetcher.from_env() is None
    monkeypatch.setenv("NOVA_UPLOAD_PREFETCH", "1")
    monkeypatch.setenv("NOVA_PREFETCH_MAX_MB", "2")
    prefetcher = UploadPrefetcher.from_env()
    assert prefetcher is not None
    assert prefetcher._max_source_bytes == 2 * 1024 * 1024  # noqa: SLF001


def test_parsers_are_thread_safe() -> None:
    """``ts.Parser`` 按线程隔离：两线程并发解析同一语言，结果与串行一致。"""
    sources = sorted((_REPO_ROOT / "core" / "nova_core").rglob("*.py"))[:40]
    texts = [(s.name, s.read_text(encoding="utf-8")) for s in sources]
    parser = get_parser("python")
    expected = [parse_at_canonical_depth(parser, name, text) for name, text in texts]
    results: dict[int, list] = {}

    def work(slot: int) -> None:
        results[slot] = [parse_at_canonical_depth(parser, name, text) for name, text in texts]

    threads = [threading.Thread(target=work, args=(slot,)) for slot in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results[0] == expected and results[1] == expected
