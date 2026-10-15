"""限流与并发闸门：登录试密码、``ask`` 频率、索引并发、``local`` 模式的回环保护、默认管理员名。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from nova_core.engine import Engine
from nova_service.__main__ import _is_loopback, main
from nova_service.app import create_app
from nova_service.config import DEFAULT_ADMIN_NAME, Settings
from nova_service.ratelimit import SlidingWindowLimiter, enforce_ask_rate
from nova_service.runtime import EngineManager

from tests.conftest import DeterministicBigramEmbedding, make_client

PASSWORD = "correct-horse-battery"


def _cloud(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    settings = Settings(data_root=tmp_path / "data", **overrides)
    app = create_app(settings)
    app.state.engine_manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    return SimpleNamespace(app=app, client=make_client(app), settings=settings)


def test_sliding_window_limits_and_expires() -> None:
    now = [0.0]
    limiter = SlidingWindowLimiter(2, 60.0, clock=lambda: now[0])
    assert limiter.acquire("u") is None
    assert limiter.acquire("u") is None
    wait = limiter.acquire("u")
    assert wait is not None and 0 < wait <= 60.0
    assert limiter.acquire("other") is None, "按 key 隔离"
    now[0] = 61.0
    assert limiter.acquire("u") is None, "窗口过去后恢复"


def test_limiter_disabled_when_limit_is_zero() -> None:
    limiter = SlidingWindowLimiter(0, 60.0)
    assert all(limiter.acquire("u") is None for _ in range(100))
    enforce_ask_rate(limiter, "u")  # 不抛
    enforce_ask_rate(None, "u")


def test_login_is_throttled_after_repeated_failures(tmp_path: Path) -> None:
    ns = _cloud(tmp_path)
    with ns.client:
        ns.client.post("/api/auth/bootstrap", json={"name": "root", "password": PASSWORD})
        ns.client.cookies.clear()
        codes = [
            ns.client.post("/api/auth/login", json={"name": "root", "password": "wrong"})
            for _ in range(10)
        ]
        assert [r.status_code for r in codes[:8]] == [401] * 8
        blocked = codes[8]
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "too_many_attempts"
        # 被限流期间连正确密码也先拒绝（不给攻击者"试出来"的反馈）；其他账户名不受影响。
        right = ns.client.post("/api/auth/login", json={"name": "root", "password": PASSWORD})
        assert right.status_code == 429
        other = ns.client.post("/api/auth/login", json={"name": "nobody", "password": "x"})
        assert other.status_code == 401
    ns.app.state.engine_manager.close()


def test_successful_login_clears_failures(tmp_path: Path) -> None:
    ns = _cloud(tmp_path)
    with ns.client:
        ns.client.post("/api/auth/bootstrap", json={"name": "root", "password": PASSWORD})
        for _ in range(5):
            ns.client.post("/api/auth/login", json={"name": "root", "password": "wrong"})
        assert (
            ns.client.post(
                "/api/auth/login", json={"name": "root", "password": PASSWORD}
            ).status_code
            == 200
        )
        for _ in range(7):  # 成功后计数清零：再失败 7 次仍未触发上限
            assert (
                ns.client.post(
                    "/api/auth/login", json={"name": "root", "password": "wrong"}
                ).status_code
                == 401
            )
    ns.app.state.engine_manager.close()


def test_ask_is_rate_limited_per_user(tmp_path: Path) -> None:
    ns = _cloud(tmp_path, ask_rate_per_min=2)
    with ns.client:
        ns.client.post("/api/auth/bootstrap", json={"name": "root", "password": PASSWORD})
        project = ns.client.post("/api/projects/resolve", json={"identityKey": "rate"})
        body = {"projectId": project.json()["projectId"], "question": "令牌怎么刷新？"}
        # 项目尚未索引：限流检查在索引检查**之前**，所以前两次是 409，第三次才是 429。
        first = [ns.client.post("/api/query/ask", json=body).status_code for _ in range(2)]
        assert first == [409, 409]
        limited = ns.client.post("/api/query/ask", json=body)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        # search 不受 ask 限流影响（不调 LLM）。
        search = ns.client.post(
            "/api/query/search", json={"projectId": body["projectId"], "query": "令牌"}
        )
        assert search.status_code == 409
    ns.app.state.engine_manager.close()


def test_index_concurrency_is_bounded(tmp_path: Path) -> None:
    manager = EngineManager.open(
        tmp_path / "data",
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    manager.set_index_concurrency(1)
    running, peak, guard = [0], [0], threading.Lock()

    def fake_apply(*_args: object, **_kwargs: object) -> object:
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with guard:
            running[0] -= 1
        from nova_core.pipeline import IngestReport

        return IngestReport()

    manager.engine.apply_changes = fake_apply  # type: ignore[method-assign]
    from nova_core.types import ChangeSet

    projects = [manager.resolve_project(f"slot-{i}").project_id for i in range(3)]
    threads = [
        threading.Thread(target=manager.ingest, args=(pid, ChangeSet())) for pid in projects
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 1, "并发索引上限为 1 时，三个不同项目的 ingest 也必须串行"
    manager.close()


@pytest.mark.parametrize(
    "host,expected",
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False),
     ("192.168.1.5", False), ("example.com", False)],
)
def test_loopback_detection(host: str, expected: bool) -> None:
    assert _is_loopback(host) is expected


def test_local_mode_refuses_non_loopback_host(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    code = main(["local", "--repo", str(repo), "--host", "0.0.0.0",
                 "--data-root", str(tmp_path / "d")])
    assert code == 2
    assert "非回环" in capsys.readouterr().err


def test_default_admin_name_is_empty() -> None:
    """默认不按名字提权：避免自部署者被"作者本人的账户名"隐式提为管理员。"""
    assert DEFAULT_ADMIN_NAME == ""
    assert Settings.from_env({}).admin_name == ""
