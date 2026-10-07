"""TASK-111：跨项目 embedding 缓存（``EmbeddingCache``）。

它存在的理由：分支进项目身份后，同一仓库每个分支都是独立项目；没有共享缓存时
"换分支 = 全量重嵌"（实测 lane-c 对 main 零复用）。本测试钉住三条纪律：
① 同内容跨项目命中；② 不同模型不混用；③ 缓存坏掉必须降级、不得让索引失败。
"""

from __future__ import annotations

import pytest
from nova_core.vectors.cache import EmbeddingCache, EmbeddingCacheError, cache_key

DIM = 4
MODEL = "api:test-model"


def test_same_content_hits_across_open_calls(tmp_path) -> None:
    """同一 (模型, 内容) 在**不同项目**（不同 open 调用）间命中。"""
    with EmbeddingCache.open(tmp_path, MODEL, DIM) as cache:
        assert cache.lookup(MODEL, ["h1"]) == {}
        cache.put(MODEL, {"h1": [1.0, 0.0, 0.0, 0.0]})

    with EmbeddingCache.open(tmp_path, MODEL, DIM) as reopened:
        assert reopened.lookup(MODEL, ["h1"]) == {"h1": [1.0, 0.0, 0.0, 0.0]}
        assert reopened.lookup(MODEL, ["h1", "absent"]) == {"h1": [1.0, 0.0, 0.0, 0.0]}


def test_different_models_use_separate_shards(tmp_path) -> None:
    """换模型 = 换分片目录：不同模型的同内容不得互相串用。"""
    with EmbeddingCache.open(tmp_path, MODEL, DIM) as cache:
        cache.put(MODEL, {"h1": [1.0, 0.0, 0.0, 0.0]})

    with EmbeddingCache.open(tmp_path, "api:other-model", DIM) as other:
        assert other.lookup("api:other-model", ["h1"]) == {}, "不同模型不得命中同一行"
        # 分片目录不同，即使传回原模型名也看不到（双重隔离，允许分片名碰撞时仍安全）。
        assert other.lookup(MODEL, ["h1"]) == {}


def test_same_shard_different_model_id_does_not_hit(tmp_path) -> None:
    """同一分片内，主键含模型名：换模型名不得命中已写行。

    为什么主键还要带模型名（分片已经按模型分了）：``_shard_name`` 把非字母数字归一为
    ``_``，因此 ``api:a-b`` 与 ``api/a_b`` 会落到**同一个分片目录**；主键带模型名才不会串用。
    """
    with EmbeddingCache.open(tmp_path, MODEL, DIM) as cache:
        cache.put(MODEL, {"h1": [1.0, 0.0, 0.0, 0.0]})
        assert cache.lookup("api:OTHER", ["h1"]) == {}
        assert cache.lookup(MODEL, ["h1"]) == {"h1": [1.0, 0.0, 0.0, 0.0]}


def test_upsert_overwrites_same_key(tmp_path) -> None:
    """同 key 重复写只留一行（缓存是幂等的并集）。"""
    with EmbeddingCache.open(tmp_path, MODEL, DIM) as cache:
        cache.put(MODEL, {"h1": [1.0, 0.0, 0.0, 0.0]})
        cache.put(MODEL, {"h1": [0.0, 1.0, 0.0, 0.0]})
        assert cache.count() == 1
        assert cache.lookup(MODEL, ["h1"]) == {"h1": [0.0, 1.0, 0.0, 0.0]}


def test_dimension_mismatch_is_reported(tmp_path) -> None:
    with EmbeddingCache.open(tmp_path, MODEL, DIM) as cache:
        with pytest.raises(EmbeddingCacheError, match="维度"):
            cache.put(MODEL, {"h1": [1.0, 0.0]})


def test_closed_cache_raises_cache_error(tmp_path) -> None:
    cache = EmbeddingCache.open(tmp_path, MODEL, DIM)
    cache.close()
    with pytest.raises(EmbeddingCacheError, match="已关闭"):
        cache.lookup(MODEL, ["h1"])


def test_cache_key_is_model_scoped() -> None:
    assert cache_key("m1", "h") != cache_key("m2", "h")
    assert cache_key("m1", "h").endswith("\x00h")
