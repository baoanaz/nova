"""TASK-035 验收：provider 健康、错误映射（503/507/500）与空索引三岔口。

覆盖卡内三层：

- §A：引擎/依赖类异常 → ``embedding_unavailable`` / ``embedding_unreachable``（503）、
  ``storage_error``（507）；**未预期异常仍是 500 ``internal_error``**（兜底不被吞）；
- §B：空索引三岔口（从未同步→409 / 有账本但空→500 ``index_failed`` / provider 坏→503）；
- §C：service 侧不再出现 ``engine._ingest``（core 侧有 ``apply_changes`` 的集成测试）。

纪律：**不联网、不加载模型**（假 provider / 注入的故障 provider），断言 provider 摘要进响应
但 **API key 不进响应**。
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.embedding import ApiNetworkError, EmbeddingConfigError, EmbeddingError
from nova_core.engine import Engine, EngineError
from nova_core.hashing import blob_hash
from nova_core.interfaces import EmbeddingProfile
from nova_core.types import BlobInput, ChangeSet
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.errors import (
    CODE_EMBEDDING_UNAVAILABLE,
    CODE_EMBEDDING_UNREACHABLE,
    CODE_STORAGE_ERROR,
    map_engine_error,
)
from nova_service.runtime import EngineManager

from tests.conftest import (
    REPO_ROOT,
    SAMPLE_FILES,
    TARGET_SYMBOL,
    DeterministicBigramEmbedding,
    make_client,
)

#: 一个**假的** API key：它只出现在（被映射的）provider 异常文本里，响应中不得出现。
FAKE_API_KEY = "sk-live-SHOULD-NOT-LEAK-abc123"
#: embedding 服务地址（不可达；本测试不真的连接它）。
UNREACHABLE_BASE_URL = "http://127.0.0.1:9"


def _settings(data_root: Path) -> Settings:
    """测试用配置（临时 data_root + 本地模式）。"""
    return Settings(data_root=data_root, local_mode=True)


def _change_set(files: dict[str, str]) -> ChangeSet:
    """把源码字符串变成 core 的 ``ChangeSet``（provider 会真的被调用）。"""
    blobs = []
    for path, content in files.items():
        data = content.encode("utf-8")
        blobs.append(BlobInput(path=path, content=data, blob_hash=blob_hash(path, data)))
    return ChangeSet(added=tuple(blobs))


# --------------------------------------------------------------------------- 故障 provider


class _BrokenProvider:
    """embed 时抛指定异常的 provider（模拟 provider 不可达/配置坏，不联网）。"""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self._profile = EmbeddingProfile(model_id="fake:broken", dim=64, max_input_tokens=512)

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise self._error

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        raise self._error


def _app_with_provider(data_root: Path, provider: object) -> FastAPI:
    """注入任意 provider 的应用（每个测试独立 data_root）。"""
    settings = _settings(data_root)
    app = create_app(settings)
    app.state.engine_manager = _manager_with_provider(settings.data_root, provider)
    return app


def _manager_with_provider(data_root: Path, provider: object) -> EngineManager:
    return EngineManager.open(
        data_root,
        engine_factory=lambda root: Engine.open(root, provider=provider),  # type: ignore[arg-type]
    )


@pytest.fixture
def network_down(tmp_path: Path) -> Iterator[tuple[TestClient, EngineManager]]:
    """provider 调用时抛 ``ApiNetworkError``（含明文 API key，用于断言不泄漏）。"""
    error = ApiNetworkError(
        f"embedding 请求网络失败（ConnectError，endpoint={UNREACHABLE_BASE_URL}，尝试 3/3）："
        f"api_key={FAKE_API_KEY}"
    )
    manager = _manager_with_provider(tmp_path / "data", _BrokenProvider(error))
    app = create_app(_settings(tmp_path / "data"))
    app.state.engine_manager = manager
    try:
        with make_client(app) as client:
            yield client, manager
    finally:
        manager.close()


@pytest.fixture
def config_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, EngineManager]]:
    """provider **构造**失败（``EMBED_MODE=api`` 缺 ``EMBED_MODEL``/``EMBED_BASE_URL``）的应用。

    真工厂读环境变量，构造在发请求之前就失败 → 整个测试**不触网**。这也是 TASK-035 卡内
    复现命令（``EMBED_MODE=api EMBED_BASE_URL=http://127.0.0.1:9``，未给 ``EMBED_MODEL``）的
    失败点：provider 构造失败 → 索引什么都没写（chunks=0）。
    """
    monkeypatch.setenv("EMBED_MODE", "api")
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    settings = _settings(tmp_path / "data")
    manager = EngineManager.open(settings.data_root)
    app = create_app(settings)
    app.state.engine_manager = manager
    try:
        with make_client(app) as client:
            yield client, manager
    finally:
        manager.close()


def _upload(client: TestClient, project_id: str, files: dict[str, str]) -> httpx.Response:
    """走真实 ``POST /api/sync/batch-upload``（blobHash 按 CF-02 计算）。"""
    blobs = [
        {
            "path": path,
            "blobHash": blob_hash(path, content.encode("utf-8")),
            "contentB64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        for path, content in files.items()
    ]
    return client.post(
        "/api/sync/batch-upload", json={"projectId": project_id, "blobs": blobs}
    )


def _resolve(client: TestClient, identity: str = "identity:error-mapping") -> str:
    response = client.post("/api/projects/resolve", json={"identityKey": identity})
    assert response.status_code == 200, response.text
    return str(response.json()["projectId"])


# --------------------------------------------------------------------------- §A 映射


def test_unreachable_provider_upload_returns_503_not_500(
    network_down: tuple[TestClient, EngineManager],
) -> None:
    """provider 不可达：``batch-upload`` → 503 ``embedding_unreachable``（非 500），不泄漏 key。"""
    client, _manager = network_down
    project_id = _resolve(client)

    response = _upload(client, project_id, SAMPLE_FILES)

    assert response.status_code == 503, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == CODE_EMBEDDING_UNREACHABLE
    message = body["error"]["message"]
    assert "请确认 embedding 服务地址可达" in message, "响应必须给可操作指引"
    assert UNREACHABLE_BASE_URL in message, "保留 provider 侧 error 摘要（便于定位）"
    assert FAKE_API_KEY not in response.text, "API key 绝不能进响应"
    assert "***" in message, "provider 摘要里的凭据被脱敏"


def test_failed_upload_then_search_is_degraded_not_409(
    network_down: tuple[TestClient, EngineManager],
) -> None:
    """**不再误导**：上传失败后的 search 绝不是 409「请先同步」。

    实测口径（见执行记录）：provider 在 **embed 阶段**失败时，分块已落库（``chunks=2``），
    于是检索照常跑，但向量通道降级（``channelsUsed=["exact","bm25"]``、``degraded=true``、
    ``degradedReason`` 写明根因）——这比拿 503 把一份可用的 BM25/精确结果也拦下来更诚实（D-30）。
    卡内复现命令（provider 构造失败 → chunks=0）的 503 由下面的 ``config_broken`` 测试锁定。
    """
    client, manager = network_down
    project_id = _resolve(client)
    assert _upload(client, project_id, SAMPLE_FILES).status_code == 503  # 账本/分块已落盘

    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )

    assert response.status_code != 409, response.text
    assert response.status_code == 200, response.text
    meta = response.json()["meta"]
    assert meta["degraded"] is True
    assert meta["degradedReason"] and "embedding" in meta["degradedReason"]
    assert "vector" in meta["degradedReason"]
    assert meta["channelsUsed"] and "vector" not in meta["channelsUsed"]
    assert manager.provider_health()[0] is False, "provider 故障已被记住（非空索引不再依赖它拦截）"
    assert FAKE_API_KEY not in response.text, "降级原因与错误响应同一纪律：secret 不进响应"
    assert "***" in meta["degradedReason"]


def test_bare_httpx_error_maps_to_unreachable(tmp_path: Path) -> None:
    """未被包装的 ``httpx`` 异常同样映射成 503 ``embedding_unreachable``。"""
    provider = _BrokenProvider(httpx.ConnectError("连接 127.0.0.1:9 被拒绝"))
    manager = _manager_with_provider(tmp_path / "data", provider)
    app = create_app(_settings(tmp_path / "data"))
    app.state.engine_manager = manager
    try:
        with make_client(app) as client:
            response = _upload(client, _resolve(client), SAMPLE_FILES)
    finally:
        manager.close()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == CODE_EMBEDDING_UNREACHABLE


def test_provider_config_error_maps_to_unavailable(
    config_broken: tuple[TestClient, EngineManager],
) -> None:
    """**卡内复现命令**：provider 配置非法（``EMBED_MODE=api`` 缺 model/base_url）
    → ``batch-upload`` 503 ``embedding_unavailable``、``search`` 503（不是 500/409）。

    不走假的 provider：让 core 真正构造失败（``EmbeddingConfigError`` → ``EngineError``），
    **不触网**（构造在发请求之前就失败）。
    """
    client, _manager = config_broken
    project_id = _resolve(client)
    upload = _upload(client, project_id, SAMPLE_FILES)
    search = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )
    assert upload.status_code == 503, upload.text
    assert upload.json()["error"]["code"] == CODE_EMBEDDING_UNAVAILABLE
    assert "EMBED_MODEL" in upload.json()["error"]["message"]
    assert search.status_code == 503, search.text
    assert search.json()["error"]["code"] == CODE_EMBEDDING_UNAVAILABLE
    assert "这不是「尚未同步」" in search.json()["error"]["message"]


def test_engine_error_without_provider_cause_is_not_mapped() -> None:
    """``EngineError`` 不带 embedding 故障链（参数类错误）**不得**被误报成 503。"""
    assert map_engine_error(EngineError("非法 project_id：'x'（期望 16 位小写十六进制）")) is None
    wrapped = EngineError("embedding provider 不可用：EmbeddingConfigError: boom")
    wrapped.__cause__ = EmbeddingConfigError("api 模式必须指定 model")
    mapped = map_engine_error(wrapped)
    assert mapped is not None
    assert (mapped.code, mapped.status) == (CODE_EMBEDDING_UNAVAILABLE, 503)


def test_storage_error_maps_to_507(
    client: TestClient, project_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """磁盘/权限类 ``OSError`` → 507 ``storage_error``（code 具体，不吞成通用 500）。"""

    def boom(self: EngineManager, project_id: str) -> dict:
        raise PermissionError(f"[Errno 13] Permission denied: '{project_id}/index.db'")

    monkeypatch.setattr(EngineManager, "sync_status", boom)
    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )

    assert response.status_code == 507, response.text
    assert response.json()["error"]["code"] == CODE_STORAGE_ERROR
    assert "读写权限" in response.json()["error"]["message"]


def test_unexpected_exception_still_500_internal_error(
    client: TestClient, project_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：错误映射不得吞掉兜底——非依赖类异常仍是 500 ``internal_error``。"""

    def boom(self: EngineManager, project_id: str) -> dict:
        raise RuntimeError("boom-secret-detail")

    monkeypatch.setattr(EngineManager, "sync_status", boom)
    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "boom-secret-detail" not in response.text


