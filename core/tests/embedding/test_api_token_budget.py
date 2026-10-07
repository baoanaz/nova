"""TASK-046 §D 单元测试：API 侧的按 token 截断、按 token 预算分批、限流韧性。

全程 httpx MockTransport（无网络）。截断相关的用例注入微型 tokenizer，使「每次重新分词会
多出若干 token」的行为可构造、可判定（这正是真机 XLM-R 边界抖动的可测试形态）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from nova_core.embedding.api import (
    DEFAULT_BATCH_TOKEN_BUDGET,
    OpenAiCompatibleEmbeddingProvider,
    _truncate_by_bytes,
    _truncate_with_tokenizer,
    iter_batches_by_token_budget,
)
from nova_core.embedding.base import ApiRateLimitError, EmbeddingConfigError
from nova_core.embedding.registry import ApiModelSpec
from tokenizers import Tokenizer, models, pre_tokenizers

BASE_URL = "https://api.example.com"


def embedding_body(count: int, dim: int = 4) -> dict:
    return {"data": [{"index": i, "embedding": [1.0, 0.0, 0.0, 0.0]} for i in range(count)]}


def make_provider(
    handler, *, spec: ApiModelSpec, tokenizer: Tokenizer | None = None, **kwargs
) -> tuple[OpenAiCompatibleEmbeddingProvider, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    provider = OpenAiCompatibleEmbeddingProvider(
        spec,
        base_url=BASE_URL,
        api_key="sk-test",
        client=client,
        sleep=lambda _seconds: None,
        tokenizer=tokenizer,
        **kwargs,
    )
    return provider, requests


def echo_handler(request: httpx.Request) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    return httpx.Response(200, json=embedding_body(len(inputs)))


# --------------------------------------------------------------------------
# 截断
# --------------------------------------------------------------------------


def test_truncate_by_bytes_keeps_prefix_and_is_byte_safe() -> None:
    text = "a" * 100
    assert _truncate_by_bytes(text, 10) == "a" * 10
    assert _truncate_by_bytes("abc", 100) == "abc"
    # 多字节字符不得被截成半个（否则会变成 U+FFFD 噪声）
    chinese = "中文测试" * 10
    out = _truncate_by_bytes(chinese, 7)
    assert len(out.encode("utf-8")) <= 7
    assert "�" not in out


def test_truncate_with_tokenizer_fills_up_to_limit(tmp_path: Path) -> None:
    """正常 tokenizer：截断后 token 数应逼近上限，而不是退到字节退路（中文欠填回归）。"""
    tokenizer = Tokenizer(models.WordLevel(vocab={"[UNK]": 0, "word": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()

    text = " ".join(["word"] * 5000)
    out = _truncate_with_tokenizer(text, 100, tokenizer)
    assert len(tokenizer.encode(out).ids) <= 100
    assert len(tokenizer.encode(out).ids) >= 90  # 必须填满，不能只剩一小截


def test_truncate_converges_when_reencoding_grows(tmp_path: Path) -> None:
    """构造「去尾后重新分词反而更长」的 tokenizer，断言回验循环收敛且不超上限。"""

    class GreedyTokenizer:
        """每次编码都追加一个 token，模拟边界抖动；截断必须回验并收敛。"""

        def __init__(self, limit: int) -> None:
            self._limit = limit

        def encode(self, text: str):
            pieces = [p for p in text.split() if p]
            ids = list(range(min(len(pieces) * 2, 10_000)))
            return type("Enc", (), {"ids": ids})()

        def decode(self, ids, skip_special_tokens: bool = False) -> str:
            # 每个 id 解出一个词，重编码后数量会翻倍（模拟抖动放大）
            return " ".join(["w"] * len(ids))

    tokenizer = GreedyTokenizer(limit=100)
    out = _truncate_with_tokenizer(" ".join(["w"] * 500), 64, tokenizer)  # type: ignore[arg-type]
    assert tokenizer.encode(out).ids
    assert len(tokenizer.encode(out).ids) <= 64


def test_oversized_input_is_truncated_and_never_reaches_api_unshrunk() -> None:
    """超长输入不得以原始长度送到 API（本卡阻断级回归）。"""
    spec = ApiModelSpec(name="bge-m3", dim=4, max_input_tokens=64, request_name="BAAI/bge-m3")
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        seen.append(len(inputs[0]))
        return httpx.Response(200, json=embedding_body(len(inputs)))

    provider, _ = make_provider(handler, spec=spec)
    huge = "x" * 100_000
    vectors = provider.embed([huge])
    assert len(vectors) == 1
    assert seen == [64]  # 字节退路（无 tokenizer）恰好截到 64 字节


# --------------------------------------------------------------------------
# 按 token 预算分批
# --------------------------------------------------------------------------


def test_iter_batches_respects_token_budget() -> None:
    items = ["a" * 100, "b" * 100, "c" * 100, "d" * 100]
    batches = list(
        iter_batches_by_token_budget(
            items, batch_size=64, token_budget=250, count_tokens=lambda t: len(t)
        )
    )
    assert batches == [["a" * 100, "b" * 100], ["c" * 100, "d" * 100]]
    for batch in batches:
        assert sum(len(item) for item in batch) <= 250


def test_iter_batches_still_respects_count_limit() -> None:
    items = ["a"] * 10
    batches = list(
        iter_batches_by_token_budget(
            items, batch_size=3, token_budget=10_000, count_tokens=lambda t: len(t)
        )
    )
    assert [len(batch) for batch in batches] == [3, 3, 3, 1]


def test_single_item_over_budget_becomes_its_own_batch() -> None:
    """单条超预算的输入单独成批（不拆、不丢、不死循环）。"""
    items = ["small", "x" * 500, "small"]
    batches = list(
        iter_batches_by_token_budget(
            items, batch_size=64, token_budget=100, count_tokens=lambda t: len(t)
        )
    )
    assert batches == [["small"], ["x" * 500], ["small"]]


def test_iter_batches_rejects_invalid_budget() -> None:
    with pytest.raises(EmbeddingConfigError, match="token_budget"):
        list(
            iter_batches_by_token_budget(
                ["a"], batch_size=1, token_budget=0, count_tokens=lambda t: 1
            )
        )


def test_provider_splits_batches_by_token_budget() -> None:
    spec = ApiModelSpec(name="bge-m3", dim=4, max_input_tokens=10_000)
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        sizes.append(len(inputs))
        return httpx.Response(200, json=embedding_body(len(inputs)))

    provider, requests = make_provider(handler, spec=spec, batch_size=64, batch_token_budget=100)
    # 10 条各 60 字节：每批最多 1 条（60+60 > 100）
    provider.embed(["x" * 60] * 10)
    assert len(requests) == 10
    assert sizes == [1] * 10


def test_provider_default_budget_is_documented_value() -> None:
    assert DEFAULT_BATCH_TOKEN_BUDGET == 8192


def test_provider_rejects_invalid_budget() -> None:
    spec = ApiModelSpec(name="bge-m3", dim=4)
    with pytest.raises(EmbeddingConfigError, match="batch_token_budget"):
        OpenAiCompatibleEmbeddingProvider(spec, base_url=BASE_URL, batch_token_budget=0)


# --------------------------------------------------------------------------
# 请求体 model 字段（§A 防回归）
# --------------------------------------------------------------------------


def test_request_model_field_uses_provider_name() -> None:
    """发给 API 的 model 必须是 provider 认的名字，而非 nova 的注册表 key。"""
    spec = ApiModelSpec(name="bge-m3", dim=4, request_name="BAAI/bge-m3")
    provider, requests = make_provider(echo_handler, spec=spec)
    provider.embed(["hello"])
    assert json.loads(requests[0].content)["model"] == "BAAI/bge-m3"


def test_request_model_field_falls_back_to_name() -> None:
    spec = ApiModelSpec(name="text-embedding-3-small", dim=4)
    provider, requests = make_provider(echo_handler, spec=spec)
    provider.embed(["hello"])
    assert json.loads(requests[0].content)["model"] == "text-embedding-3-small"


# --------------------------------------------------------------------------
# 限流韧性
# --------------------------------------------------------------------------


def test_rate_limit_is_retried_then_succeeds() -> None:
    """429 后成功：断言会重试并最终返回（不把瞬时限流升级为失败）。"""
    spec = ApiModelSpec(name="bge-m3", dim=4)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, text='{"message":"TPM limit reached"}')
        return httpx.Response(200, json=embedding_body(1))

    provider, requests = make_provider(handler, spec=spec)
    assert len(provider.embed(["a"])) == 1
    assert attempts["n"] == 2
    assert len(requests) == 2


def test_rate_limit_exhausts_retries_without_infinite_loop() -> None:
    spec = ApiModelSpec(name="bge-m3", dim=4)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text='{"message":"TPM limit reached"}')

    provider, requests = make_provider(handler, spec=spec)
    with pytest.raises(ApiRateLimitError, match="429"):
        provider.embed(["a"])
    assert len(requests) == 3  # 1 + 2 次重试，有界
