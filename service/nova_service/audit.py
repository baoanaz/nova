"""查询审计的写入侧（TASK-084 §A，补 TASK-064 漏建的 ``audit.py``）。

职责只有一件事：把一次 ``/api/query/*`` 的结果**如实**落进 ``nova-meta.db`` 的
``query_audit`` 表。读取与聚合在 :meth:`nova_service.metadb.MetaDB.usage_summary`
与 ``routers/ops.py``（TASK-064 已交付），本模块不重复实现。

三条硬约束（TASK-064 §C / TASK-084 §A）：

1. **绝不让检索因审计失败而失败**——两个公开入口 :func:`record_query` /
   :func:`record_query_error` 都**不抛异常**：内部 try/except 兜底，失败只记 WARN。
   审计是旁路：记不上账不该让用户拿不到检索结果（单测钉住这一点）。
2. **不存源码内容**——只落 ``id/path/lines/tier/score``（Module/04 §8 冻结"不含源码内容"），
   见 :func:`evidence_meta`。
3. **脱敏**——``query`` 文本与失败摘要都过 :func:`nova_service.logging.redact_text`，
   **在写库之前**完成（不是只在日志层）：用户可能把 API key 粘进查询框，落库即留痕。

口径（"数字可比"的前提，只写在这里，各 handler 不再各写一套）：

- ``latency_ms`` 只包住检索/组装那一段（``EngineManager.search``），**不含**懒重扫与渲染，
  与 TASK-064 §C-3 一致；
- ``mode`` 取 ``fast``（``/api/query/search``）/ ``deep``（``/api/query/ask``），
  与 ``metadb.py`` 的 DDL 注释（``-- fast | deep``）及 WebUI 的 ``fast | deep`` 词汇一致；
  ``ask`` 另有 ``degraded=true`` 标记 Phase 2 的降级包（D-26）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from nova_core.contextpack.render import evidence_group

from nova_service.logging import current_request_id, get_logger, redact_text
from nova_service.metadb import MetaDB

__all__ = [
    "ANSWER_STORE_MAX_CHARS",
    "EVIDENCE_FIELDS",
    "MODE_ASK",
    "MODE_SEARCH",
    "STATUS_ANSWERED",
    "STATUS_DEGRADED",
    "STATUS_INSUFFICIENT",
    "evidence_meta",
    "redact_query_text",
    "record_query",
    "record_query_error",
    "truncate_answer",
]

logger = get_logger("nova_service.audit")

#: 两个端点的审计 ``mode``（DDL 注释冻结：``fast | deep``）。
MODE_SEARCH = "fast"
MODE_ASK = "deep"

# --------------------------------------------------------------------------- TASK-099 §A

#: ``answer_text`` 的长度上限（字符）。超过就截断并追加 :data:`ANSWER_TRUNCATION_SUFFIX`。
#:
#: 为什么必须卡：一条审计就能把库撑起来——LLM 在大 ``max_tokens`` 下可以输出数万字，而
#: ``query_audit`` 每项目保留 :data:`nova_service.metadb.QUERY_AUDIT_KEEP` 条。20000 字符 ≈
#: 40 KB/条，× 1000 条 ≈ 40 MB 上界（与项目索引同一个量级，可接受）。
ANSWER_STORE_MAX_CHARS = 20_000
#: 截断标记（用户要能看出"这里被截了"，而不是以为模型只说了这些）。
ANSWER_TRUNCATION_SUFFIX = "…（已截断）"

#: ``answer_status`` 的三个取值（TASK-099 §A；与 ``routers/query.py`` 的 ``status`` 逐一对应）。
#:
#: 为什么单独存这一列而不是"有正文就是成功"：``answerable=false`` 短路时**根本不调 LLM**，
#: 与"调了 LLM 但答案是空"是两件事，只有状态列能把它们区分开。
STATUS_ANSWERED = "answered"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_DEGRADED = "degraded"

#: ``evidence_json`` 的允许字段（Module/04 §8：**不含源码内容**）。
#:
#: TASK-108 追加 ``symbol`` / ``group`` / ``reason``：历史页对 search_context 的“Tool 输出”
#: 要如实展示**工具返回的结构**（分组 / 符号 / 召回依据），而不只是编号与路径。
#: 仍然**不含 ``content``**（源码正文）——那是 §8 的红线。
EVIDENCE_FIELDS: tuple[str, ...] = (
    "id",
    "path",
    "lines",
    "tier",
    "score",
    "symbol",
    "group",
    "reason",
)

#: 常见 API Key 的**裸串**形态（前面没有 ``api_key=`` 这类键名，``redact_text`` 的既有规则
#: 罩不住）。用户完全可能把 key 直接粘进查询框，而查询文本要落 ``query_audit``——于是本模块
#: 自己再加一道：``nova_`` 是本服务签发的 API Key（``auth.TOKEN_PREFIX``），``sk-`` 是
#: embedding 供应商的 key（OpenAI-compatible 家族，见 ``core/embedding/api.py``）。
#:
#: 为什么不直接改 :func:`nova_service.logging.redact_text`：那会改到本卡文件所有权清单外的
#: 公共模块（对所有日志/响应的脱敏面生效），已在执行记录的"未决问题"里向编排者提出。
_BARE_SECRET_RE = re.compile(r"\b(?:nova_|sk-)[A-Za-z0-9_\-]{8,}")


def redact_query_text(text: str) -> str:
    """审计文本脱敏（``redact_text`` + 裸 key 兜底）；**在写库前**调用。"""
    return _BARE_SECRET_RE.sub("***", redact_text(text))


def evidence_field_names() -> Sequence[str]:
    """``evidence_json`` 的字段名（冻结字段的自证入口；测试直接断言实现与之一致）。"""
    return EVIDENCE_FIELDS


def evidence_meta(pack: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    """``ContextPack`` → evidence 元数据（**只取** :data:`EVIDENCE_FIELDS`）。

    ``evidence`` 与 ``docs`` 共用 E 编号空间（D-21），编号即装填顺序，因此合并后按编号排序
    才是真正的"最相关在前"（与 :func:`nova_service.packmeta.evidence_summary` 同一口径）。

    TASK-108：追加 ``symbol`` / ``group`` / ``reason``——历史页要展示工具**返回的结构**
    （分组 / 符号名 / 召回依据），而不只是编号与路径。分组用 core 的
    :func:`nova_core.contextpack.render.evidence_group` 判定，与 Agent 实际看到的 Markdown
    分组**同源**（两处各写一份必然会漂移）。**不含** ``content``（源码正文，§8 红线）。
    """
    items = [*(getattr(pack, "evidence", None) or ()), *(getattr(pack, "docs", None) or ())]
    items.sort(key=_evidence_order)
    if limit is not None:
        items = items[:limit]
    # 分组基准分 = 包内代码证据的最高分（与 render 的 ``top1`` 同口径）。
    top1 = max(
        (item.score for item in items if getattr(item, "type", None) != "spec"), default=0.0
    )
    return [
        {
            "id": item.id,
            "path": item.path,
            "lines": list(item.lines) if item.lines is not None else None,
            "tier": item.evidence_tier,
            "score": item.score,
            "symbol": getattr(item, "symbol", None),
            "group": evidence_group(item, top1=top1),
            "reason": getattr(item, "reason", ""),
        }
        for item in items
    ]


def truncate_answer(text: str | None) -> str | None:
    """卡住答案正文的长度（``None`` 原样返回："没走 LLM"不该变成一个空串）。

    只截断不脱敏：LLM 输出不含用户凭据（provider 的 key 只在出站 ``Authorization`` 头里），
    而证据正文里的代码片段本来就来自用户自己的仓库，没有新高敏信息。
    """
    if text is None:
        return None
    if len(text) <= ANSWER_STORE_MAX_CHARS:
        return text
    return text[:ANSWER_STORE_MAX_CHARS] + ANSWER_TRUNCATION_SUFFIX


def record_query(
    db: MetaDB | None,
    *,
    project_id: str,
    mode: str,
    query: str,
    pack: Any,
    latency_ms: float,
    degraded: bool = False,
    answerable: bool | None = None,
    confidence: str | None = None,
    citation_coverage: float | None = None,
    llm_latency_ms: float | None = None,
    answer_tokens: int | None = None,
    user_id: str | None = None,
    answer_text: str | None = None,
    answer_status: str | None = None,
) -> None:
    """把一次**成功返回**的查询落库（旁路：任何失败只记 WARN，不影响检索）。

    字段默认取自 ``pack`` / 调用方给出的**真实值**，不编造；新增的三个 LLM 观测值
    （TASK-088 §E：``citation_coverage`` / ``llm_latency_ms`` / ``answer_tokens``）在
    **没走 LLM** 时保持 ``None``——"尚未测量"不是 0（TASK-064 §A 的诚实性纪律）。

    ``request_id``（TASK-094 §C）取自上下文的 ``current_request_id()``：与响应头
    ``X-Request-Id``、日志的 ``requestId`` **同一个 contextvar**，因此历史页看到的 id 与用户
    在报错时拿到的头、以及 TASK-090 的日志端点能查到的记录**必然一致**（这就是本卡要的接缝）。
    取不到（未绑定）时落 ``None``，不编造。

    ``answer_text`` / ``answer_status``（TASK-099 §A）：**同生共死**——有正文就必须有状态
    （否则前端不知道那段文本是什么），没正文也必须留下状态（否则"没调 LLM"等于没记录）。
    正文走 :func:`truncate_answer` 卡长度。
    """
    if db is None:
        return
    try:
        budget = getattr(pack, "budget", None)
        db.record_query(
            project_id=project_id,
            mode=mode,
            query=redact_query_text(query),
            latency_ms=_millis(latency_ms),
            answerable=bool(pack.answerable) if answerable is None else bool(answerable),
            confidence=pack.confidence if confidence is None else confidence,
            degraded=bool(degraded),
            evidence_count=len(getattr(pack, "evidence", None) or ()),
            docs_count=len(getattr(pack, "docs", None) or ()),
            used_tokens=int(budget.used_tokens) if budget is not None else 0,
            citation_coverage=citation_coverage,
            llm_latency_ms=_millis_or_none(llm_latency_ms),
            answer_tokens=answer_tokens,
            request_id=current_request_id(),
            answer_text=truncate_answer(answer_text),
            answer_status=answer_status,
            evidence=evidence_meta(pack),
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 - 旁路：任何失败都不得让检索失败
        _warn_swallowed(project_id, mode, exc)


def record_query_error(
    db: MetaDB | None,
    *,
    project_id: str,
    mode: str,
    query: str,
    latency_ms: float,
    reason: str,
    user_id: str | None = None,
    answer_status: str | None = None,
) -> None:
    """把一次**没走到检索结果**的请求落库（业务失败与未预期异常共用）。

    为什么失败路径也要记（TASK-084 §B）：不记的话 ``usage.failed`` 恒为 0，
    "失败次数"这个数就成了谎话——与 TASK-062 对索引统计的口径一致（成功与失败都落）。

    ``answerable`` 留 ``None`` 表示"没走到判定那一步"（与 ``False``"证据不足"是两件事），
    配合 ``degraded=True`` 归入 :meth:`MetaDB.usage_summary` 的 ``failed`` 计数。
    """
    if db is not None:
        try:
            db.record_query(
                project_id=project_id,
                mode=mode,
                query=redact_query_text(query),
                latency_ms=_millis(latency_ms),
                answerable=None,
                confidence=None,
                degraded=True,
                evidence_count=0,
                docs_count=0,
                used_tokens=0,
                citation_coverage=None,
                request_id=current_request_id(),
                # TASK-099 §A：失败路径**没有**答案正文（``None``），但状态仍要如实记。
                # 不写 ``STATUS_DEGRADED`` 当默认值：调用方没说"走的是哪条降级分支"时宁可留空，
                # 也不替它下一个可能不对的结论。
                answer_text=None,
                answer_status=answer_status,
                evidence=(),
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001 - 旁路
            _warn_swallowed(project_id, mode, exc)
    _log_failure(project_id, mode, reason)


def _log_failure(project_id: str, mode: str, reason: str) -> None:
    """失败摘要进日志（**脱敏后**：provider 报错可能带 key 片段）。"""
    try:
        logger.warning(
            "查询审计：%s（mode=%s）失败一次：%s", project_id, mode, redact_query_text(reason)
        )
    except Exception:  # noqa: BLE001 - 连日志都不该拖垮请求 # pragma: no cover
        pass


def _warn_swallowed(project_id: str, mode: str, exc: BaseException) -> None:
    try:
        logger.warning(
            "查询审计写入失败（检索照常）：%s（mode=%s）→ %s",
            project_id,
            mode,
            redact_query_text(f"{type(exc).__name__}: {exc}"),
        )
    except Exception:  # noqa: BLE001 # pragma: no cover
        pass


def _millis(latency_ms: float) -> int:
    """毫秒耗时 → 非负整数（``query_audit.latency_ms`` 是 INTEGER NOT NULL）。"""
    try:
        return max(0, int(round(float(latency_ms))))
    except (TypeError, ValueError):  # pragma: no cover - 调用方恒传数字
        return 0


def _millis_or_none(latency_ms: float | None) -> int | None:
    """可选的毫秒耗时（``None`` 表示"没走 LLM"，与 0 毫秒是两件事）。"""
    return None if latency_ms is None else _millis(latency_ms)


def _evidence_order(item: Any) -> int:
    raw = str(getattr(item, "id", ""))[1:]
    return int(raw) if raw.isdigit() else 0
