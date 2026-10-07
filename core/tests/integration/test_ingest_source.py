"""TASK-031 §A 回归：``Engine.ingest(..., source=)`` 与"指纹失效静默清空向量索引"。

**立项理由（本文件存在的唯一原因）**：上传（blob）模式下索引内容全在 ``ChangeSet`` 里，
本地目录未绑定。配置指纹一级/二级失效（D-07）会走 ``full_reparse`` / ``reembed``，这两条
路径**遍历 ``source.list_files()`` 重建**；若 service 不传 ``source``，内部退化为
``_EmptySource``：

```text
库里有文件 + 指纹失效（如异常中断）→ FULL_REPARSE
 → _rebuild_vectors() 重建向量表（旧向量先删）
 → list_files() 为空 → 没有任何文件被重新解析/重嵌
 → 向量索引为空，SQLite 分块仍在，且指纹被改写成"一致"→ 不再报错、不再重试（静默）
```

实测口径（2026-09-10，本文件与执行记录同源）：
- **不是**"chunks 表被清空"——SQLite 侧 files/chunks/symbols 原样保留，真正的损失是
  **向量索引被清空**，检索退化为单通道（``answerable`` 由 True 掉到 False）；
- 恢复方式只能是"再一次传入 ``source`` 触发重建"，而指纹已被改写，需要新的失效事件才会
  再走这条路径——这就是"静默"的代价。

两条断言都必须存在：``source=None`` → 清空（错误行为）；``source=BlobSource`` → 正常重建
（正确行为）。CI 不联网、不加载模型（确定性假 embedding，见 ``conftest``）。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from nova_core.chunking import PARSER_CONFIG_KEY
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_core.storage import Store
from nova_core.types import BlobInput, ChangeSet
from nova_core.vectors import VectorStore

from .conftest import DESIGN_DOC, QUERY, TOKEN_MODULE, DeterministicBigramEmbedding

#: 上传文件集（2 个文件：一个 python 模块 + 一份 markdown 设计文档）。
UPLOADED_FILES: dict[str, bytes] = {
    "src/token_service.py": TOKEN_MODULE.encode("utf-8"),
    "docs/token.md": DESIGN_DOC.encode("utf-8"),
}
#: 伪造的"坏指纹"（任何与当前指纹不同的值都触发 FULL_REPARSE）。
BROKEN_FINGERPRINT = "broken-parser-config"


class LedgerSource:
    """按"账本 + 镜像"读取的内存 source（语义等价于 service 的 ``BlobSource``）。

    存在的意义是让本测试停在 core 边界内：只验证 ``Engine.ingest(source=...)`` 是否正确
    接住外部 source，不依赖 service 包。
    """

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)

    def read(self, path: str) -> bytes:
        try:
            return self._files[path]
        except KeyError:  # SourceProvider 口径：缺失即 FileNotFoundError
            raise FileNotFoundError(path) from None

    def list_files(self) -> tuple[str, ...]:
        return tuple(sorted(self._files))


def _change_set(files: dict[str, bytes]) -> ChangeSet:
    blobs = tuple(
        BlobInput(path=path, content=data, blob_hash=blob_hash(path, data))
        for path, data in files.items()
    )
    return ChangeSet(added=blobs)


def _counts(engine: Engine, project_id: str) -> dict[str, int]:
    with Store.open(engine.project_dir(project_id)) as store:
        return store.counts()


def _vector_count(engine: Engine, project_id: str) -> int:
    provider = engine.provider
    with VectorStore.open(engine.project_dir(project_id), provider.profile.dim) as vectors:
        return vectors.count()


def _fingerprint(engine: Engine, project_id: str) -> str | None:
    with Store.open(engine.project_dir(project_id)) as store:
        return store.get_config(PARSER_CONFIG_KEY)


def _break_fingerprint(engine: Engine, project_id: str) -> None:
    """伪造指纹不一致：下一次 ``ingest`` 必然判 FULL_REPARSE（D-07 一级失效）。"""
    with Store.open(engine.project_dir(project_id)) as store:
        store.set_config(PARSER_CONFIG_KEY, BROKEN_FINGERPRINT)


def _uploaded(tmp_path: Path) -> tuple[Engine, str]:
    """上传模式的项目：内容只经 ``ChangeSet`` 进入引擎（本地目录未绑定）。"""
    provider = DeterministicBigramEmbedding()
    engine = Engine.open(tmp_path, provider=provider)
    project_id = engine.resolve_project("identity:uploaded-repo", "uploaded-demo").project_id
    engine.ingest(project_id, _change_set(UPLOADED_FILES))
    return engine, project_id


def _iter_paths(files: Iterable[str]) -> list[str]:
    return sorted(files)


def test_fingerprint_invalidation_without_source_wipes_vector_index(tmp_path: Path) -> None:
    """**错误行为（回归锚点）**：不传 ``source`` 时向量索引被清空且检索静默降级。"""
    engine, project_id = _uploaded(tmp_path)
    before = _counts(engine, project_id)
    vectors_before = _vector_count(engine, project_id)
    assert before["files"] == len(UPLOADED_FILES)
    assert before["chunks"] > 0
    assert vectors_before == before["chunks"], "基线：每个 chunk 都应有向量"
    assert engine.search(project_id, QUERY).answerable is True

    _break_fingerprint(engine, project_id)
    engine.ingest(project_id, ChangeSet())  # 不抛异常 → 错误是静默的
    assert _fingerprint(engine, project_id) != BROKEN_FINGERPRINT, (
        "指纹已被改写成'一致'：错误不会被重试，也不会再被发现"
    )

    after = _counts(engine, project_id)
    assert after["chunks"] == before["chunks"], "SQLite 分块仍在（损失不在这一侧）"
    assert _vector_count(engine, project_id) == 0, "向量索引被清空：这就是'静默清空'的可观测面"
    assert engine.search(project_id, QUERY).answerable is False, "检索质量静默退化到不可用"


def test_fingerprint_invalidation_with_source_rebuilds(tmp_path: Path) -> None:
    """**正确行为（修复目标）**：同一指纹失效下传入 ``source`` → 正常重解析与重建向量。"""
    engine, project_id = _uploaded(tmp_path)
    before = _counts(engine, project_id)
    vectors_before = _vector_count(engine, project_id)

    _break_fingerprint(engine, project_id)
    engine.ingest(project_id, ChangeSet(), source=LedgerSource(UPLOADED_FILES))

    after = _counts(engine, project_id)
    assert after == before, "重建是幂等的：文件/分块/符号数不变，不重复写入"
    assert _vector_count(engine, project_id) == vectors_before == after["chunks"], (
        "向量索引被完整重建"
    )
    pack = engine.search(project_id, QUERY)
    assert pack.answerable is True, "重建后检索能力恢复"
    assert {item.path for item in [*pack.evidence, *pack.docs]} & set(
        _iter_paths(UPLOADED_FILES)
    ), "检索应命回上传的文件"


def test_ingest_without_source_keeps_working_for_changeset_only_writes(tmp_path: Path) -> None:
    """不传 ``source`` 的增量（指纹未失效）仍正常——本卡不改 CLI/本地目录路径的语义。"""
    engine, project_id = _uploaded(tmp_path)
    before = _counts(engine, project_id)
    updated = dict(UPLOADED_FILES)
    updated["src/token_service.py"] = TOKEN_MODULE.replace('return "old"', 'return "new"').encode()
    engine.ingest(project_id, ChangeSet(modified=_change_set(updated).added))

    after = _counts(engine, project_id)
    assert after["files"] == before["files"]
    assert after["chunks"] == before["chunks"]
    assert _vector_count(engine, project_id) > 0
