"""解析预取子进程（``pipeline.prepare``）：结果与内联路径逐行相同，失败一律回退内联。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from nova_core.hashing import blob_hash
from nova_core.pipeline import DirectorySource, Indexer, prepare
from nova_core.pipeline.prepare import PrepareStream, prepare_file
from nova_core.storage import Store
from nova_core.types import BlobInput, ChangeSet
from nova_core.vectors import VectorStore

from .conftest import TEST_DIM, CountingEmbedding

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _fresh_pool() -> Iterator[None]:
    prepare.shutdown()
    yield
    prepare.shutdown()


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
        "# 冷启动\n\n索引流水线把解析放进子进程。AT&T C# c++ 混排。\n\n## 小节\n\n正文 café。\n",
        encoding="utf-8",
    )
    (docs / "plain.md").write_text("# Title\n\nSome text.\n", encoding="utf-8")
    (root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01binary\x00" * 10)
    (root / "huge.py").write_text("x = 1\n" * 40_000, encoding="utf-8")
    (root / "native").mkdir()
    (root / "native" / "a.cpp").write_text("int add(int a, int b) { return a + b; }\n")
    (root / "native" / "a.h").write_text("class Box { public: int size() const; };\n")
    (root / "native" / "b.c").write_text("static int twice(int v) { return v * 2; }\n")


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


def _index(repo: Path, project: Path) -> tuple[object, dict[str, list[tuple]]]:
    """首次同步形态：全部文件作为 ``added`` 进一次 ingest（与 service flush 一致）。"""
    blobs = []
    for file in sorted(repo.rglob("*")):
        if file.is_file():
            path = file.relative_to(repo).as_posix()
            data = file.read_bytes()
            blobs.append(BlobInput(path=path, content=data, blob_hash=blob_hash(path, data)))
    with Store.open(project) as store, VectorStore.open(project, dim=TEST_DIM) as vectors:
        # workers=1：向量阶段双消费者的跨窗口去重计数随时序抖动（见 embedding_sink），
        # 与预取无关；固定单消费者让报告可逐字段比较。
        report = Indexer(
            store, CountingEmbedding(), vectors, DirectorySource(repo), workers=1
        ).ingest(ChangeSet(added=tuple(blobs)))
    return report, _dump(project)


def test_pool_results_match_inline_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _write_corpus(repo)

    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "0")
    inline_report, inline_dump = _index(repo, tmp_path / "inline")

    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "1")
    monkeypatch.setattr(prepare, "MIN_COLD_POOL_FILES", 0)
    hits: list[str] = []
    original_take = PrepareStream.take

    def counting_take(self: PrepareStream, path: str):
        result = original_take(self, path)
        if result is not None:
            hits.append(path)
        return result

    monkeypatch.setattr(PrepareStream, "take", counting_take)
    pool_report, pool_dump = _index(repo, tmp_path / "pool")

    assert inline_report.files_parsed > 50
    assert len(hits) >= inline_report.files_parsed - 2, "预取子进程应当实际服务了绝大多数文件"
    assert pool_dump == inline_dump
    assert pool_report == inline_report
    assert inline_report.errors  # broken.py 的语法错误被同样记录
    assert "blob.bin" in inline_report.skipped_files


def test_prepare_file_returns_none_on_failure() -> None:
    assert prepare_file("a.bin", b"a\x00b", "python") is None
    assert prepare_file("a.xyz", b"text", "no-such-language") is None
    prepared = prepare_file("a.py", b"def f():\n    return 1\n", "python")
    assert prepared is not None and prepared.chunks and len(prepared.segments) == len(
        prepared.chunks
    )


def test_broken_pool_falls_back_to_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子进程崩溃（如被 OOM kill）→ 剩余文件回退内联，结果不变。"""
    repo = tmp_path / "repo"
    _write_corpus(repo)
    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "0")
    _, inline_dump = _index(repo, tmp_path / "inline")

    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "1")
    monkeypatch.setattr(prepare, "MIN_COLD_POOL_FILES", 0)
    original_take = PrepareStream.take
    calls = {"n": 0}

    def killing_take(self: PrepareStream, path: str):
        calls["n"] += 1
        if calls["n"] == 5 and self._executor is not None:
            for process in list(self._executor._processes.values()):  # noqa: SLF001
                process.kill()
        return original_take(self, path)

    monkeypatch.setattr(PrepareStream, "take", killing_take)
    _, pool_dump = _index(repo, tmp_path / "pool")
    assert calls["n"] > 5
    assert pool_dump == inline_dump


def test_small_ingest_does_not_start_a_cold_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "1")
    stream = PrepareStream([("a.py", b"x = 1\n", "python")])
    assert not stream.active
    stream.close()


def test_parse_workers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "0")
    assert prepare.parse_workers() == 0
    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "99")
    assert prepare.parse_workers() == prepare.MAX_PARSE_WORKERS
    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "x")
    assert prepare.parse_workers() == prepare.DEFAULT_PARSE_WORKERS


def _nested_python(depth: int) -> str:
    return "".join(f"{'  ' * level}class C{level}:\n" for level in range(depth)) + (
        "  " * depth + "pass\n"
    )


def _nested_cpp(depth: int) -> str:
    return "".join(f"namespace n{level} {{\n" for level in range(depth)) + "int f();\n" + (
        "}\n" * depth
    )


def test_recursion_depth_matches_inline_near_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抽取器递归吞 RecursionError：子进程必须在与内联相同的栈深解析，阈值两侧结果才一致。"""
    repo = tmp_path / "repo"
    (repo / "py").mkdir(parents=True)
    (repo / "cc").mkdir()
    for depth in range(100, 1000, 15):
        (repo / "py" / f"n{depth:04d}.py").write_text(_nested_python(depth), encoding="utf-8")
        (repo / "cc" / f"n{depth:04d}.cpp").write_text(_nested_cpp(depth), encoding="utf-8")

    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "0")
    inline_report, inline_dump = _index(repo, tmp_path / "inline")
    recursion_failures = [e for e in inline_report.errors if "RecursionError" in e]
    assert recursion_failures, "样本必须跨过递归阈值（部分文件内联解析失败）"
    assert len(recursion_failures) < inline_report.files_parsed, "也必须有成功解析的深文件"

    monkeypatch.setenv(prepare.PARSE_WORKERS_ENV, "1")
    monkeypatch.setattr(prepare, "MIN_COLD_POOL_FILES", 0)
    hits: list[str] = []
    original_take = PrepareStream.take

    def counting_take(self: PrepareStream, path: str):
        result = original_take(self, path)
        if result is not None:
            hits.append(path)
        return result

    monkeypatch.setattr(PrepareStream, "take", counting_take)
    pool_report, pool_dump = _index(repo, tmp_path / "pool")
    assert len(hits) == inline_report.files_parsed
    assert pool_report == inline_report
    assert pool_dump == inline_dump
