"""请求日志的**读取与查询侧**（TASK-090 §B/§C）。

存储选择（§A，本卡裁定 **方案 A：文件轮转 sink**）：

| 维度 | 结论 |
|---|---|
| 载体 | ``{data_root}/logs/request.log``（JSONL，一行一条请求） |
| 窗口 | 单文件 :data:`nova_service.config.DEFAULT_LOG_MAX_BYTES` × (备份数+1)，加保留天数 |
| 为什么不用 DB | 本地模式按 R34/``test_tenancy`` **不该建 ``nova-meta.db``**；用表方案会让本地模式
  彻底没有日志，文件方案两种形态都持久化，且与 metadb 零耦合 |

本模块只做三件事，写入侧不在这里：

1. :func:`capture_request`——把一次请求的**可结构化字段**记进 ``logging``（经 ``RedactingFilter``
   脱敏后落到 JSONL）；
2. :func:`lookup`——按 ``requestId`` 在日志家族（当前文件 + 轮转备份）里**倒序**找那条记录；
3. :func:`authorize`——归属规则（§C）：只有 ``userId`` 匹配者能看，其余一律与"不存在"同响应。

**脱敏是硬纪律**（§B）：本模块**从不**读取 ``Authorization`` / ``Cookie`` / 请求体；``path`` 里
可能混进的裸 key（``sk-`` / ``nova_``）再过一道 :func:`redact_query_text` 同款兜底。
5xx 的堆栈进 ``traceback`` 字段，但 HTTP 响应仍只给 ``errors.py`` 的通用文案。
"""

from __future__ import annotations

import json
import re
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nova_service.logging import get_logger, redact_text

__all__ = [
    "MAX_FIELD_CHARS",
    "MAX_RELATED_LOGS",
    "MAX_TRACEBACK_CHARS",
    "RequestLogEntry",
    "capture_request",
    "is_visible_to",
    "lookup",
    "lookup_all",
    "owner_is_visible",
    "read_entries",
    "redact_request_text",
]

logger = get_logger("nova_service.requestlog")

#: 单个字段的返回上限（§C："**截断**长字段，避免响应过大"）。
MAX_FIELD_CHARS = 2000
#: ``traceback`` 的返回上限（比普通字段宽：ASGI 的 ``ExceptionGroup`` 包装很啰嗦，
#: 2000 字符会在还没写到**根因**时就截断——而根因正是排查要看的东西）。
MAX_TRACEBACK_CHARS = 8000
#: 同一 requestId 下最多附带多少条**旁路**日志（§C：处理器抛出的堆栈在另一行，见
#: :func:`lookup_all` 的说明；上限防止响应过大）。
MAX_RELATED_LOGS = 20

#: 裸 key 兜底（与 ``audit.redact_query_text`` 同一形态：``redact_text`` 的键名规则罩不住裸串）。
#: ``nova_`` 是本服务签发的 API Key（``auth.TOKEN_PREFIX``），``sk-`` 是 embedding/LLM 的 key。
_BARE_SECRET_RE = re.compile(r"\b(?:nova_|sk-)[A-Za-z0-9_\-]{8,}")


def redact_request_text(text: str) -> str:
    """请求日志文本脱敏（``redact_text`` + 裸 key 兜底）。"""
    return _BARE_SECRET_RE.sub("***", redact_text(text))


@dataclass(frozen=True, slots=True)
class RequestLogEntry:
    """一条请求日志（§B 的字段表；``extra`` 里是与本次请求相关的附加结构化字段）。"""

    request_id: str
    ts: str
    level: str
    logger: str
    message: str
    method: str | None
    path: str | None
    status: int | None
    duration_ms: float | None
    user_id: str | None
    project_id: str | None
    error_code: str | None
    error_message: str | None
    traceback: str | None
    extra: Mapping[str, Any]

    def to_json(self, *, max_chars: int = MAX_FIELD_CHARS) -> dict[str, Any]:
        """截断后的响应形态（``camelCase``，与 CF-05 / 其余端点一致）。

        ``traceback`` 用更宽的 :data:`MAX_TRACEBACK_CHARS`，且截断策略是**保头保尾**
        （见 :func:`_truncate`）——异常链的根因在尾部，只保头等于把最有用的部分丢掉。
        """
        return {
            "requestId": self.request_id,
            "ts": self.ts,
            "level": self.level,
            "method": self.method,
            "path": _truncate(self.path, max_chars),
            "status": self.status,
            "durationMs": self.duration_ms,
            "userId": self.user_id,
            "projectId": self.project_id,
            "errorCode": self.error_code,
            "errorMessage": _truncate(self.error_message, max_chars),
            "traceback": _truncate(self.traceback, MAX_TRACEBACK_CHARS),
            "extra": {
                key: _truncate(value, max_chars) if isinstance(value, str) else value
                for key, value in self.extra.items()
            },
        }

    @property
    def user_id_or_none(self) -> str | None:
        return self.user_id or None


