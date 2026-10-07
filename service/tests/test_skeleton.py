"""TASK-030 验收：CF-05 路径快照 / healthz / 错误信封 / requestId / 日志脱敏。

对应任务卡"验收标准（DoD）"逐条；每条测试的 docstring 标注它守的是哪一条。

后续卡说明：TASK-034（attach/rescan）、TASK-060（meta/me/bootstrap）、TASK-062/064
（overview/index-runs/index-stats/index-stats 汇总/usage summary）预授权的**新增路径**属
CF-05 的**扩展**，卡内已登记。本文件用 :data:`TASK_EXTENSION_PATHS` 把"允许多出哪些"写死：
除此之外路径集合仍必须与 ``docs/contracts/openapi.yaml`` **完全相等**
（多一个/少一个/改名依然失败）。
``docs/contracts/openapi.yaml`` 的同步由编排者执行（实施 AI 不改契约文件）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_service.__main__ import build_parser
from nova_service.config import Settings
from nova_service.errors import ApiError
from nova_service.logging import JsonFormatter, redact_text

from tests.conftest import make_app, make_client

# --------------------------------------------------------------------------- CF-05 路径快照

#: 各任务卡预授权的 CF-05 **扩展路径**（卡内登记；已同步进 ``openapi.yaml``）。
#:
#: **TASK-110 后为空**：此前累积的扩展（TASK-034 的 attach/rescan、TASK-060 的 meta/me/
#: bootstrap、TASK-062/064 的统计、TASK-090 的请求日志、TASK-099 的调用时间线与
#: 用户级 LLM 配置、TASK-110 的后台五模块）已**全部写入** ``docs/contracts/openapi.yaml``，
#: 因此路径集合现在与合同**逐字相等**，不需要任何例外。
#:
#: 保留这个空集合（而不是删掉机制）的原因：它把"合同先改、实现后跟"的流程留在原处——
#: 将来某张卡需要先落实现再等编排者同步合同时，往这里加一条即可，且仍然会把
#: "白名单外的任何差异"变成失败。**但首选做法是同时改合同**（TASK-110 就是这么做的：
#: 用户 2026-09-15 明确要求不留兼容余地，直接按最终形态改）。
TASK_EXTENSION_PATHS: frozenset[str] = frozenset()


def test_openapi_paths_match_cf05_contract(
    app: FastAPI, contract_paths: dict[str, set[str]]
) -> None:
    """CF-05 地基：应用暴露的路径集合 = 合同集合 + TASK-034 预授权扩展（其余完全相等）。

    路径多一个 / 少一个 / 改名（含路径参数名）都会失败，并在断言消息里给出两侧差异。
    """
    actual = set(app.openapi()["paths"])
    expected = set(contract_paths) | set(TASK_EXTENSION_PATHS)
    assert actual == expected, _path_diff(actual, expected)


def test_openapi_methods_match_cf05_contract(
    app: FastAPI, contract_paths: dict[str, set[str]]
) -> None:
    """每个路径上的 HTTP 方法集合也必须与 CF-05 一致（路径对了方法漂移同样破坏合同）。"""
    actual_paths = app.openapi()["paths"]
    mismatches = []
    for path, methods in sorted(contract_paths.items()):
        actual = {method.lower() for method in actual_paths.get(path, {})}
        if actual != methods:
            mismatches.append(f"{path}: 合同={sorted(methods)} 实现={sorted(actual)}")
    assert not mismatches, "方法与 CF-05 不一致：\n" + "\n".join(mismatches)


def _path_diff(actual: set[str], expected: set[str]) -> str:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    return (
        "路径集合与 CF-05 不一致（路径是冻结合同，不得增删改名）\n"
        f"  实现缺少（合同有、应用没有）：{missing}\n"
        f"  实现多出（应用有、合同没有）：{extra}"
    )


# --------------------------------------------------------------------------- healthz


def test_healthz_fields_and_defaults(client: TestClient) -> None:
    """``/healthz``：200 + 字段齐全；不加载 embedding 模型（无 provider 字段）。"""
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"]
    assert payload["localMode"] is True
    assert payload["auth"] == "disabled(local)"
    assert payload["core"] == {"importable": True}
    assert Path(payload["dataRoot"]).name == "data"


def test_healthz_deep_reports_provider_failure_without_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?deep=1``：provider 不可用时 200 + ``core.ok=false`` + reason（不得 500）。"""
    monkeypatch.setenv("EMBED_MODE", "api")  # api 模式缺 model/base_url → 构造即失败
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)

    response = client.get("/healthz", params={"deep": 1})
    assert response.status_code == 200
    core = response.json()["core"]
    assert core["importable"] is True
    assert core["ok"] is False
    assert core["reason"]


# --------------------------------------------------------------------------- 错误信封（CF-05）


def test_not_implemented_helper_still_builds_501_envelope() -> None:
    """``not_implemented`` 助手保留：501 + ``{"error":{"code":"not_implemented"}}``。

    TASK-060/062/064 已把 CF-05 里最后的占位端点（``/api/auth/*``、``/api/usage/**``）落地，
    因此不再有"路径存在但返 501"的路由；这个助手继续留给**未来**的新占位使用，
    这里直接验证它的信封形状（否则它会成为无人使用的死代码）。
    """
    from nova_service.errors import not_implemented

    error = not_implemented("某种能力", "TASK-XXX")
    assert error.status == 501
    assert error.code == "not_implemented"
    assert error.message


