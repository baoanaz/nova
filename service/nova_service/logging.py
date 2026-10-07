"""结构化 JSON 日志（TASK-030 §交付物；Module/06 §2.4 可观测 + §3 secret 纪律）。

纪律（卡内冻结）：

- 一律 JSON 单行写 **stderr**（stdout 留给将来的协议/管道用途，日志不得污染）；
- 字段：``ts`` / ``level`` / ``logger`` / ``msg`` / ``requestId`` + 调用方 ``extra``；
- **脱敏**：``Authorization`` / ``Cookie`` / token / 请求体不进日志。落地方式有两条：
  ① 访问日志只记录 method / path / status / 耗时（**不碰 headers、不碰 body**）；
  ② 所有经 :class:`JsonFormatter` 输出的文本再过一遍 :func:`redact_text`，
     即使调用方手滑把 header 拼进 message，secret 也不会落盘。

``requestId`` 用 :mod:`contextvars` 传递（middleware 每请求设置一次），
因此线程池里的同步 handler 打日志也能带上同一个 id（``anyio.to_thread`` 会复制上下文）。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "JSON_FILE_HANDLER_NAME",
    "JSON_LOG_HANDLER_NAME",
    "REDACTED",
    "bind_request_id",
    "configure_logging",
    "current_request_id",
    "get_logger",
    "prune_log_files",
    "redact_text",
    "register_secret",
    "reset_request_id",
]

#: JSON handler 的固定名字（``configure_logging`` 幂等：重复调用只改 level，不叠 handler）。
JSON_LOG_HANDLER_NAME = "nova-json-stderr"
#: 日志文件 handler 的固定名字（TASK-090 §A：轮转 JSONL，保证服务重启后日志仍在）。
JSON_FILE_HANDLER_NAME = "nova-json-file"
#: 脱敏占位符。
REDACTED = "***"

_request_id: ContextVar[str | None] = ContextVar("nova_request_id", default=None)
_secrets: set[str] = set()

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
#: ``Authorization: xxx`` / ``cookie=xxx`` / ``"token": "xxx"`` 形态的键值对。
_KEYED_SECRET_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[_-]?key"
    r"|access[_-]?token|token|secret|password|passwd|credential)\b(\"?\s*[:=]\s*)(\"?)([^\s,;\"']+)"
)

#: ``LogRecord`` 的标准属性（作为 ``extra`` 过滤依据；新增属性靠 ``__dict__`` 差集识别）。
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime"}


def redact_text(text: str) -> str:
    """脱敏一段文本（Bearer token、key: value 形态的凭据、显式登记的 secret）。"""
    if not text:
        return text
    scrubbed = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    scrubbed = _KEYED_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}", scrubbed
    )
    for secret in _secrets:
        if secret and secret in scrubbed:
            scrubbed = scrubbed.replace(secret, REDACTED)
    return scrubbed


def register_secret(value: str) -> None:
    """登记一个需要脱敏的明文（如运行期生成的一次性 token）；空值忽略。"""
    if value:
        _secrets.add(value)


def bind_request_id(request_id: str) -> Token[str | None]:
    """绑定当前上下文的 requestId（返回值交给 :func:`reset_request_id` 还原）。"""
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()


def get_logger(name: str = "nova_service") -> logging.Logger:
    """取 logger（配置由 :func:`configure_logging` 统一负责）。"""
    return logging.getLogger(name)


class RedactingFilter(logging.Filter):
    """把 ``record.msg`` / args / ``extra`` 里的凭据抹掉（第二道防线，见模块 docstring）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            if isinstance(value, str):
                record.__dict__[key] = redact_text(value)
        return True