def capture_request(
    *,
    request_id: str,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    user_id: str | None = None,
    project_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    exc_info: BaseException | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """把一次请求记进日志（**绝不因日志失败而影响请求**）。

    ``exc_info`` 非空时附上完整堆栈——这是 5xx 排查的核心价值（§B），但**只进日志**，
    HTTP 响应仍走 ``errors.py`` 的通用文案。

    为什么全部字段都过 :func:`redact_request_text` 而不是只靠 ``RedactingFilter``：
    filter 只处理 ``record.msg`` 与 ``extra`` 的**字符串**值；本函数在**入日志之前**就脱敏，
    这样即使将来某个 handler 漏挂 filter，落盘的也已是干净文本（纵深防御）。
    """
    try:
        payload: dict[str, Any] = {
            "requestId": request_id,
            "method": method,
            "path": redact_request_text(path),
            "status": int(status),
            "durationMs": round(float(duration_ms), 2),
        }
        if user_id:
            payload["userId"] = user_id
        if project_id:
            payload["projectId"] = project_id
        if error_code:
            payload["errorCode"] = error_code
        if error_message:
            payload["errorMessage"] = redact_request_text(error_message)
        for key, value in (extra or {}).items():
            payload[key] = redact_request_text(value) if isinstance(value, str) else value
        if exc_info is not None:
            payload["traceback"] = redact_request_text(_format_traceback(exc_info))
        logger.info("request", extra=payload)
    except Exception as exc:  # noqa: BLE001 - 日志是旁路：记不上也不能拖垮请求
        try:
            logger.warning("请求日志写入失败（请求照常）：%s", redact_request_text(str(exc)))
        except Exception:  # noqa: BLE001 # pragma: no cover
            pass


def is_visible_to(entry: RequestLogEntry, user_id: str | None) -> bool:
    """归属规则（§C）：``user_id`` 为 ``None``（本地模式）时**放行**，否则必须精确匹配。

    见 :func:`owner_is_visible`（真正的判定逻辑；这里只是取条目的 ``userId``）。
    """
    return owner_is_visible(entry.user_id, user_id)


def owner_is_visible(owner: str | None, requester: str | None) -> bool:
    """归属规则本体：``requester`` 为 ``None``（本地模式，R34）时放行，否则必须相等。

    - 本地模式（无账户体系）：``requester is None`` → 全部可见，行为与今天一致；
    - 云端形态：只认 ``owner == requester``。

    **注意**：``owner`` 为空（例如未认证的 401 请求没有 userId、或路径未匹配到路由）时
    **恒为不可见**——调用方按"不存在"处理（同 404），不给探测面（Module/06 §2.2）。
    """
    if requester is None:
        return True
    return bool(owner) and owner == requester


def lookup(
    log_path: str | Path,
    request_id: str,
    *,
    user_id: str | None = None,
    max_files: int = 32,
) -> RequestLogEntry | None:
    """按 ``requestId`` 查**最近一条** ``request`` 记录（当前文件 + 轮转备份，倒序找）。

    找不到、越权（归属不匹配）都返回 ``None``——调用方对两种情况给**同一个** 404 响应。

    为什么倒序：同一 id 理论上只出现一次，但调用方可以自带 ``X-Request-Id`` 复用同一个 id
    （中间件尊重调用方传入），此时"最近一次"才是有用的答案。
    """
    for entry in lookup_all(log_path, request_id, user_id=user_id, max_files=max_files):
        if entry.message == "request":
            return entry
    return None


def lookup_all(
    log_path: str | Path,
    request_id: str,
    *,
    user_id: str | None = None,
    max_files: int = 32,
    limit: int = 200,
) -> list[RequestLogEntry]:
    """同一 ``requestId`` 下的**全部**日志行（新的在前；归属不匹配则空列表）。

    为什么不止查 ``request`` 那一行：**已处理**的 5xx（如 TASK-035 映射出的 503 embedding
    不可达）由 ``errors.py`` 的异常处理器自己打堆栈（``exc_info=exc``），堆栈落在**另一条**
    记录上。只返回 ``request`` 行的话，用户报一个 503 却看到 ``traceback: null``——而那正是
    最需要堆栈的场景。调用方（``routers/ops.py``）把剩余行作为 ``relatedLogs`` 附带返回。
    """
    if not request_id:
        return []
    matched = [
        entry for entry in read_entries(log_path, max_files=max_files)
        if entry.request_id == request_id
    ]
    if not matched:
        return []
    # 归属看该请求的**发起者**：同一 requestId 的所有行共享同一身份（401 那行为空）。
    owner = next((entry.user_id for entry in matched if entry.user_id), None)
    if not owner_is_visible(owner, user_id):
        logger.info(
            "请求日志查询被拒（归属不匹配）",
            extra={"requestId": request_id, "requester": user_id or "local"},
        )
        return []
    return matched[: max(1, int(limit))]


def read_entries(path: str | Path, *, max_files: int = 32) -> list[RequestLogEntry]:
    """读日志家族并返回**按时间倒序**的条目（新的在前；无法解析的行直接跳过）。

    ``max_files`` 是允许读取的最大轮转编号；只接受 ``.1`` 到 ``.max_files``，避免异常残留的
    ``.99`` 在较新备份缺失时被切片选中、越过配置窗口。
    """
    base = Path(path)
    files = [base] if base.exists() else []
    # 轮转备份：``request.log.1`` 最新、编号越大越旧（RotatingFileHandler 的既定命名）。
    max_backup = max(0, int(max_files))
    backups = sorted(base.parent.glob(f"{base.name}.*"), key=_backup_order)
    files += [candidate for candidate in backups if _backup_order(candidate) <= max_backup]
    entries: list[RequestLogEntry] = []
    for candidate in files:
        entries.extend(_read_file(candidate))
    return entries


def _backup_order(candidate: Path) -> int:
    """备份文件的序号（``request.log.1`` → 1）；无数字后缀排最后（不参与倒序优先级）。"""
    suffix = candidate.name.rsplit(".", 1)[-1]
    return int(suffix) if suffix.isdigit() else 1 << 30


def _read_file(path: Path) -> list[RequestLogEntry]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # 权限/竞态：当作空文件（查不到就是查不到，不 500）
        return []
    return [entry for entry in (_parse_line(line) for line in text.splitlines()) if entry]


def _parse_line(line: str) -> RequestLogEntry | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:  # 半行（进程被杀时正在写）或非本服务写入的行
        return None
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        return None
    known = {
        "ts",
        "level",
        "logger",
        "msg",
        "requestId",
        "method",
        "path",
        "status",
        "durationMs",
        "userId",
        "projectId",
        "errorCode",
        "errorMessage",
        "traceback",
    }
    return RequestLogEntry(
        request_id=request_id,
        ts=str(payload.get("ts", "")),
        level=str(payload.get("level", "")),
        logger=str(payload.get("logger", "")),
        message=str(payload.get("msg", "")),
        method=_as_text(payload.get("method")),
        path=_as_text(payload.get("path")),
        status=_as_int(payload.get("status")),
        duration_ms=_as_float(payload.get("durationMs")),
        user_id=_as_text(payload.get("userId")),
        project_id=_as_text(payload.get("projectId")),
        error_code=_as_text(payload.get("errorCode")),
        error_message=_as_text(payload.get("errorMessage")),
        traceback=_as_text(payload.get("traceback")),
        extra=MappingProxyType(
            {key: value for key, value in payload.items() if key not in known}
        ),
    )


def _truncate(value: Any, max_chars: int) -> Any:
    """超长文本截断为"头 + 省略标记 + 尾"。

    保留**尾部**是有意的：Python 异常链的根因（``RuntimeError: ...`` 那行）在最后，
    只留开头会在 ASGI 的 ``ExceptionGroup`` 包装上把最有用的那一行裁掉。
    """
    if not isinstance(value, str) or len(value) <= max_chars:
        return value
    if max_chars <= 32:  # 太小就只保头（保尾反而全被标记占满）
        return value[:max_chars]
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(value) - max_chars
    return f"{value[:head]}\n…（中间省略 {omitted} 字符）…\n{value[-tail:]}"


def _as_text(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int, float)) and str(value) else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