def test_unknown_path_uses_error_envelope(client: TestClient) -> None:
    """未知路径 404 也走同一信封（不得裸抛 Starlette 默认 detail 结构）。"""
    response = client.get("/api/definitely-not-a-route")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_api_error_400_and_409_shapes(tmp_path: Path) -> None:
    """构造抛 ``ApiError`` 的路由：状态码与信封形态正确（400 / 409）。"""
    app = make_app(tmp_path)

    @app.get("/_test/invalid")
    async def _invalid() -> None:
        raise ApiError("invalid_query", "query 不能为空", 400)

    @app.get("/_test/conflict")
    async def _conflict() -> None:
        raise ApiError("index_in_progress", "索引尚未就绪", 409)

    with make_client(app) as test_client:
        bad = test_client.get("/_test/invalid")
        conflict = test_client.get("/_test/conflict")
    assert bad.status_code == 400
    assert bad.json() == {"error": {"code": "invalid_query", "message": "query 不能为空"}}
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "index_in_progress"


def test_unhandled_exception_returns_500_without_stack(tmp_path: Path) -> None:
    """未捕获异常 → 500 ``internal_error``；响应不含堆栈/异常文本（细节只在日志）。"""
    app = make_app(tmp_path)

    @app.get("/_test/boom")
    async def _boom() -> None:
        raise RuntimeError("boom-secret-detail")

    with make_client(app) as test_client:
        response = test_client.get("/_test/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "boom-secret-detail" not in response.text
    assert "Traceback" not in response.text


# --------------------------------------------------------------------------- requestId 与日志脱敏


def test_request_id_is_echoed_and_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """响应带 ``X-Request-Id``，且同一 id 出现在日志行里（可检索）。"""
    with caplog.at_level(logging.INFO, logger="nova_service.app"):
        response = client.get("/healthz", headers={"X-Request-Id": "fixed-request-id"})
    assert response.headers["X-Request-Id"] == "fixed-request-id"

    records = [record for record in caplog.records if getattr(record, "requestId", None)]
    assert records, "访问日志里没有 requestId"
    formatted = [json.loads(JsonFormatter().format(record)) for record in records]
    matching = [line for line in formatted if line.get("requestId") == "fixed-request-id"]
    assert matching, f"日志里检索不到 requestId：{[line.get('requestId') for line in formatted]}"
    assert matching[0]["path"] == "/healthz"
    assert matching[0]["method"] == "GET"
    assert matching[0]["status"] == 200


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    """调用方不带 id 时服务端生成（非空、非固定值）。"""
    first = client.get("/healthz").headers["X-Request-Id"]
    second = client.get("/healthz").headers["X-Request-Id"]
    assert first and second and first != second


def test_logs_do_not_leak_authorization_or_cookie(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """日志脱敏：带 ``Authorization``/``Cookie`` 的请求不得把 secret 写进日志。"""
    secret = "secret-token-abcdef"
    with caplog.at_level(logging.INFO):
        response = client.get(
            "/healthz",
            headers={"Authorization": f"Bearer {secret}", "Cookie": f"nova_session={secret}"},
        )
    assert response.status_code == 200
    assert secret not in caplog.text
    assert not any(secret in str(record.__dict__) for record in caplog.records)
    # 请求体同样不进日志：占位路由会把 body 原样丢掉，日志里不应出现其内容。
    with caplog.at_level(logging.INFO):
        client.post("/api/query/search", json={"query": secret})
    assert secret not in caplog.text


def test_redact_text_masks_credentials() -> None:
    """脱敏函数本身的正例（防止"日志里碰巧没写 secret"式的假绿）。"""
    assert "secret-token" not in redact_text("Authorization: Bearer secret-token")
    assert "hunter2" not in redact_text('{"password": "hunter2"}')
    assert "sess-123" not in redact_text("cookie=sess-123; path=/")
    assert redact_text("plain message") == "plain message"


# --------------------------------------------------------------------------- 配置与 CLI


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NOVA_DATA_ROOT", str(tmp_path / "root"))
    resolved = Settings.from_env()
    assert resolved.data_root == tmp_path / "root"
    assert resolved.local_mode is False
    assert (resolved.host, resolved.port, resolved.log_level) == ("127.0.0.1", 8787, "info")


def test_deployment_mode_cannot_disable_full_auth_flow() -> None:
    """旧环境变量不再把普通部署切成缺少账户/API Key 的本地模式。"""
    resolved = Settings.from_env({"NOVA_LOCAL_MODE": "true", "NOVA_REGISTER_OPEN": "false"})
    assert resolved.local_mode is False
    assert resolved.auth_required is True


def test_cli_parser_accepts_documented_flags(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--data-root",
            str(tmp_path),
            "--log-level",
            "debug",
        ]
    )
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.data_root == tmp_path
    assert args.log_level == "debug"
    assert args.reload is False
