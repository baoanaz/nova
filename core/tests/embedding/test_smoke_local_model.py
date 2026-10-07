"""真实模型冒烟（不进 CI 默认集；需联网下载模型文件）。

用法：

    NOVA_EMBED_SMOKE=1 uv run pytest core/tests/embedding/test_smoke_local_model.py -q -s

可调环境变量：
- ``NOVA_EMBED_SLUG``：模型 slug（默认 ``multilingual-e5-small``；可换 ``arctic-embed-xs``）；
- ``NOVA_EMBED_CACHE``：模型缓存目录（默认 ``/tmp/nova-embedding-cache``，**不落仓库**）。

CI 默认跳过（无网络、且模型文件体积远大于仓库允许的产物）；真实结果回填到
TASK-008 任务卡"执行记录"，供 TASK-015 bake-off 复用机器规格与耗时基线。
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest
from nova_core.embedding import create_provider, get_local_spec

SMOKE_ENABLED = os.environ.get("NOVA_EMBED_SMOKE") == "1"

pytestmark = pytest.mark.skipif(
    not SMOKE_ENABLED,
    reason="真实模型冒烟默认跳过；设 NOVA_EMBED_SMOKE=1 启用（需联网下载模型）",
)

#: 冒烟样本：一句中文 + 一行代码（卡内 DoD 要求）。
CHINESE = "token 过期后在哪里刷新"
CODE = "def refresh_token(self) -> str:"


def test_real_model_embeds_chinese_and_code() -> None:
    slug = os.environ.get("NOVA_EMBED_SLUG", "multilingual-e5-small")
    cache_dir = os.environ.get("NOVA_EMBED_CACHE", "/tmp/nova-embedding-cache")
    spec = get_local_spec(slug)

    started = time.perf_counter()
    provider = create_provider({"model": slug, "cache_dir": cache_dir, "preload": True})
    load_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    passages = provider.embed([CHINESE, CODE])
    passage_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    [query_vector] = provider.embed_query([CHINESE])
    query_ms = (time.perf_counter() - started) * 1000

    assert provider.profile.model_id == f"local:{slug}"
    assert provider.profile.dim == spec.dim
    assert provider.profile.max_input_tokens == spec.max_input_tokens
    for vector in [*passages, query_vector]:
        assert len(vector) == spec.dim
        assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5

    cosine = float(np.dot(passages[0], query_vector))
    # 批组合不得改变向量（pad 位必须保留 tokenizer 自带的 attention_mask）
    [alone] = provider.embed([CHINESE])
    batch_cosine = float(np.dot(alone, passages[0]))
    print(
        f"[smoke] slug={slug} dim={spec.dim} max_input_tokens={spec.max_input_tokens} "
        f"pooling={spec.pooling} cache={cache_dir}\n"
        f"[smoke] 加载 {load_ms:.0f}ms；passage 批量(2 条) {passage_ms:.0f}ms；"
        f"query 单条 {query_ms:.0f}ms\n"
        f"[smoke] passage/query 余弦（同文本不同前缀）={cosine:.6f}；"
        f"单条 vs 批内余弦={batch_cosine:.6f}"
    )
    assert batch_cosine > 0.9999  # 批组合不影响单条结果
    # e5 有前缀约定，两侧同文本向量不应完全一致（前缀确实生效）。
    if spec.query_prefix or spec.passage_prefix:
        assert cosine < 0.9999
