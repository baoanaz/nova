"""统一错误信封与异常处理器（TASK-030 §交付物；CF-05 冻结合同）。

CF-05 冻结的 Error 形态（**不得改**）::

    {"error": {"code": "<稳定机器码>", "message": "<人类可读说明>"}}

转义规则（本卡冻结）：

- 业务/校验错误一律抛 :class:`ApiError`（含 HTTP 状态码），由处理器转成信封；
- 未知路径 404、方法不允许 405 也走同一信封（``not_found`` / ``method_not_allowed``）；
- 未捕获异常 → 500 ``internal_error``：**响应不含堆栈**，全量堆栈只进日志（Module/06 §3
  secret 纪律：错误响应不得泄漏实现细节与凭据）；
- 尚未实现的端点 → 501 ``not_implemented``（占位路由统一用它，见 TASK-030 §routers）；
- **引擎/依赖类异常 → 503/507 映射（TASK-035 §A）**：provider 不可用、provider 不可达、
  磁盘/权限错误各有稳定 code 与**可操作指引**，见 :func:`map_engine_error`。

映射纪律（TASK-035 §A）：

- **只按异常类型判断**（含 ``__cause__`` 故障链），不做 message 字符串匹配；
- ``ApiError`` 之外不新增响应形态：映射结果仍是 CF-05 的信封；
- 响应文案里的 provider 摘要经 :func:`nova_service.logging.redact_text` 脱敏（API key 不外泄），
  全量堆栈只进日志。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from nova_core.embedding import (
    ApiAuthError,
    ApiNetworkError,
    ApiRateLimitError,
    ApiResponseError,
    EmbeddingConfigError,
    EmbeddingDimMismatchError,
    EmbeddingError,
    LocalModelUnavailableError,
)
from nova_core.engine import EngineError
from starlette.exceptions import HTTPException as StarletteHTTPException

from nova_service.logging import redact_text

try:  # pragma: no cover - httpx 是 nova-core 的硬依赖（embedding/api.py），正常必然可导入
    import httpx

    _HTTPX_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError,)
except ModuleNotFoundError:  # pragma: no cover - 极端裁剪环境下的兜底（映射少一类，不崩）
    _HTTPX_ERRORS = ()

__all__ = [
    "CODE_EMBEDDING_UNAVAILABLE",
    "CODE_EMBEDDING_UNREACHABLE",
    "CODE_STORAGE_ERROR",
    "INTERNAL_ERROR_MESSAGE",
    "PROVIDER_UNAVAILABLE_HINT",
    "PROVIDER_UNREACHABLE_HINT",
    "STORAGE_ERROR_HINT",
    "ApiError",
    "embedding_failure_reason",
    "error_envelope",
    "install_error_handlers",
    "internal_error_response",
    "map_engine_error",
    "not_implemented",
]

logger = logging.getLogger("nova_service.errors")

#: 500 的对外文案（不透露任何内部细节；细节在日志里）。
INTERNAL_ERROR_MESSAGE = "服务内部错误，请稍后重试；服务端日志含完整堆栈"

#: provider 构造/配置/模型文件类故障（503）。
CODE_EMBEDDING_UNAVAILABLE = "embedding_unavailable"
#: provider 调用不可达（连接/超时/鉴权/限流/响应异常，503）。
CODE_EMBEDDING_UNREACHABLE = "embedding_unreachable"
#: 磁盘/权限类 ``OSError``（507）。
CODE_STORAGE_ERROR = "storage_error"

#: 503（provider 不可用）的可操作指引（Module/06 §3：响应要写清楚"下一步做什么"）。
PROVIDER_UNAVAILABLE_HINT = (
    "请检查 embedding 配置（EMBED_MODE / EMBED_MODEL / EMBED_BASE_URL / EMBED_API_KEY）与"
    "模型缓存目录（EMBED_CACHE_DIR / EMBED_MODEL_DIR）：本地模式下首次使用可能需要联网下载模型，"
    "离线环境请预先放置模型文件或显式设置 EMBED_OFFLINE=1 与 EMBED_MODEL_DIR。"
)
#: 503（provider 不可达）的可操作指引。
PROVIDER_UNREACHABLE_HINT = (
    "请确认 embedding 服务地址可达（EMBED_BASE_URL）、网络/代理配置正确、EMBED_API_KEY 有效；"
    "服务端日志含完整堆栈与 provider 的原始报错。"
)
#: 507（存储错误）的可操作指引。
STORAGE_ERROR_HINT = "请检查 data_root 所在磁盘的空余空间与读写权限；服务端日志含完整堆栈。"

#: 故障链里出现即判"provider 不可用"（构造/配置/模型文件/维度不一致）。
_PROVIDER_UNAVAILABLE_ERRORS: tuple[type[BaseException], ...] = (
    EmbeddingConfigError,
    LocalModelUnavailableError,
    EmbeddingDimMismatchError,
)
#: 故障链里出现即判"provider 不可达"（网络/鉴权/限流/响应非法）。
_PROVIDER_UNREACHABLE_ERRORS: tuple[type[BaseException], ...] = (
    ApiAuthError,
    ApiNetworkError,
    ApiRateLimitError,
    ApiResponseError,
)
#: 需要挂异常处理器的类型（每个都必须能被 :func:`map_engine_error` 映射）。
_MAPPED_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    EngineError,
    EmbeddingError,
    OSError,
    *_HTTPX_ERRORS,
)

#: 异常链的追溯层数上限（防循环引用；真实故障链都在 2 层内）。
_CAUSE_CHAIN_LIMIT = 5


class ApiError(Exception):
    """带稳定机器码的业务错误（处理器按 ``status`` 返回 CF-05 信封）。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def error_envelope(code: str, message: str) -> dict[str, dict[str, str]]:
    """CF-05 的 Error 信封（唯一构造点）。"""
    return {"error": {"code": code, "message": message}}


