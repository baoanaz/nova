"""TASK-036 §D 回归：``chunks > 0`` 但向量索引为空时，降级必须**可见**（R41 附注）。

**立项理由**：TASK-031 实测过的"静默清空"形态（见 ``test_ingest_source.py`` 的成因分析）
此前**完全不可见**——``Engine.search_with_trace`` 的 ``degraded`` 只反映"向量通道调用失败/
provider 缺失"，通道调用成功但**表里一行都没有**时它照样返回 ``degraded=False``。
调用方（service / MCP 端点）据此无法区分"检索确实没有向量命中"与"向量索引已经不在了"。

本文件锁死三条语义（R41 只允许复用既有 ``degraded`` / ``degraded_reason`` 字段，
CF-03 ``ContextPack`` / CF-04 ``SyncStatus`` 的字段集不动）：

1. ``chunks > 0`` 且 ``vectors == 0`` → ``degraded=True``，reason 明确写出两边计数；
2. 健康索引（``vectors == chunks``）→ ``degraded`` 保持 False，不被误报；
3. 真空库（``chunks == 0``）→ **不**算降级：那是"还没索引"，由 ``answerable`` 表达，
   把它报成"通道坏了"会让首次索引前的查询得到误导性诊断。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from nova_core.chunking import PARSER_CONFIG_KEY
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_core.storage import Store
from nova_core.types import BlobInput, ChangeSet
from nova_core.vectors import VectorStore

from .conftest import DESIGN_DOC, QUERY, TOKEN_MODULE, DeterministicBigramEmbedding

#: 上传文件集（与 ``test_ingest_source.py`` 同语料，便于两处结论互相印证）。
UPLOADED_FILES: Mapping[str, str] = {
    "src/token_service.py": TOKEN_MODULE,
    "docs/token.md": DESIGN_DOC,
}
#: 伪造的"坏指纹"（任何与当前指纹不同的值都触发 FULL_REPARSE）。
BROKEN_FINGERPRINT = "broken-parser-config"
#: 向量索引为空的判据文案（R41 附注要求"说明向量索引为空（可能未重建）"）。
GAP_MARKER = "向量索引为空（可能未重建）"


def _uploaded(tmp_path: Path) -> tuple[Engine, str]:
    """建立一个"已索引完成"的上传模式项目（每个 chunk 都有向量）。"""
    engine = Engine.open(tmp_path, provider=DeterministicBigramEmbedding())
    project_id = engine.resolve_project("identity:vector-health", "vector-health-demo").project_id
    blobs = tuple(
        BlobInput(path=path, content=text.encode("utf-8"), blob_hash=blob_hash(path, text.encode()))
        for path, text in UPLOADED_FILES.items()
    )
    engine.ingest(project_id, ChangeSet(added=blobs))
    return engine, project_id


def _wipe_vectors(engine: Engine, project_id: str) -> None:
    """制造"静默清空"：伪造指纹不一致 → 下一次 ``ingest``（不传 source）走 FULL_REPARSE。"""
    with Store.open(engine.project_dir(project_id)) as store:
        store.set_config(PARSER_CONFIG_KEY, BROKEN_FINGERPRINT)
    engine.ingest(project_id, ChangeSet())


def _counts(engine: Engine, project_id: str) -> dict[str, int]:
    with Store.open(engine.project_dir(project_id)) as store:
        return store.counts()


def _vector_count(engine: Engine, project_id: str) -> int:
    provider = engine.provider
    with VectorStore.open(engine.project_dir(project_id), provider.profile.dim) as vectors:
        return vectors.count()


def test_healthy_index_is_not_reported_as_degraded(tmp_path: Path) -> None:
    """反向断言：向量索引完好时不得误报降级（否则断言 1 变成永远为真的空断言）。"""
    engine, project_id = _uploaded(tmp_path)
    counts = _counts(engine, project_id)
    assert counts["chunks"] > 0
    assert _vector_count(engine, project_id) == counts["chunks"]

    trace = engine.search_with_trace(project_id, QUERY)
    assert trace.degraded is False
    assert trace.degraded_reason is None
    assert trace.pack.answerable is True


def test_chunks_without_vectors_is_reported_as_degraded(tmp_path: Path) -> None:
    """**核心断言**：``chunks > 0`` + ``vectors == 0`` → ``degraded=True`` 且原因可读。"""
    engine, project_id = _uploaded(tmp_path)
    _wipe_vectors(engine, project_id)

    counts = _counts(engine, project_id)
    assert counts["chunks"] > 0, "SQLite 分块仍在（这就是'静默'的可观测面）"
    assert _vector_count(engine, project_id) == 0

    trace = engine.search_with_trace(project_id, QUERY)
    assert trace.degraded is True, "向量索引为空必须可见，不能只有 answerable 悄悄变 False"
    assert trace.degraded_reason is not None
    assert GAP_MARKER in trace.degraded_reason
    assert f"chunks={counts['chunks']}" in trace.degraded_reason
    assert "vectors=0" in trace.degraded_reason
    assert trace.pack.answerable is False, "降级与不可回答同时成立（诚实标注，不返回 503 硬错）"


def test_empty_index_is_not_reported_as_degraded(tmp_path: Path) -> None:
    """真空库（尚未索引）不算降级：不把"还没索引"伪装成"通道坏了"。"""
    engine = Engine.open(tmp_path, provider=DeterministicBigramEmbedding())
    project_id = engine.resolve_project("identity:empty-repo", "empty-demo").project_id
    assert _counts(engine, project_id)["chunks"] == 0

    trace = engine.search_with_trace(project_id, QUERY)
    assert trace.degraded is False
    assert trace.degraded_reason is None
    assert trace.pack.answerable is False


def test_gap_reason_is_appended_to_existing_degraded_reason(tmp_path: Path) -> None:
    """两处降级同时成立时：既有原因不被覆盖（通道降级与"向量为空"都要能读到）。"""
    engine, project_id = _uploaded(tmp_path)
    _wipe_vectors(engine, project_id)

    flaky = _QueryFailingEmbedding()
    engine_with_flaky = Engine.open(engine.data_root, provider=flaky)
    trace = engine_with_flaky.search_with_trace(project_id, QUERY)

    assert trace.degraded is True
    reason = trace.degraded_reason or ""
    assert "vector 通道降级" in reason, "向量通道异常的原因必须保留在前"
    assert GAP_MARKER in reason, "向量索引为空的原因必须追加在后"
    assert reason.index("vector 通道降级") < reason.index(GAP_MARKER)


class _QueryFailingEmbedding:
    """索引侧正常、查询侧必炸的 provider（制造"向量通道降级"且与空表共存）。

    只用于断言两条降级来源的拼接顺序；不修改任何引擎实现。
    """

    def __init__(self) -> None:
        self._inner = DeterministicBigramEmbedding()

    @property
    def profile(self):
        return self._inner.profile

    def embed(self, texts):
        return self._inner.embed(texts)

    def embed_query(self, texts):
        raise RuntimeError("query embedding unavailable (test double)")

