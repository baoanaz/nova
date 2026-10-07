"""**用户级** LLM 配置：解析"这次调用该用哪一份 LLM 配置"（TASK-099 §C-4）。

用户 2026-09-14 原话：

> 设置页面，其中 LLM 修改成自定义的配置方式，模型名、URL、Key。目的是给用户自定义 LLM 的选择。
> 这个自定义是和用户绑定的，如果涉及到 ASK 工具调用，并且用户配置了的话，就用用户的定义。

因此本模块只做一件事，也是**唯一**的解析入口（REST 面与 MCP 面共用，不各写一套）：

```
resolve_llm_config(settings, user_id=..., db=...) -> ResolvedLlmConfig
    用户配置存在 → 用它（source="user"）
    否则          → settings 的 ANSWER_*（source="server"）
```

四条纪律：

1. **不泄漏 key**：:class:`ResolvedLlmConfig` 含明文 key 且**只供构造 provider**。它的
   :meth:`ResolvedLlmConfig.to_public_json` 是唯一允许出网的形态（只有 ``apiKeyConfigured``
   布尔）。本模块不打印 key，异常也不带 key（构造 provider 时由
   ``answer.HttpAnswerProvider`` 负责 ``register_secret``，即全仓日志脱敏的第二道防线）。
2. **降级不是错误**：用户没配（或配置读不出来）→ 用服务端默认；服务端也没配 →
   ``configured=False``，调用方走 D-26 降级包（**不抛异常**，L5 冷启动体验）。
3. **明文存储**（卡内 §C-2 裁定）：单用户自部署是主场景，DB 与 ``.env`` 同机同信任域，
   加密存储不增加实际安全性；但必须**如实说明**而不是暗示已加密。
4. **纯标准库 + 现有依赖**：不引新依赖；``Settings`` 是不可变对象，缺省值只有一份
   （``settings.answer_*``），本模块不复制第二套常量。

**已知限制（如实记录，不在本卡修）**：MCP 面的 provider 由 ``build_mcp`` 在构造时确定，
``ask_project`` 的 ``_provider_resolver`` 虽在**每次调用时**重新解析用户配置，但
``nova-service local`` 起的长驻进程把 ``Settings`` 包进了闭包，用户配置的变更需重启服务
（TASK-094 的配额快照问题同源，属 provider 生命周期重构，另开卡）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nova_service.answer import AnswerProvider
from nova_service.config import Settings
from nova_service.llmprotocol import DEFAULT_PROTOCOL
from nova_service.logging import get_logger

if TYPE_CHECKING:  # 仅类型：避免无谓的模块级导入
    from nova_service.metadb import MetaDB

__all__ = [
    "SOURCE_SERVER",
    "SOURCE_USER",
    "ResolvedLlmConfig",
    "build_provider_for",
    "provider_for_request",
    "resolve_llm_config",
]

logger = get_logger("nova_service.llmconfig")

#: 配置来源（写进 ``GET /api/meta`` 的 ``config.llm.source``，设置页据此显示"当前生效的是哪份"）。
SOURCE_USER = "user"
SOURCE_SERVER = "server"


@dataclass(frozen=True, slots=True)
class ResolvedLlmConfig:
    """一次 ask 实际生效的 LLM 配置（**含明文 key**，只在服务端内部流转）。"""

    base_url: str | None
    api_key: str | None
    model: str | None
    timeout_s: float
    max_tokens: int
    temperature: float
    #: ``"user"`` = 用户配置生效；``"server"`` = 回落服务端默认（环境变量）。
    source: str
    #: 上游协议（TASK-113 / D-47）；``"openai"`` 为默认（与升级前的行为逐字相同）。
    protocol: str = DEFAULT_PROTOCOL

    @property
    def configured(self) -> bool:
        """三个必填项是否齐备（与 ``Settings.answer_configured`` 同一口径）。"""
        return bool(self.base_url and self.model and self.api_key)

    @property
    def missing_keys(self) -> tuple[str, ...]:
        """缺哪几项的**字段名**（不是环境变量名——用户配置不来自环境变量）。

        只回字段名，不回值，不回长度：连"key 有几个字符"都是信息（TASK-088 §F 的同一纪律）。
        """
        missing: list[str] = []
        if not self.base_url:
            missing.append("baseUrl")
        if not self.api_key:
            missing.append("apiKey")
        if not self.model:
            missing.append("model")
        return tuple(missing)

    def to_public_json(self) -> dict[str, Any]:
        """可出网的形态：**绝不含 key 的任何部分**（含长度/前缀/哈希）。"""
        return {
            "configured": self.configured,
            "apiKeyConfigured": bool(self.api_key),
            "missingKeys": list(self.missing_keys),
            "source": self.source,
            "protocol": self.protocol,
        }


def resolve_llm_config(
    settings: Settings,
    *,
    user_id: str | None = None,
    db: MetaDB | None = None,
) -> ResolvedLlmConfig:
    """解析生效配置：**用户配置优先，否则服务端默认**（卡内 §C-4 的唯一入口）。

    ``user_id`` 为 ``None``（本地模式无账户 / 匿名 / MCP 无身份）或 ``db`` 不可用时直接走
    服务端默认——这是**正常的降级路径**，不是错误，因此不记 WARN 也不抛异常。

    读用户配置失败（库损坏等）同样回落服务端默认并记一条 WARN（**带 user_id，不带 key**）：
    一次读库失败不该让用户的提问变成 500。
    """
    if user_id and db is not None:
        try:
            record = db.get_llm_config(user_id)
        except Exception as exc:  # noqa: BLE001 - 读配置失败不该让 ask 挂掉
            logger.warning(
                "读取用户 LLM 配置失败（回落服务端默认）：user=%s → %s",
                user_id,
                type(exc).__name__,
            )
        else:
            if record is not None:
                return ResolvedLlmConfig(
                    base_url=record.base_url,
                    api_key=record.api_key,
                    model=record.model,
                    timeout_s=settings.answer_timeout_s,
                    max_tokens=settings.answer_max_tokens,
                    temperature=settings.answer_temperature,
                    source=SOURCE_USER,
                    protocol=_user_protocol(settings, record.protocol),
                )
    return ResolvedLlmConfig(
        base_url=settings.answer_base_url,
        api_key=settings.answer_api_key,
        model=settings.answer_model,
        timeout_s=settings.answer_timeout_s,
        max_tokens=settings.answer_max_tokens,
        temperature=settings.answer_temperature,
        source=SOURCE_SERVER,
        protocol=_server_protocol(settings),
    )


def _server_protocol(settings: Settings) -> str:
    """服务端默认协议（未配置 → ``openai``）。非法值在 ``Settings.from_env`` 已拦。

    直接回落 ``DEFAULT_PROTOCOL`` 而不再次 ``normalize_protocol``：``Settings`` 可能被
    测试直接构造（绕过 ``from_env``）并传入任意字符串，构造 provider 时仍有最后一道校验。
    """
    return str(settings.answer_protocol or DEFAULT_PROTOCOL)


def _user_protocol(settings: Settings, raw: str | None) -> str:
    """用户配置的协议；旧行（迁移前写入）为 ``NULL`` → 回落服务端默认。

    为什么不是无条件回落 ``openai``：用户在设置页设了协议、服务端也设了协议时，
    “用户只改了模型名、没动协议”应当继续用他之前选的那份（服务端默认），
    而不是被一个空值重置成 openai。
    """
    if raw is None or not str(raw).strip():
        return _server_protocol(settings)
    return str(raw)


def build_provider_for(
    settings: Settings,
    *,
    user_id: str | None = None,
    db: MetaDB | None = None,
) -> AnswerProvider | None:
    """按解析出的配置构造 provider（**未配置 → ``None``**，调用方走 D-26 降级包）。

    直接构造 ``answer.HttpAnswerProvider``（而不是给 ``build_provider`` 加参数）：**本卡不改
    ``answer.py``**（它不在交付物所有权清单里），而该类本就是公开的、以参数接配置的——
    从用户配置构造与从环境变量构造走的是同一条 HTTP 实现，不存在两套行为。

    超时/``max_tokens``/温度一律取自 ``settings``：这三个是服务端的运行参数，不是用户可配项
    （§C-3 的请求体只有 ``{model, baseUrl, apiKey}``）。

    key 的脱敏交给 :class:`HttpAnswerProvider`（构造时 ``register_secret``，全仓日志脱敏的
    第二道防线），本模块不重复实现。
    """
    from nova_service.answer import HttpJsonProvider

    resolved = resolve_llm_config(settings, user_id=user_id, db=db)
    if not resolved.configured:
        return None
    return HttpJsonProvider(
        base_url=str(resolved.base_url),
        api_key=str(resolved.api_key),
        model=str(resolved.model),
        protocol=resolved.protocol,
        timeout_s=settings.answer_timeout_s,
    )


def provider_for_request(
    app: Any,
    *,
    user_id: str | None = None,
    db: MetaDB | None = None,
) -> AnswerProvider | None:
    """REST 面的 provider 缓存（与 ``answer.provider_for_app`` 同一职责，多了用户维度）。

    缓存键 = ``(Settings 身份, user_id, 用户配置的 updated_at)``：

    - ``Settings`` 是不可变对象、每 app 一个，用**身份比较**判断全局配置有没有变——
      不比较（也不存）任何 secret 内容（TASK-088 的既有口径）；
    - ``updated_at`` 让"用户刚改了配置"立刻失效，而不必重启服务（这是本卡相对 MCP 面的优势）；
    - ``user_id`` 保证 A 的 provider 不会被 B 复用（**隔离靠缓存键，不只靠解析逻辑**）。

    读库失败：不缓存，按无用户配置处理（即服务端默认）——与 :func:`resolve_llm_config`
    的降级口径一致。

    **测试接缝（与 TASK-088 的 ``answer.provider_for_app`` 逐字一致）**：
    ``app.state.answer_provider`` 是注入位——测试先在那里放一个假 provider，并把
    ``app.state.answer_provider_settings`` 设为**同一个** ``Settings`` 对象即可生效。
    没有这个接缝，没有真实 LLM 的单测就只能走降级分支（那样 §A 的"答案落库"会**永远测不到**）。
    """
    settings = getattr(getattr(app, "state", None), "settings", None)
    if not isinstance(settings, Settings):  # pragma: no cover - 只有手拼 app 才会走到
        return None
    injected = getattr(app.state, "answer_provider", None)
    if isinstance(injected, AnswerProvider) and (
        getattr(app.state, "answer_provider_settings", None) is settings
    ):
        return injected
    stamp: int | None = None
    if user_id and db is not None:
        try:
            record = db.get_llm_config(user_id)
        except Exception:  # noqa: BLE001 - 读配置失败 → 当没配（resolve 里已记 WARN 口径）
            record = None
        stamp = record.updated_at if record is not None else None
    key = (id(settings), user_id, stamp)
    cached = getattr(app.state, "llm_provider", None)
    if isinstance(cached, AnswerProvider) and getattr(app.state, "llm_provider_key", None) == key:
        return cached
    provider = build_provider_for(settings, user_id=user_id, db=db)
    app.state.llm_provider = provider
    app.state.llm_provider_key = key
    return provider
