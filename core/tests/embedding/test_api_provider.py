"""api.py 单元测试：httpx MockTransport，全程无网络（TASK-008 DoD）。

覆盖：请求体（model/input 批）、响应 index 重排、归一化、重试与退避、Retry-After、
超时配置、错误分类（鉴权/限流/网络/响应）、key 脱敏、query 前缀。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import numpy as np
import pytest
from nova_core.embedding.api import (
    OpenAiCompatibleEmbeddingProvider,
    build_default_client,
    embeddings_endpoint,
)
from nova_core.embedding.base import (
    ApiAuthError,
    ApiNetworkError,
    ApiRateLimitError,
    ApiResponseError,
    EmbeddingDimMismatchError,
)
from nova_core.embedding.registry import API_MODELS, ApiModelSpec

SPEC = ApiModelSpec(name="bge-m3", dim=4, max_input_tokens=512)
API_KEY = "sk-test-secret-key-123456"
BASE_URL = "https://api.example.com"


def embedding_body(vectors: list[list[float]], *, reverse_index: bool = False) -> dict:
    rows = [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)]
    if reverse_index:
        rows.reverse()
    return {"data": rows}


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    spec: ApiModelSpec = SPEC,
    api_key: str | None = API_KEY,
    base_url: str = BASE_URL,
    sleeps: list[float] | None = None,
    **kwargs,
) -> tuple[OpenAiCompatibleEmbeddingProvider, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording_handler))
    recorded_sleeps = sleeps if sleeps is not None else []
    provider = OpenAiCompatibleEmbeddingProvider(
        spec,
        base_url=base_url,
        api_key=api_key,
        client=client,
        sleep=recorded_sleeps.append,
        **kwargs,
    )
    return provider, requests


def test_request_shape_and_normalization() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        vectors = [[3.0, 4.0, 0.0, 0.0], [0.0, 0.0, 5.0, 0.0]]
        return httpx.Response(200, json=embedding_body(vectors))

    provider, requests = make_provider(handler)
    vectors = provider.embed(["a", "b"])
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.example.com/v1/embeddings"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"model": "bge-m3", "input": ["a", "b"]}
    assert np.allclose(vectors[0], [0.6, 0.8, 0.0, 0.0], atol=1e-6)
    assert np.allclose(vectors[1], [0.0, 0.0, 1.0, 0.0], atol=1e-6)


def test_empty_input_makes_no_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("空输入不应发请求")

    provider, requests = make_provider(handler)
    assert provider.embed([]) == []
    assert requests == []


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.example.com", "https://api.example.com/v1/embeddings"),
        ("https://api.example.com/", "https://api.example.com/v1/embeddings"),
        ("https://api.example.com/v1", "https://api.example.com/v1/embeddings"),
        ("https://api.example.com/v1/", "https://api.example.com/v1/embeddings"),
    ],
)
def test_endpoint_construction_avoids_duplicated_v1(base_url: str, expected: str) -> None:
    assert embeddings_endpoint(base_url) == expected


def test_response_rows_are_reordered_by_index() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=embedding_body(
                [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], reverse_index=True
            ),
        )

    provider, _ = make_provider(handler)
    first, second = provider.embed(["first", "second"])
    assert first[0] == pytest.approx(1.0)
    assert second[1] == pytest.approx(1.0)


def test_retry_on_server_error_then_success() -> None:
    sleeps: list[float] = []
    attempts = {"count": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(500, json={"error": "boom"})
        vectors = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        return httpx.Response(200, json=embedding_body(vectors))

    provider, requests = make_provider(handler, sleeps=sleeps)
    vectors = provider.embed(["a", "b"])
    assert len(vectors) == 2
    assert len(requests) == 2
    assert sleeps == [0.5]  # 指数退避起点


def test_rate_limit_exhausts_retries_and_raises() -> None:
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    provider, requests = make_provider(handler, sleeps=sleeps)
    with pytest.raises(ApiRateLimitError) as excinfo:
        provider.embed(["a"])
    assert len(requests) == 3  # 1 次 + ≤2 次重试（卡内 §C）
    assert sleeps == [0.5, 1.0]
    assert "429" in str(excinfo.value)


def test_retry_after_header_is_honored() -> None:
    sleeps: list[float] = []
    attempts = {"count": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(200, json=embedding_body([[1.0, 0.0, 0.0, 0.0]]))

    provider, _ = make_provider(handler, sleeps=sleeps)
    provider.embed(["a"])
    assert sleeps == [2.0]


def test_auth_error_is_classified_and_key_never_leaks() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"invalid key {API_KEY}"}})

    provider, requests = make_provider(handler)
    with pytest.raises(ApiAuthError) as excinfo:
        provider.embed(["a"])
    assert len(requests) == 1  # 鉴权失败不重试
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(provider)
    assert repr(provider).endswith("api_key=***)")


def test_error_body_is_redacted() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"bad request: key={API_KEY}")

    provider, _ = make_provider(handler)
    with pytest.raises(ApiResponseError) as excinfo:
        provider.embed(["a"])
    message = str(excinfo.value)
    assert API_KEY not in message
    assert "***" in message


def test_network_error_is_classified_and_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    provider, requests = make_provider(handler)
    with pytest.raises(ApiNetworkError) as excinfo:
        provider.embed(["a"])
    assert len(requests) == 3
    assert "embedding" in str(excinfo.value)
    assert API_KEY not in str(excinfo.value)


def test_non_json_response_raises_readable_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    provider, _ = make_provider(handler)
    with pytest.raises(ApiResponseError, match="JSON"):
        provider.embed(["a"])


def test_row_count_mismatch_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=embedding_body([[1.0, 0.0, 0.0, 0.0]]))

    provider, _ = make_provider(handler)
    with pytest.raises(ApiResponseError, match="行数"):
        provider.embed(["a", "b"])


def test_dim_mismatch_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=embedding_body([[1.0, 0.0, 0.0]]))

    provider, _ = make_provider(handler)
    with pytest.raises(EmbeddingDimMismatchError, match="dim"):
        provider.embed(["a"])


def test_query_prefix_is_injected_for_models_with_convention() -> None:
    spec = ApiModelSpec(name="custom", dim=4, query_prefix="query: ", passage_prefix="passage: ")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=embedding_body([[1.0, 0.0, 0.0, 0.0]]))

    provider, requests = make_provider(handler, spec=spec)
    provider.embed(["hello"])
    provider.embed_query(["hello"])
    assert json.loads(requests[0].content)["input"] == ["passage: hello"]
    assert json.loads(requests[1].content)["input"] == ["query: hello"]


def test_batching_respects_batch_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        return httpx.Response(200, json=embedding_body([[1.0, 0.0, 0.0, 0.0]] * len(inputs)))

    provider, requests = make_provider(handler, batch_size=2)
    provider.embed(["a", "b", "c"])
    assert len(requests) == 2


def test_authorization_header_omitted_without_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=embedding_body([[1.0, 0.0, 0.0, 0.0]]))

    provider, requests = make_provider(handler, api_key=None)
    provider.embed(["a"])
    assert "authorization" not in requests[0].headers
    assert "api_key=None" in repr(provider)


def test_default_client_timeouts_match_card() -> None:
    client = build_default_client()
    try:
        assert client.timeout.connect == 10.0
        assert client.timeout.read == 60.0
    finally:
        client.close()


def test_close_only_closes_owned_client() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=embedding_body([[1.0, 0.0, 0.0, 0.0]]))

    provider, _ = make_provider(handler)
    provider.close()
    assert provider.embed(["a"])  # 注入的客户端不被 provider 关闭

    owned = OpenAiCompatibleEmbeddingProvider(
        API_MODELS["bge-m3"], base_url=BASE_URL, api_key=API_KEY
    )
    owned.close()
    assert owned._client.is_closed  # 断言自建客户端的释放行为（私有字段仅测试可见）