def not_implemented(feature: str, task: str = "") -> ApiError:
    """占位路由的统一错误（501）：明说"未实现"与归属任务，不假装成功。"""
    suffix = f"（由 {task} 交付）" if task else ""
    return ApiError(
        code="not_implemented",
        message=f"{feature} 尚未实现{suffix}：本版本为 M2a 骨架，该端点为占位。",
        status=501,
    )


def internal_error_response() -> JSONResponse:
    """未捕获异常的统一响应（500 + 信封，无堆栈）。"""
    return JSONResponse(
        status_code=500, content=error_envelope("internal_error", INTERNAL_ERROR_MESSAGE)
    )


def _cause_chain(exc: BaseException) -> Iterator[BaseException]:
    """异常及其故障链（``__cause__`` 优先，其次 ``__context__``；层数上限见常量）。"""
    current: BaseException | None = exc
    for _ in range(_CAUSE_CHAIN_LIMIT):
        if current is None:
            return
        yield current
        current = current.__cause__ or current.__context__


def _first_in_chain(
    exc: BaseException, types: tuple[type[BaseException], ...]
) -> BaseException | None:
    """故障链里第一个属于 ``types`` 的异常（**按类型判断**，不看 message）。"""
    return next((item for item in _cause_chain(exc) if isinstance(item, types)), None)


def _summary(exc: BaseException) -> str:
    """异常的一行摘要（脱敏：API key/token 不进响应）。"""
    return redact_text(f"{type(exc).__name__}: {exc}")


def embedding_failure_reason(exc: BaseException) -> str | None:
    """异常是否属于 **embedding provider 故障链**；是则返回脱敏摘要，否则 ``None``。

    TASK-035 §A/§B 共用：异常处理器用它决定 503 映射，``EngineManager.provider_health``
    用它记住"最近一次 provider 故障"。**只按类型判断**（``EmbeddingError`` / ``httpx``）。
    """
    embedding_types = (EmbeddingError, *_HTTPX_ERRORS)
    found = _first_in_chain(exc, embedding_types)
    return _summary(found) if found is not None else None


