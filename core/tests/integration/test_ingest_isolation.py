"""TASK-036 §B 回归：单个"读不了"的文件**不得**拖垮整次 ingest。

**立项理由（真实复现）**：``DirectorySource.list_files()`` 与 ``DirectorySource.read()`` 对
"什么算合法仓库相对路径"的口径不一致：

```text
list_files()  → candidate.relative_to(root).as_posix()   # 原样返回，含反斜杠
read(path)    → _resolve(path) 拒绝 "\\"  → SourcePathError(ValueError)
```

反斜杠在 Linux 上是**合法文件名字符**（``weird\\name.py`` 是一个 10 字节的单段文件名），
真实的老仓库里存在这类文件（本仓库六个靶场里没有，属代码审查发现的潜在缺陷，不是自举命中的）。

修复前的最小复现（``Engine.ingest_repo`` 直接抛异常，退出码 1，**已解析的文件也留不下来**）：

```text
$ uv run nova-core ingest --repo <含 weird\\name.py 的仓库> --data <tmp>
nova-core: SourcePathError: 非法仓库相对路径：'src/weird\\name.py'
```

根因分两半，归属不同（本文件只锁"隔离"这一半）：

- **读写口径不一致**在 ``pipeline/source.py``，归 TASK-037（TASK-036 明确不得改该文件）；
- **隔离缺口**在 ``pipeline/indexer.py`` 与 ``engine.plan_scan``（此前只捕 ``OSError``），
  归本卡 —— 一个文件的读失败必须以 ``errors`` 如实记录后跳过，而不是中止整仓。

因此本文件断言的是行为契约（"不中止 + 如实记录 + 其余文件照常入库"），
不把 ``list_files()`` 是否应该返回该类路径当成已裁定的事实。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from nova_core.engine import Engine
from nova_core.interfaces import EmbeddingProfile
from nova_core.pipeline import DirectorySource, Indexer, IngestReport
from nova_core.storage import Store
from nova_core.vectors import VectorStore

#: 会被 ``read()`` 拒绝的路径（Linux 合法文件名：单段名字里含反斜杠）。
HOSTILE_PATH = "weird\\name.py"
#: 同仓库里的正常文件（必须照常入库）。
GOOD_NAME = "ok.py"
GOOD_SOURCE = "def ok() -> int:\n    return 1\n"
HOSTILE_SOURCE = "def hostile() -> int:\n    return 2\n"

#: 反斜杠是 Windows 的路径分隔符，那里构造不出该文件名（缺陷形态仅存在于 POSIX 文件系统）。
_NEEDS_POSIX_FILENAME = pytest.mark.skipif(
    sys.platform == "win32", reason="反斜杠在 Windows 上是路径分隔符，构造不出该文件名"
)


class _FakeEmbedding:
    """确定性假 provider（CI 不联网、不加载模型；本文件只关心 ingest 的隔离行为）。"""

    def __init__(self, dim: int = 8) -> None:
        self._profile = EmbeddingProfile(model_id="fake:isolation", dim=dim, max_input_tokens=512)

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (self._profile.dim - 1) for _ in texts]

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts)


def _make_repo(tmp_path: Path, *, hostile: bool) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / GOOD_NAME).write_text(GOOD_SOURCE, encoding="utf-8")
    if hostile:
        (root / HOSTILE_PATH).write_text(HOSTILE_SOURCE, encoding="utf-8")
    return root


def _assert_isolated(report: IngestReport, *, hostile: bool, parsed: int, chunks: int) -> None:
    assert report.files_parsed == parsed, "正常文件必须照常解析入库"
    assert report.chunks_new == chunks
    if hostile:
        assert report.errors, "读不了的文件必须**如实进入 errors**（不静默、不中止）"
        assert all(HOSTILE_PATH in error for error in report.errors), report.errors
        assert all("SourcePathError" in error for error in report.errors), (
            "错误信息要能让运维认出根因"
        )
    else:
        assert report.errors == ()


@_NEEDS_POSIX_FILENAME
def test_ingest_repo_isolates_unreadable_path(tmp_path: Path) -> None:
    """``Engine.ingest_repo``（CLI 路径）：含敌意文件名的仓库仍能完成索引。"""
    root = _make_repo(tmp_path, hostile=True)
    engine = Engine.open(tmp_path / "data", provider=_FakeEmbedding())
    handle, _ = engine.resolve_repo(root)

    report = engine.ingest_repo(handle.project_id, root)  # 修复前：抛 SourcePathError

    _assert_isolated(report, hostile=True, parsed=1, chunks=1)
    status = engine.sync_status(handle.project_id)
    assert status.files_indexed == 1
    assert status.chunks == 1


@_NEEDS_POSIX_FILENAME
def test_full_reparse_isolates_unreadable_path(tmp_path: Path) -> None:
    """``Indexer.full_reparse``（遍历 provider 清单那条路径）同样隔离，而不是整仓失败。

    注：``full_reparse`` 会枚举两次清单（``_collect_inputs`` 解析一轮，``_rebuild_vectors``
    为了拿到存量 chunk id 再解析一轮），所以读失败的文件会**报两次**。这是枚举方式的既有
    结果（不是本次修复引入的），本测试只锁"每次都如实记录、不中止"。
    """
    root = _make_repo(tmp_path, hostile=True)
    project = tmp_path / "project"
    provider = _FakeEmbedding()
    with (
        Store.open(project) as store,
        VectorStore.open(project, dim=provider.profile.dim) as vectors,
    ):
        indexer = Indexer(store, provider, vectors, DirectorySource(root))
        report = indexer.full_reparse()  # 修复前：抛 SourcePathError

        _assert_isolated(report, hostile=True, parsed=1, chunks=1)
        assert store.counts()["files"] == 1
        assert vectors.count() == 1


def test_clean_repo_records_no_errors(tmp_path: Path) -> None:
    """反向断言：没有敌意文件时不得凭空产生错误（防止"一律报错"式的假通过）。"""
    root = _make_repo(tmp_path, hostile=False)
    engine = Engine.open(tmp_path / "data", provider=_FakeEmbedding())
    handle, _ = engine.resolve_repo(root)

    report = engine.ingest_repo(handle.project_id, root)

    _assert_isolated(report, hostile=False, parsed=1, chunks=1)


def test_read_failure_does_not_lose_other_files_in_change_set(tmp_path: Path) -> None:
    """变更集路径的隔离回归：``plan_scan`` 读不到的文件之外，其余文件仍进入 ChangeSet。"""
    root = _make_repo(tmp_path, hostile=True)
    engine = Engine.open(tmp_path / "data", provider=_FakeEmbedding())
    handle, _ = engine.resolve_repo(root)

    engine.ingest_repo(handle.project_id, root)  # 第一次：建立扫描状态

    # 第二次 ingest：所有内容未变 → 无变更、无错误（敌意文件不进扫描状态，也不该反复报错）
    again = engine.ingest_repo(handle.project_id, root)
    assert again.chunks_new == 0
    assert again.files_parsed == 0

    # 改一个正常文件：增量必须照常工作（证明状态没被敌意文件污染）
    (root / "src" / GOOD_NAME).write_text(
        GOOD_SOURCE.replace("return 1", "return 11"), encoding="utf-8"
    )
    delta = engine.ingest_repo(handle.project_id, root)
    assert delta.files_parsed == 1
    assert delta.chunks_new == 1
