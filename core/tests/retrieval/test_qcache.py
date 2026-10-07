"""``PersistentQueryVectorCache`` / ``CachedOnlyProvider`` 的单测（TASK-101 §F）。

覆盖四条纪律：读写的自证字段、未命中**必须可见地**降级、索引侧一律拒绝、模型/维度不符必须拒绝。
"""

from __future__ import annotations

import json

import pytest
from nova_core.retrieval.qcache import CACHE_SCHEMA, CachedOnlyProvider, PersistentQueryVectorCache
from nova_core.retrieval.vector import VectorChannelError


def test_round_trip_persists_vectors(tmp_path) -> None:
    path = tmp_path / "qvec.json"
    cache = PersistentQueryVectorCache(path)
    cache.put("token 过期", [0.1, 0.2])
    assert cache.put_all() == 1

    reloaded = PersistentQueryVectorCache(path)
    assert reloaded.get("token 过期") == [0.1, 0.2]
    assert reloaded.get("不存在") is None
    assert reloaded.misses == 1


def test_identity_fields_are_adopted_from_file(tmp_path) -> None:
    """回归：自证字段必须**读进对象**，否则后续 identity_mismatch 会误判为一致（实测踩过）。"""
    path = tmp_path / "qvec.json"
    cache = PersistentQueryVectorCache(path)
    cache.put("q", [1.0])
    cache.bind_identity(model="api:voyage-4-lite", dim=1024)
    cache.put_all()

    fresh = PersistentQueryVectorCache(path)
    assert fresh.identity_mismatch(model="api:voyage-4-lite", dim=1024) is None
    assert fresh.identity_mismatch(model="api:other", dim=1024) is not None
    assert fresh.identity_mismatch(model="api:voyage-4-lite", dim=384) is not None


def test_wrong_model_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "qvec.json"
    payload = {
        "schema": CACHE_SCHEMA,
        "embedding_model": "api:other",
        "dim": 8,
        "queries": {"q": [1.0]},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    cache = PersistentQueryVectorCache(path, model="api:voyage-4-lite", dim=8)
    assert len(cache) == 0  # 不符 → 当作空缓存，不抛错（缓存是加速手段）


def test_broken_file_is_not_fatal(tmp_path) -> None:
    path = tmp_path / "qvec.json"
    path.write_text("{ not json", encoding="utf-8")
    assert len(PersistentQueryVectorCache(path)) == 0


def test_cached_only_provider_hits_and_misses(tmp_path) -> None:
    cache = PersistentQueryVectorCache(tmp_path / "qvec.json")
    cache.put("known query", [0.5, 0.5])
    provider = CachedOnlyProvider(cache, model="api:voyage-4-lite", dim=2)

    assert provider.embed_query(["known query"]) == [[0.5, 0.5]]
    assert provider.profile.model_id == "api:voyage-4-lite"
    assert provider.profile.dim == 2

    with pytest.raises(VectorChannelError) as excinfo:
        provider.embed_query(["unknown query"])
    assert "未命中" in str(excinfo.value)


def test_cached_only_provider_refuses_index_side(tmp_path) -> None:
    """索引侧必须拒绝：拿缓存去建索引会写进一批"来源不明"的向量。"""
    cache = PersistentQueryVectorCache(tmp_path / "qvec.json")
    provider = CachedOnlyProvider(cache, model="api:x", dim=2)
    with pytest.raises(RuntimeError):
        provider.embed(["some text"])


def test_search_without_key_falls_back_to_offline(tmp_path, monkeypatch) -> None:
    """回归（用户实测暴露的真实缺陷）：无 key 的机器上 `search` **不该**报维度不匹配。

    上一版只让 `--replay` 以索引指纹为准，于是无 key 的 `search` 直接抛
    ``DimensionMismatchError``（期望 384 / 实际 1024）——离线机器连"随便问一句"都做不到。
    现在改为：真实 provider 不可用或维度与索引不符时**自动切离线**并打印提示。
    """
    from nova_core.cli import app as cli_app
    from nova_core.engine import Engine

    class _FakeProvider:
        def __init__(self, dim: int) -> None:
            from nova_core.interfaces import EmbeddingProfile

            self._profile = EmbeddingProfile(model_id="local:fake", dim=dim, max_input_tokens=512)

        @property
        def profile(self):
            return self._profile

        def embed(self, texts):  # pragma: no cover - 本用例不建索引
            raise AssertionError("不该走到索引侧")

        def embed_query(self, texts):  # pragma: no cover - 离线时不会被调用
            raise AssertionError("不该走到真实 provider")

    cache = PersistentQueryVectorCache(tmp_path / "qvec.json")
    cache.put("q", [0.0] * 8)
    cache.bind_identity(model="api:voyage-4-lite", dim=8)
    cache.put_all()

    engine = Engine.open(tmp_path / "data", provider=_FakeProvider(dim=384))
    # 索引指纹 8 维 vs 真实 provider 384 维 → 必须自动切离线，而不是抛错
    engine.set_query_cache(cache)
    monkeypatch.setattr(
        cli_app, "_index_embedding_fingerprint", lambda _engine, _pid: ("api:voyage-4-lite", 8)
    )
    cli_app._sync_cache_identity(engine, cache, project_id="p", replay=False)
    assert isinstance(engine.provider, CachedOnlyProvider)