# --------------------------------------------------------------------------- §B 空索引三岔口


def test_empty_index_never_synced_is_409(client: TestClient) -> None:
    """① 从未同步（账本为空 + chunks=0）→ 409 ``index_in_progress``（现状语义不变）。"""
    project_id = _resolve(client)
    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "index_in_progress"
    assert "请先同步" in response.json()["error"]["message"]


def test_empty_index_with_ledger_is_500_index_failed(
    client: TestClient, engine_manager: EngineManager, project_id: str
) -> None:
    """② 有账本但索引为空（上次索引失败）→ 500 ``index_failed``，**不得**再说「请先同步」。"""
    _record_ledger_only(engine_manager, project_id)

    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )

    assert response.status_code == 500, response.text
    body = response.json()
    assert body["error"]["code"] == "index_failed"
    assert "请先同步" not in body["error"]["message"]
    assert "重新同步" in body["error"]["message"]
    assert str(len(SAMPLE_FILES)) in body["error"]["message"]


def _record_ledger_only(manager: EngineManager, project_id: str) -> None:
    """只落账本与 blob 镜像、**不索引**（= 上次上传/索引失败后的真实磁盘状态）。"""
    blobs, state = manager.project_paths(project_id)
    for path, content in SAMPLE_FILES.items():
        data = content.encode("utf-8")
        digest = blob_hash(path, data)
        blobs.put(path, digest, data)
        state.record_file(path, digest, len(data))
    state.save()


