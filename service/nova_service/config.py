"""nova-service 配置（TASK-030 §交付物）。

设计依据：``docs/design/Module/06-服务化与部署.md`` §2.4（可观测：secret 全部环境变量注入）、
``docs/contracts/PROCESS.md`` §3.8 R34（专用 ``local`` 命令：无鉴权、绑 127.0.0.1）。

口径（本卡冻结，后续卡只读不改）：

- 纯标准库实现（``os.environ`` + ``dataclasses``），**不引入 pydantic-settings**（依赖最小化）；
- ``data_root`` 与 core 的 ``NOVA_DATA_ROOT`` 同名同义（``~/.nova``，core 只管 ``projects/``）；
- 普通 ``serve`` 固定走完整账户与鉴权流程；只有显式 ``local`` 命令会设置 ``local_mode``；
- ``local_rescan_interval_s``（TASK-034 §C）：本地模式下检索前的懒重扫间隔，默认 2.0 秒，
  **0 表示禁用**（测试与"只读演示"）；只影响本地模式（远端模式走客户端上传）；
- 非法布尔值**显式报错**而不是静默取默认——``local_mode`` 决定是否要求鉴权，静默取真会让
  配置写错的人以为安全策略生效（诚实性优先于启动便利）；
- LLM 总结配置（``ANSWER_*``，TASK-088 / Module/04 §2）：**用户只需给 URL/KEY/MODEL 三个**，
  其余三项有内置默认值但同样允许环境变量覆盖。**未配置 = 未配置**（三个里的任一为空即视为
  未配置），``ask`` 走 D-26 降级包而不是启动报错（L5：开源产品冷启动体验）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nova_service import __version__
from nova_service.llmprotocol import normalize_protocol

__all__ = [
    "ADMIN_NAME_ENV",
    "ANSWER_API_KEY_ENV",
    "ANSWER_BASE_URL_ENV",
    "ANSWER_MAX_TOKENS_ENV",
    "ANSWER_MODEL_ENV",
    "ANSWER_TEMPERATURE_ENV",
    "ANSWER_TIMEOUT_S_ENV",
    "ANSWER_MAX_CONTEXT_TOKENS_ENV",
    "ANSWER_PROTOCOL_ENV",
    "ANSWER_PROVIDER_ENV",
    "EMBED_TPM_ENV",
    "EMBED_RPM_ENV",
    "COOKIE_SECURE_ENV",
    "DATA_ROOT_ENV",
    "DEFAULT_ANSWER_MAX_TOKENS",
    "DEFAULT_ANSWER_TEMPERATURE",
    "DEFAULT_ANSWER_TIMEOUT_S",
    "DEFAULT_ADMIN_NAME",
    "DEFAULT_DATA_ROOT",
    "DEFAULT_HOST",
    "DEFAULT_LOCAL_RESCAN_INTERVAL_S",
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_LOG_MAX_BYTES",
    "DEFAULT_LOG_RETENTION_DAYS",
    "DEFAULT_PORT",
    "DEFAULT_STORAGE_LIMIT_PER_PROJECT_BYTES",
    "DEFAULT_STORAGE_LIMIT_PER_USER_BYTES",
    "DEFAULT_STORAGE_WARN_RATIO",
    "LOCAL_RESCAN_INTERVAL_ENV",
    "LOG_BACKUP_COUNT_ENV",
    "LOG_DIRNAME",
    "LOG_MAX_BYTES_ENV",
    "LOG_RETENTION_DAYS_ENV",
    "PROJECTS_DIRNAME",
    "STORAGE_LIMIT_PER_PROJECT_ENV",
    "STORAGE_LIMIT_PER_USER_ENV",
    "STORAGE_WARN_RATIO_ENV",
    "Settings",
]

#: 数据根环境变量（与 core 的 ``nova_core.engine.DATA_ROOT_ENV`` 同名同义）。
DATA_ROOT_ENV = "NOVA_DATA_ROOT"
#: 懒重扫间隔环境变量（TASK-034 §C；0 = 禁用）。
LOCAL_RESCAN_INTERVAL_ENV = "NOVA_LOCAL_RESCAN_INTERVAL"
#: session cookie 的 Secure 属性（TASK-060；HTTPS 部署必须置 true）。
COOKIE_SECURE_ENV = "NOVA_COOKIE_SECURE"
#: 数据根子目录名（core 的 ``projects/``；元数据库文件名见 ``metadb.META_DB_FILENAME``）。
PROJECTS_DIRNAME = "projects"
#: 请求日志目录名（TASK-090 §A：落 ``{data_root}/logs/request.log``）。
LOG_DIRNAME = "logs"
#: 请求日志单文件上限（字节；超过即轮转，TASK-090 §A 窗口的"按 MB"维度）。
LOG_MAX_BYTES_ENV = "NOVA_LOG_MAX_BYTES"
#: 请求日志保留的轮转备份数（连同当前文件，窗口 = (备份数+1) × 单文件上限）。
LOG_BACKUP_COUNT_ENV = "NOVA_LOG_BACKUP_COUNT"
#: 请求日志保留天数（TASK-090 §A 窗口的"按天"维度；0 = 不按天清理，只按体积）。
LOG_RETENTION_DAYS_ENV = "NOVA_LOG_RETENTION_DAYS"
#: 默认：单文件 8 MiB × (9 备份 + 当前) ≈ 80 MiB 上界（单机 VPS 足够，且不会无限增长）。
DEFAULT_LOG_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 9
#: 默认保留 14 天（用户报错往往隔几天才反馈，太短查不到、太长无必要）。
DEFAULT_LOG_RETENTION_DAYS = 14

# ------------------------------------------------------------------ 存储配额（TASK-094 §B1）
#
# 默认值由用户 2026-09-14 拍板；**实测依据**（编排者测量，两仓库外推，样本很少）：
#
# | 仓库 | 可索引文件 | chunks | 索引占用 |
# |---|---|---|---|
# | cockpit-agents-py | 287 | 3416 | 29 MB |
# | nova 自身 | 399 | 4931 | 52 MB |
#
# 换算率 ≈ 10 KB / chunk → 单项目 500 MB ≈ 50,000 chunks，对中型项目（29 MB）有 17 倍余量。
# 本卡实施时在真机复测：cockpit-agents 索引目录 **28.05 MiB / 3416 chunks**（≈ 8.6 KB/chunk），
# 与上述外推一致。**不要为了"让默认值好看"而调它**——真实分布要靠上线后观察（TASK-093）。
#
# TASK-110 §1.5：**按角色的配额**（公测 500 MB / 内测 1 GB / 管理员 5 GB）在
# ``nova_service.roles.QUOTA_BY_ROLE``；下面这两个退化为**无角色信息时的兜底**
# （本地模式、未登录的隐式账户）。
#: 单项目存储上限（字节，默认 500 MB）。
STORAGE_LIMIT_PER_PROJECT_ENV = "NOVA_STORAGE_LIMIT_PER_PROJECT_BYTES"
#: 单用户存储总额上限（字节，默认 2 GB）。
STORAGE_LIMIT_PER_USER_ENV = "NOVA_STORAGE_LIMIT_PER_USER_BYTES"
#: 告警阈值比例（相对上限；默认 0.8 = 80%）。
STORAGE_WARN_RATIO_ENV = "NOVA_STORAGE_WARN_RATIO"
#: 首个管理员的账户名（TASK-110 §7.1：按名字指定，而不是靠“最早创建者”）。
#:
#: 为什么做成配置：这是**部署级**信息（谁是负责人随环境变），写死在代码里等于把某台机器的人员
#: 情况烧进产品。默认值 ``xuwenzheng`` 是本项目当前的实际部署者（卡内 §7.1 已拍板）。
ADMIN_NAME_ENV = "NOVA_ADMIN_NAME"
DEFAULT_ADMIN_NAME = "xuwenzheng"
#: 默认单项目上限：500 MiB。
DEFAULT_STORAGE_LIMIT_PER_PROJECT_BYTES = 500 * 1024 * 1024
#: 默认单用户上限：2 GiB（40G VPS 约容纳 20 个活跃用户）。
DEFAULT_STORAGE_LIMIT_PER_USER_BYTES = 2 * 1024 * 1024 * 1024
#: 默认告警阈值：上限的 80%（留出反应时间，而不是撞线才提醒）。
DEFAULT_STORAGE_WARN_RATIO = 0.8

# ------------------------------------------------------------------ LLM 总结（TASK-088）
# 用户只需给三个必填项；下面三项是内置默认值（可覆盖），口径见 Module/04 §2 参数表。
#: LLM 的 OpenAI-compatible base URL（如 ``http://host:8080/v1``）。
ANSWER_BASE_URL_ENV = "ANSWER_BASE_URL"
#: LLM 的 API key（**绝不进日志/响应/仓库/设置页**）。
ANSWER_API_KEY_ENV = "ANSWER_API_KEY"
#: LLM 模型名（nova 统一使用 ``deepseek-flash``；可配置）。
#:
#: 不写代码默认值：网关背后的真实路由由部署方维护（同一名字在不同网关可以是不同模型），
#: 因此“用哪个模型”是部署决策，必须显式给（未配 → ``answer_configured`` 为 False）。
ANSWER_MODEL_ENV = "ANSWER_MODEL"
#: 整体超时秒数（Module/04 §2：连接 10s / 整体 60s）。
ANSWER_TIMEOUT_S_ENV = "ANSWER_TIMEOUT_S"
#: 单次回答的 ``max_tokens``（Module/04 §2：3072）。
ANSWER_MAX_TOKENS_ENV = "ANSWER_MAX_TOKENS"
#: 采样温度（Module/04 §2：0.2——调查要事实不要创意）。
ANSWER_TEMPERATURE_ENV = "ANSWER_TEMPERATURE"
#: 该 LLM 的**最大上下文窗口**（token）。仅用于设置页展示，不参与任何调优。
#:
#: 为什么做成配置而不是代码里的模型名映射：服务端无法可靠地知道用户自建网关背后的
#: 真实模型与其窗口（同一个 ``deepseek-flash`` 名字在不同网关可以是不同东西）。
#: 写死映射会把“猜测”当作事实展示（TASK-107 修的真实缺陷：代码里只认一个硬编码的
#: 已废弃模型名，与实际使用的模型名不一致，页面上因此恒为空）。
ANSWER_MAX_CONTEXT_TOKENS_ENV = "ANSWER_MAX_CONTEXT_TOKENS"
#: 该 LLM 厂商名（仅展示用；不填则按模型名推测）。
ANSWER_PROVIDER_ENV = "ANSWER_PROVIDER"
#: 上游协议（TASK-113 / D-47）：``openai`` / ``responses`` / ``anthropic``。
#:
#: 为什么需要它：同一网关的不同模型可能只开放不同协议（实测 2026-09-16：
#: ``deepseek-v4-flash`` 只声明 ``ANTHROPIC``/``RESPONSES``，``deepseek-v4-pro`` **仅**
#: ``ANTHROPIC``）。升级前只实现了 OpenAI Chat Completions，用户选了其它协议的模型会得到
#: “保存成功但 ask 持续 503”的静默失灵。未配置 → ``openai``（与升级前逐字相同）。
ANSWER_PROTOCOL_ENV = "ANSWER_PROTOCOL"
#: Embedding 的速率配额（仅展示用；TPM = token/分钟，RPM = 请求/分钟）。
#: 同样不写死在代码里：配额随账号档位变化，属“账号属性”而非“模型属性”。
EMBED_TPM_ENV = "EMBED_TPM"
EMBED_RPM_ENV = "EMBED_RPM"
#: 三个必填项的默认值（未配置任一 → ``answer_configured`` 为 False，``ask`` 走降级包）。
DEFAULT_ANSWER_TIMEOUT_S = 60.0
DEFAULT_ANSWER_MAX_TOKENS = 3072
DEFAULT_ANSWER_TEMPERATURE = 0.2
#: 默认懒重扫间隔（秒）：本地模式下检索前最多每 2s 扫一次（Module/05 §3.6 的 freshness 语义）。
DEFAULT_LOCAL_RESCAN_INTERVAL_S = 2.0
#: 默认数据根（core 的 ``DEFAULT_DATA_ROOT`` 同值；service 只读 settings，不重复定义语义）。
DEFAULT_DATA_ROOT = Path.home() / ".nova"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_LOG_LEVEL = "info"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class Settings:
    """服务运行配置（不可变；测试用 ``dataclasses.replace`` 派生变体）。"""

    data_root: Path = DEFAULT_DATA_ROOT
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL
    local_mode: bool = False
    local_rescan_interval_s: float = DEFAULT_LOCAL_RESCAN_INTERVAL_S
    #: session cookie 的 ``Secure``（TASK-060）：本地 http 调试为 False，上云必须 True。
    cookie_secure: bool = False
    #: 请求日志窗口（TASK-090 §A）：单文件上限 / 备份数 / 保留天数。
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT
    log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS
    #: 存储配额（TASK-094 §B1）：**0 表示不限**（本地开发与测试用；配额判定恒为 ``ok``）。
    #: TASK-110 起，已登录用户的实际上限走 ``roles.QUOTA_BY_ROLE``（见 ``quota.py``），
    #: 这两个值退化为"没有角色信息时"的兜底。
    storage_limit_per_project_bytes: int = DEFAULT_STORAGE_LIMIT_PER_PROJECT_BYTES
    storage_limit_per_user_bytes: int = DEFAULT_STORAGE_LIMIT_PER_USER_BYTES
    #: 告警阈值比例（相对上限）。比例语义下**0 不表示不限**，因此只接受 (0, 1]；
    #: 想关掉告警就设上限为 0（那样配额恒为 ``ok``）。
    storage_warn_ratio: float = DEFAULT_STORAGE_WARN_RATIO
    #: LLM 总结（TASK-088）：三个必填项＋三个可覆盖默认值。
    answer_base_url: str | None = None
    answer_api_key: str | None = None
    answer_model: str | None = None
    answer_timeout_s: float = DEFAULT_ANSWER_TIMEOUT_S
    answer_max_tokens: int = DEFAULT_ANSWER_MAX_TOKENS
    answer_temperature: float = DEFAULT_ANSWER_TEMPERATURE
    #: 展示用元数据（TASK-107）：模型上下文窗口与厂商。**不参与任何调优**，
    #: 未配置时设置页如实显示 `—`，不猜。
    answer_max_context_tokens: int | None = None
    answer_provider: str | None = None
    #: 上游协议（TASK-113 / D-47）；``None`` = 未配置 → 用 :data:`llmprotocol.DEFAULT_PROTOCOL`。
    #:
    #: 存字符串而不是枚举对象：``Settings`` 是 dataclass，测试大量用 ``dataclasses.replace``
    #: 派生变体，字符串最省事；合法性在构造 provider 时由 ``llmprotocol.normalize_protocol``
    #: 检查（非法值显式报错，不静默回落）。
    answer_protocol: str | None = None
    #: Embedding 速率配额（展示用）：TPM / RPM。未配置 → 页面显示 `—`。
    embed_tpm: int | None = None
    embed_rpm: int | None = None
    #: 首个管理员的名字（TASK-110 §7.1）：迁移 / 启动时把它提为 ``role='admin'``。
    admin_name: str = DEFAULT_ADMIN_NAME
    version: str = __version__

    @property
    def auth_required(self) -> bool:
        """是否要求凭据（= 非本地模式，Module/06 §2.2）；本地模式免鉴权（R34）。"""
        return not self.local_mode

    @property
    def answer_configured(self) -> bool:
        """LLM 是否已配置（三个必填项都非空；Module/04 §6）。

        **不是**“能连上”：未配置 → ``ask`` 走降级包并提示管理员去配；
        配了但打不通 → 另一条降级路径（§6 故障矩阵）。
        """
        return bool(self.answer_base_url and self.answer_model and self.answer_api_key)

    @property
    def answer_missing_env(self) -> tuple[str, ...]:
        """未配置时缺哪几个环境变量（只回**变量名**，不回值/不回长度）。"""
        missing: list[str] = []
        if not self.answer_base_url:
            missing.append(ANSWER_BASE_URL_ENV)
        if not self.answer_api_key:
            missing.append(ANSWER_API_KEY_ENV)
        if not self.answer_model:
            missing.append(ANSWER_MODEL_ENV)
        return tuple(missing)

    @property
    def storage_quota_enabled(self) -> bool:
        """是否有任何一份配额生效（两个上限都为 0 → 不限，判定恒为 ``ok``）。"""
        return self.storage_limit_per_project_bytes > 0 or self.storage_limit_per_user_bytes > 0

    @property
    def meta_db_path(self) -> Path:
        """``nova-meta.db`` 的落点（与 core 的 ``projects/`` 同级，Module/06 §4-A）。"""
        return self.data_root / "nova-meta.db"

    @property
    def log_dir(self) -> Path:
        """请求日志目录（TASK-090 §A：``{data_root}/logs/``）。"""
        return self.data_root / LOG_DIRNAME

    @property
    def request_log_path(self) -> Path:
        """请求日志文件（TASK-090 §A：JSONL，轮转后为 ``request.log.1`` … ``.N``）。"""
        return self.log_dir / "request.log"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """从环境变量构造配置（缺省即本卡默认值）。"""
        source: Mapping[str, str] = os.environ if env is None else env
        raw_root = source.get(DATA_ROOT_ENV)
        return cls(
            data_root=Path(raw_root).expanduser() if raw_root else DEFAULT_DATA_ROOT,
            # 部署统一走完整账户流程；专用 `local` CLI 会在启动前显式 replace 为 True。
            local_mode=False,
            local_rescan_interval_s=_as_float(
                source.get(LOCAL_RESCAN_INTERVAL_ENV),
                LOCAL_RESCAN_INTERVAL_ENV,
                default=DEFAULT_LOCAL_RESCAN_INTERVAL_S,
            ),
            cookie_secure=_as_bool(source.get(COOKIE_SECURE_ENV), COOKIE_SECURE_ENV, default=False),
            log_max_bytes=_as_int(
                source.get(LOG_MAX_BYTES_ENV), LOG_MAX_BYTES_ENV, default=DEFAULT_LOG_MAX_BYTES
            ),
            log_backup_count=_as_int(
                source.get(LOG_BACKUP_COUNT_ENV),
                LOG_BACKUP_COUNT_ENV,
                default=DEFAULT_LOG_BACKUP_COUNT,
            ),
            log_retention_days=_as_int(
                source.get(LOG_RETENTION_DAYS_ENV),
                LOG_RETENTION_DAYS_ENV,
                default=DEFAULT_LOG_RETENTION_DAYS,
            ),
            # TASK-094 §B1：配额（0 = 不限；非法/负数显式报错，与日志窗口同纪律）。
            storage_limit_per_project_bytes=_as_non_negative_int(
                source.get(STORAGE_LIMIT_PER_PROJECT_ENV),
                STORAGE_LIMIT_PER_PROJECT_ENV,
                default=DEFAULT_STORAGE_LIMIT_PER_PROJECT_BYTES,
            ),
            storage_limit_per_user_bytes=_as_non_negative_int(
                source.get(STORAGE_LIMIT_PER_USER_ENV),
                STORAGE_LIMIT_PER_USER_ENV,
                default=DEFAULT_STORAGE_LIMIT_PER_USER_BYTES,
            ),
            storage_warn_ratio=_as_ratio(
                source.get(STORAGE_WARN_RATIO_ENV),
                STORAGE_WARN_RATIO_ENV,
                default=DEFAULT_STORAGE_WARN_RATIO,
            ),
            answer_base_url=(source.get(ANSWER_BASE_URL_ENV) or "").strip() or None,
            answer_api_key=(source.get(ANSWER_API_KEY_ENV) or "").strip() or None,
            answer_model=(source.get(ANSWER_MODEL_ENV) or "").strip() or None,
            answer_timeout_s=_as_float(
                source.get(ANSWER_TIMEOUT_S_ENV),
                ANSWER_TIMEOUT_S_ENV,
                default=DEFAULT_ANSWER_TIMEOUT_S,
            ),
            answer_max_tokens=_as_int(
                source.get(ANSWER_MAX_TOKENS_ENV),
                ANSWER_MAX_TOKENS_ENV,
                default=DEFAULT_ANSWER_MAX_TOKENS,
            ),
            answer_temperature=_as_float(
                source.get(ANSWER_TEMPERATURE_ENV),
                ANSWER_TEMPERATURE_ENV,
                default=DEFAULT_ANSWER_TEMPERATURE,
            ),
            answer_max_context_tokens=_as_optional_int(
                source.get(ANSWER_MAX_CONTEXT_TOKENS_ENV), ANSWER_MAX_CONTEXT_TOKENS_ENV
            ),
            answer_provider=(source.get(ANSWER_PROVIDER_ENV) or "").strip() or None,
            # TASK-113：协议名归一（容别名、大小写）；非法值**显式报错**而不是静默取默认。
            answer_protocol=_as_protocol(source.get(ANSWER_PROTOCOL_ENV)),
            embed_tpm=_as_optional_int(source.get(EMBED_TPM_ENV), EMBED_TPM_ENV),
            embed_rpm=_as_optional_int(source.get(EMBED_RPM_ENV), EMBED_RPM_ENV),
            admin_name=(source.get(ADMIN_NAME_ENV) or "").strip() or DEFAULT_ADMIN_NAME,
        )


def _as_bool(raw: str | None, name: str, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ValueError(f"环境变量 {name} 必须是布尔值（{sorted(_TRUTHY | _FALSY)}），收到 {raw!r}")


def _as_int(raw: str | None, name: str, *, default: int) -> int:
    """解析**正整数**（与 :func:`_as_float` 同纪律：非法值显式报错，不静默取默认）。

    只用于"0 无意义且危险"的项（如日志单文件上限：0 会变成无限增长）。
    允许 0 的项（配额）用 :func:`_as_non_negative_int`。
    """
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数，收到 {raw!r}") from None
    if value < 1:
        raise ValueError(f"环境变量 {name} 必须 ≥ 1，收到 {raw!r}") from None
    return value


def _as_non_negative_int(raw: str | None, name: str, *, default: int) -> int:
    """解析**非负整数**（``0`` 是合法且有意义的值：配额里表示"不限"）。"""
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数，收到 {raw!r}") from None
    if value < 0:
        raise ValueError(f"环境变量 {name} 不能为负（0 表示不限），收到 {raw!r}")
    return value


def _as_optional_int(raw: str | None, name: str) -> int | None:
    """可缺省的整数配置：缺失/空串 → ``None``；非法值**显式报错**。

    与 :func:`_as_int` 的差别是“没配”是一个合法状态（设置页显示 `—`），而“配错”
    仍然要报——静默回落会让用户以为配额已生效（与 ``local_mode`` 的纪律一致）。
    """
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数，收到 {raw!r}") from None


def _as_protocol(raw: str | None) -> str | None:
    """解析 ``ANSWER_PROTOCOL``（TASK-113）：别名归一，非法值显式报错。

    为什么在这里就归一而不是留到构造 provider：启动时就该知道配置写错了。
    为什么非法值不静默回落 ``openai``：用户把 ``responses`` 拼错时，静默回落会让他看到
    “服务起来了、保存成功了、ask 仍然 503”——正是 TASK-113 要消灭的静默失灵。
    """
    if raw is None or not raw.strip():
        return None
    return normalize_protocol(raw)


def _as_ratio(raw: str | None, name: str, *, default: float) -> float:
    """解析 ``(0, 1]`` 的比例（告警阈值）。

    为什么不允许 0：比例语义下 ``0`` 会被读成"用量一超过 0 就告警"（正好相反），
    静默接受就制造了一个反向开关。要关告警请把**上限**设为 0（配额恒 ``ok``）。
    """
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字，收到 {raw!r}") from None
    if not 0 < value <= 1:
        raise ValueError(
            f"环境变量 {name} 必须落在 (0, 1] 区间（0 会被读成「一超就告警」；"
            f"要关闭告警请把配额上限设为 0），收到 {raw!r}"
        )
    return value


def _as_float(raw: str | None, name: str, *, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字，收到 {raw!r}") from None
    if value < 0:
        raise ValueError(f"环境变量 {name} 不能为负（0 表示禁用），收到 {raw!r}")
    return value