def map_engine_error(exc: BaseException) -> ApiError | None:
    """引擎/依赖类异常 → :class:`ApiError`（TASK-035 §A）；不属映射面则 ``None``。

    | 异常（含故障链） | 结果 |
    |---|---|
    | provider 构造/配置/模型文件/维度不一致 | 503 ``embedding_unavailable`` + 可操作指引 |
    | provider 调用不可达（网络/鉴权/限流/响应） | 503 ``embedding_unreachable`` + 可操作指引 |
    | ``OSError``（磁盘/权限） | 507 ``storage_error`` |
    | 其它（含无 embedding 故障链的 ``EngineError``） | ``None`` → 仍是 500 ``internal_error`` |

    为什么 ``EngineError`` 要看故障链：core 用它做**通用**引擎错误（如"非法 project_id"
    "仓库路径不是目录"），只有 ``Engine.provider`` 包装 provider 构造失败时才属依赖故障
    （``raise EngineError(...) from exc``，故 ``__cause__`` 是 embedding 异常）。
    """
    if isinstance(exc, _PROVIDER_UNAVAILABLE_ERRORS):
        return ApiError(
            CODE_EMBEDDING_UNAVAILABLE,
            f"embedding provider 不可用：{_summary(exc)}。{PROVIDER_UNAVAILABLE_HINT}",
            503,
        )
    if isinstance(exc, (*_PROVIDER_UNREACHABLE_ERRORS, *_HTTPX_ERRORS)):
        return ApiError(
            CODE_EMBEDDING_UNREACHABLE,
            f"embedding 服务不可达：{_summary(exc)}。{PROVIDER_UNREACHABLE_HINT}",
            503,
        )
    if isinstance(exc, EngineError):
        reason = embedding_failure_reason(exc)
        if reason is not None:
            return ApiError(
                CODE_EMBEDDING_UNAVAILABLE,
                f"embedding provider 不可用：{reason}。{PROVIDER_UNAVAILABLE_HINT}",
                503,
            )
        storage = _first_in_chain(exc, (OSError,))
        if storage is not None:
            return _storage_error(storage)
        return None
    if isinstance(exc, OSError):
        return _storage_error(exc)
    return None


def _storage_error(exc: BaseException) -> ApiError:
    return ApiError(
        CODE_STORAGE_ERROR,
        f"存储/文件系统错误（{_summary(exc)}）。{STORAGE_ERROR_HINT}",
        507,
    )


def install_error_handlers(app: FastAPI) -> None:
    """挂上 CF-05 的信封处理器（ApiError / HTTPException / 校验错误 / 引擎映射 / 兜底）。"""

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=error_envelope(exc.code, exc.message)
        )

    async def _mapped_engine_error(request: Request, exc: Exception) -> JSONResponse:
        # TASK-035 §A：引擎/依赖类异常 → 503/507（根因进日志，指引进响应，不泄露 secret）。
        mapped = map_engine_error(exc)
        logger.error(
            "mapped engine error",
            extra={
                "requestId": request.headers.get("X-Request-Id", ""),
                "method": request.method,
                "path": request.url.path,
                "excType": type(exc).__name__,
                "code": mapped.code if mapped is not None else "internal_error",
            },
            exc_info=exc,
        )
        if mapped is None:  # pragma: no cover - 注册类型里只有 EngineError 可能落到这里
            return internal_error_response()
        return JSONResponse(
            status_code=mapped.status, content=error_envelope(mapped.code, mapped.message)
        )

    for exc_type in _MAPPED_EXCEPTION_TYPES:
        app.add_exception_handler(exc_type, _mapped_engine_error)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_STATUS_CODES.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(code, str(exc.detail) if exc.detail else code),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_envelope("invalid_request", _format_validation(exc)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 日志留全量（含堆栈），响应只给通用文案（Module/06 §3：响应不泄漏实现细节）。
        logger.exception("unhandled error: %s %s", request.method, request.url.path)
        _ = exc
        return internal_error_response()


#: HTTP 状态码 → 稳定机器码（CF-05 只冻结信封形态；这里是本卡的映射口径）。
_HTTP_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    501: "not_implemented",
}


def _format_validation(exc: RequestValidationError) -> str:
    """校验错误摘要（只含字段位置与原因，不含请求体内容）。"""
    details = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        details.append(f"{location or 'body'}: {error.get('msg', 'invalid')}")
    return "请求参数校验失败（" + "; ".join(details) + "）" if details else "请求参数校验失败"