def test_empty_index_with_broken_provider_is_503(
    config_broken: tuple[TestClient, EngineManager],
) -> None:
    """③ provider 坏 → 503（**优先于**前两者：根因优先，客户端不会陷入重试循环）。

    这里是「从未同步 + provider 坏」的组合：仍是 503（不是 409）。
    """
    client, _manager = config_broken
    project_id = _resolve(client)
    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == CODE_EMBEDDING_UNAVAILABLE


def test_empty_index_provider_bad_beats_index_failed(
    config_broken: tuple[TestClient, EngineManager],
) -> None:
    """③' 有账本但索引为空 + provider 坏 → 503（不是 500 ``index_failed``）：根因优先。"""
    client, manager = config_broken
    project_id = _resolve(client)
    _record_ledger_only(manager, project_id)  # 账本非空、chunks=0（模拟上次索引失败）

    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == CODE_EMBEDDING_UNAVAILABLE


def test_non_empty_index_is_not_gated(client: TestClient, engine_manager: EngineManager) -> None:
    """有索引时三岔口不参与：正常 200（回归：不要把正常路径也拦下来）。"""
    from tests.conftest import upload_files

    project_id = engine_manager.resolve_project("identity:indexed", "indexed").project_id
    upload_files(engine_manager, project_id, SAMPLE_FILES)
    response = client.post(
        "/api/query/search", json={"projectId": project_id, "query": TARGET_SYMBOL}
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- provider_health


def test_provider_health_reports_failure_then_recovers(tmp_path: Path) -> None:
    """故障记忆：失败后如实报 not-ok，成功一次即恢复（不残留陈旧状态）。"""
    error = ApiNetworkError("embedding 请求网络失败（ConnectError）")
    broken = _manager_with_provider(tmp_path / "data", _BrokenProvider(error))
    healthy = _manager_with_provider(tmp_path / "data2", DeterministicBigramEmbedding())
    broken_id = broken.resolve_project("identity:health", "health").project_id
    healthy_id = healthy.resolve_project("identity:health", "health").project_id
    try:
        assert broken.provider_health() == (True, None), "未调用过：配置合法即视为 ok"

        with pytest.raises(EmbeddingError):
            broken.ingest(broken_id, _change_set(SAMPLE_FILES))
        ok, reason = broken.provider_health()
        assert ok is False
        assert reason == "ApiNetworkError: embedding 请求网络失败（ConnectError）"

        broken._provider_error = "stale"  # noqa: SLF001 - 直接摆一个陈旧记忆，验证成功会清空
        assert broken.provider_health() == (False, "stale")
        healthy.ingest(healthy_id, _change_set(SAMPLE_FILES))
        assert healthy.provider_health() == (True, None)
    finally:
        broken.close()
        healthy.close()


def test_provider_health_is_ok_for_loaded_real_engine(tmp_path: Path) -> None:
    """真工厂（默认本地 ONNX）在**不加载模型**的前提下也能报 ok（/healthz 毫秒级的前提）。"""
    manager = EngineManager.open(tmp_path / "data")
    try:
        ok, reason = manager.provider_health()
        assert (ok, reason) == (True, None), "配置合法且 provider 已构造：ok"
    finally:
        manager.close()


# --------------------------------------------------------------------------- §C 收敛


def test_service_does_not_call_private_engine_ingest() -> None:
    """§C：service 源码里不再出现 ``engine._ingest``（跨包调私有方法已收敛）。"""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "service" / "nova_service").rglob("*.py"))
        if "engine._ingest(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    assert hasattr(Engine, "apply_changes"), "core 侧公开入口必须存在"