class JsonFormatter(logging.Formatter):
    """``LogRecord`` → 单行 JSON（字段见模块 docstring）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        request_id = current_request_id()
        if request_id:
            payload["requestId"] = request_id
        for key, value in sorted(record.__dict__.items()):
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = redact_text(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "info",
    *,
    log_path: str | Path | None = None,
    max_bytes: int = 0,
    backup_count: int = 0,
    retention_days: int = 0,
) -> logging.Logger:
    """装上 JSON handler（幂等：只加一次，重复调用即改 level）。返回根 logger。

    ``log_path`` 非空时**额外**挂一个轮转文件 handler（TASK-090 §A）：stderr 保留人看的实时输出，
    文件是给"事后按 requestId 查"用的持久副本。两者共用同一个 :class:`JsonFormatter` 与
    :class:`RedactingFilter`，因此**脱敏对文件与终端逐字一致**（不会出现"文件里有 key、终端没有"）。

    为什么文件默认不挂（``log_path=None``）：测试与 ``mcp-config`` 这类短生命周期进程不需要落盘，
    而"日志写到用户主目录"应当是默认行为而不是隐式副作用——只有 ``app.create_app`` 显式开启。
    """
    root = logging.getLogger()
    handler = _find_handler(root, JSON_LOG_HANDLER_NAME)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        handler.name = JSON_LOG_HANDLER_NAME
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    _harden(root, handler)
    handler.setLevel(_resolve_level(level))
    root.setLevel(_resolve_level(level))
    if log_path is not None:
        _install_file_handler(
            root,
            Path(log_path),
            level=_resolve_level(level),
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        prune_log_files(Path(log_path), retention_days=retention_days)
    return root


def _find_handler(root: logging.Logger, name: str) -> logging.Handler | None:
    return next((item for item in root.handlers if getattr(item, "name", "") == name), None)


def _harden(root: logging.Logger, handler: logging.Handler) -> None:
    """挂上 RedactingFilter（第二道防线）。

    为什么过滤器挂在 **handler** 上：handler 会收到所有子 logger 传播上来的记录，因此
    caplog / 第三方 handler / 文件 handler 拿到的都已是脱敏文本。
    （logger 级 filter 只对直接写在该 logger 上的记录生效，覆盖不到子 logger，故不作为主防线。）
    """
    if not any(isinstance(item, RedactingFilter) for item in handler.filters):
        handler.addFilter(RedactingFilter())
    if not any(isinstance(item, RedactingFilter) for item in root.filters):
        root.addFilter(RedactingFilter())


def _install_file_handler(
    root: logging.Logger,
    path: Path,
    *,
    level: int,
    max_bytes: int,
    backup_count: int,
) -> None:
    """挂轮转文件 handler（幂等：路径变化时替换，避免旧路径继续被写）。

    ``max_bytes<=0`` 时**不轮转**（单文件无限增长）——保留这个口子是为了让用户能显式选择"只按
    天数清理"；默认值走 :data:`nova_service.config.DEFAULT_LOG_MAX_BYTES`（有界）。
    """
    existing = _find_handler(root, JSON_FILE_HANDLER_NAME)
    if existing is not None and Path(getattr(existing, "baseFilename", "")) == path.resolve():
        existing.setLevel(level)
        return
    if existing is not None:  # 路径变了（同进程换 data_root）：关掉旧的，换新的
        root.removeHandler(existing)
        existing.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    rotating = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max(0, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
        delay=True,  # 首个请求才建文件：起服务不因日志目录权限问题失败
    )
    rotating.name = JSON_FILE_HANDLER_NAME
    rotating.setFormatter(JsonFormatter())
    _harden(root, rotating)
    rotating.setLevel(level)
    root.addHandler(rotating)


def prune_log_files(path: Path, *, retention_days: int, now: float | None = None) -> int:
    """按天清理窗口（TASK-090 §A）：删除 ``path`` 及其轮转备份中超过保留期的文件。

    返回删除的**文件数**（可观测；服务启动与每次写入前调用）。

    为什么不只靠 ``RotatingFileHandler``：它只按**体积**轮转，"最近 N 天"这个用户语义表达不了——
    低流量服务可能几个月都不触发一次轮转，于是"窗口"名存实亡。两条腿各管一个维度：体积管上界、
    天数管陈旧（卡内 §A 的"有界保留"就是这两个维度的交集）。

    ``retention_days<=0`` 表示不按天清理。删除失败（权限/占用）只忽略：清理是**尽力而为**，
    不该因为它而让服务起不来。
    """
    if retention_days <= 0:
        return 0
    current = time.time() if now is None else now
    cutoff = current - retention_days * 86400
    removed = 0
    for candidate in _log_family(path):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except OSError:  # 权限/占用/竞态：跳过（清理是旁路）
            continue
    return removed


def _log_family(path: Path) -> list[Path]:
    """``request.log`` 及其轮转备份（``request.log.1`` … ``request.log.9``）。"""
    family = [path] if path.exists() else []
    family += sorted(path.parent.glob(f"{path.name}.*"))
    return family


def _resolve_level(level: str) -> int:
    resolved = logging.getLevelName(level.strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO
