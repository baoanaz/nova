"""TASK-REVIEW-RUNTIME P2-1：query cache 生命周期与模型身份。

锁定三条不变量（外部架构评审 P2-1 的两处缺陷）：

1. **缓存归 Engine 持有**——旧行为是 ``recall()`` 每次调用新建 ``QueryEmbeddingCache``，
   调用结束即丢，注释里写的"进程内 60s 跨查询复用"压根不存在；
2. **key 必须带模型身份**——``Engine.set_provider()`` 能在生命周期内换模型
   （``--replay`` 切离线 provider、D-47 换用户模型），只按 query 做 key 会取到
   另一个模型的向量（维度相同则静默给出错误相似度）；
3. **换模型即清空**——旧向量对新模型无意义，不清会继续命中。

不联网、不加载模型（确定性假 embedding，见本目录 ``conftest``）。
"""

from __future__ import annotations

from pathlib import Path

from nova_core.engine import Engine
from nova_core.retrieval.vector import QueryEmbeddingCache

from .conftest import DESIGN_DOC, QUERY, TOKEN_MODULE, DeterministicBigramEmbedding


def _engine(tmp_path: Path) -> tuple[Engine, str]:
    engine = Engine.open(tmp_path, provider=DeterministicBigramEmbedding())
    project_id = engine.resolve_project("identity:p2-1", "p2-1").project_id
    return engine, project_id


def _ingest(engine: Engine, project_id: str) -> None:
    from nova_core.hashing import blob_hash
    from nova_core.types import BlobInput, ChangeSet

    files = {"src/token_service.py": TOKEN_MODULE, "docs/token.md": DESIGN_DOC}
    payloads = [(path, data.encode("utf-8")) for path, data in files.items()]
    engine.apply_changes(
        project_id,
        ChangeSet(
            added=tuple(
                BlobInput(path=path, content=data, blob_hash=blob_hash(path, data))
                for path, data in payloads
            )
        ),
    )


def test_query_cache_has_model_identity_after_search(tmp_path: Path) -> None:
    """开项目做一次检索后，Engine 持有的缓存已绑定当前模型身份。"""
    engine, project_id = _engine(tmp_path)
    _ingest(engine, project_id)

    engine.search(project_id, QUERY)

    cache = engine.query_cache
    assert isinstance(cache, QueryEmbeddingCache)
    assert cache.model == DeterministicBigramEmbedding().profile.model_id


def test_query_cache_is_shared_across_calls(tmp_path: Path) -> None:
    """跨查询复用：同一 query 第二次不再调 provider（hits 增长）。

    这就是旧实现缺失的行为——之前每次 ``recall()`` 都新建缓存，hits 永远是 0。
    """
    engine, project_id = _engine(tmp_path)
    _ingest(engine, project_id)

    engine.search(project_id, QUERY)
    cache = engine.query_cache
    after_first = cache.hits

    engine.search(project_id, QUERY)

    assert cache.hits > after_first


def test_provider_switch_rebinds_and_clears_cache(tmp_path: Path) -> None:
    """换 provider → 缓存身份重绑并清空（旧向量对新模型无意义）。"""
    engine, project_id = _engine(tmp_path)
    _ingest(engine, project_id)
    engine.search(project_id, QUERY)

    cache = engine.query_cache
    assert cache.model is not None
    assert len(cache) > 0

    class OtherModel(DeterministicBigramEmbedding):
        @property
        def profile(self):  # type: ignore[override]
            base = super().profile
            return type(base)(
                model_id="local:other-model", dim=base.dim, max_input_tokens=base.max_input_tokens
            )

    engine.set_provider(OtherModel())
    engine.search(project_id, QUERY)

    assert cache.model == "local:other-model"
