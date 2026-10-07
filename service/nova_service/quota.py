"""存储配额判定与 tool 内告警（TASK-094 §B2/§B3）。

用户需求原文（2026-09-14）：

> 用户存在内存上限，需要在 tool 的内容中报警，提示 Agent 提醒用户进行管理内存。

因此本模块的产出**不是日志、不是指标**，而是一段能直接放进 tool 返回内容、被 Agent **原话转述
给用户**的 Markdown（:func:`QuotaStatus.warning_markdown`）。日志里再写一份是补充，不是替代。

四条口径（本卡冻结；TASK-110 起第 1 条改口径）：

1. **检索只告警、上传硬拒**（用户 2026-09-15 拍板）：TASK-094 的告警语义在检索路径上
   **逐字不变**（检索不会让占用变大，拒绝它毫无意义），但**上传**超过角色配额时拒绝——
   否则“配额”只是一句文案。硬拒实现在 :func:`enforce_upload_limit`。
2. **旁路纪律**（与 TASK-084 审计同一纪律）：配额统计是**旁路**——统计失败、目录读不到、
   元数据库不可用，都**不得**让检索失败。两个入口分工：
   - :func:`warning_for` 给检索路径用，**吞掉所有异常**返回 ``None``；
   - :func:`check_quota` 给测试与只读端点用，抛异常以便暴露缺陷。
3. **上限为 0 = 不限**（``Settings.storage_quota_enabled`` 为假且无角色配额时）：判定恒为
   ``ok``，tool 返回里**不出现告警节**（不打扰本地开发与测试）。
4. **口径诚实**（TASK-083 纪律）：不限时 ``ratio`` 是 ``None`` 而不是 0——"没有上限"与
   "用了 0%"是两件事；未测量同样不填 0。

尺寸来源：``{data_root}/projects/{id}`` 的递归求和（:func:`nova_service.stats.dir_size_bytes`），
即**索引数据的磁盘占用**（``index.db`` + ``vectors/`` + ``blobs/`` + 同步账本），
**不含**用户的源码仓库。真机实测：``cockpit-agents`` 项目 18 个文件 / 3416 chunks 的目录
求和约 0.8 ms（28 MiB），因此每次检索前做一次求和是可接受的成本。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nova_service.config import Settings
from nova_service.errors import ApiError
from nova_service.logging import get_logger, redact_text
from nova_service.roles import quota_bytes_for

if TYPE_CHECKING:  # 仅类型：避免 stats ↔ quota 的导入环（见 project_usage_bytes）
    from nova_service.metadb import MetaDB
    from nova_service.runtime import EngineManager

__all__ = [
    "STATUS_EXCEEDED",
    "STATUS_OK",
    "STATUS_WARNING",
    "STORAGE_WARNING_HEADING",
    "QuotaDimension",
    "QuotaStatus",
    "append_warning",
    "check_quota",
    "effective_user_limit_bytes",
    "enforce_upload_limit",
    "format_bytes",
    "project_usage_bytes",
    "status_from_sizes",
    "user_usage_bytes",
    "visible_project_ids",
    "warning_for",
]

logger = get_logger("nova_service.quota")

#: 三态（:func:`check_quota` 的返回值与 web 的显示分支共用同一套词）。
STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_EXCEEDED = "exceeded"

#: tool 返回里那一节的标题（**冻结**：测试与 Agent 侧都以它为准）。
STORAGE_WARNING_HEADING = "### Storage Warning"

#: 控制台路径（告警文案指引用户去删项目；与 web 的路由一致）。
_CONSOLE_PATH = "/"


@dataclass(frozen=True, slots=True)
class QuotaDimension:
    """一个维度的用量（单用户总额 或 单项目）。"""

    used_bytes: int
    #: 上限；``0`` 表示**不限**（此时 :attr:`ratio` 与 :attr:`status` 见下）。
    limit_bytes: int
    #: 已用 / 上限；**不限时为 ``None``**（"没有上限"不是"用了 0%"）。
    ratio: float | None
    status: str

    @property
    def unlimited(self) -> bool:
        return self.limit_bytes <= 0

    def to_json(self) -> dict[str, Any]:
        return {
            "usedBytes": self.used_bytes,
            "limitBytes": self.limit_bytes,
            "ratio": self.ratio,
            "status": self.status,
            "unlimited": self.unlimited,
        }


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """一次配额判定的完整结果（用户维度 + 项目维度）。"""

    project_id: str
    user: QuotaDimension
    project: QuotaDimension
    #: 生效的告警阈值比例（回显给 UI，便于解释"为什么现在报警"）。
    warn_ratio: float

    @property
    def status(self) -> str:
        """总体状态 = 两个维度里**最差**的那个（``exceeded`` > ``warning`` > ``ok``）。"""
        order = {STATUS_OK: 0, STATUS_WARNING: 1, STATUS_EXCEEDED: 2}
        return max((self.user.status, self.project.status), key=lambda item: order[item])

    @property
    def should_warn(self) -> bool:
        return self.status != STATUS_OK

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "warnRatio": self.warn_ratio,
            "projectId": self.project_id,
            "user": self.user.to_json(),
            "project": self.project.to_json(),
        }

    def warning_markdown(self) -> str | None:
        """tool 返回里要追加的那一节；``ok``/不限 → ``None``（**不打扰**）。

        文案是给 **Agent 读并转述**的：每条都以"请提醒用户"收尾，并说清"去哪儿删、删了会怎样"
        （配合 §D 的删除入口形成闭环）。超限时**明确写出"不阻断"**——否则 Agent 可能自作主张
        停止索引，而那不是我们的行为。
        """
        if not self.should_warn:
            return None
        lines = [STORAGE_WARNING_HEADING]
        lines.extend(_dimension_lines(self.user, subject="当前账户的索引数据"))
        if self.project.status != STATUS_OK:
            lines.extend(
                _dimension_lines(self.project, subject=f"项目 {self.project_id} 的索引数据")
            )
        if self.status == STATUS_EXCEEDED:
            lines.append(
                "- 已超出上限：**检索与已有项目的使用不受影响**，但**新的索引上传会被拒绝**"
                f"（请提醒用户去控制台（{_CONSOLE_PATH}）删除不再需要的项目以释放空间。"
                "删除只影响索引数据，源码文件不受影响，需要时重新索引即可。）"
            )
        else:
            lines.append(
                f"- 请提醒用户：可在控制台（{_CONSOLE_PATH}）查看各项目占用，"
                "删除不再需要的项目以释放空间。删除只影响索引数据，源码文件不受影响。"
            )
        return "\n".join(lines)


def _dimension_lines(dimension: QuotaDimension, *, subject: str) -> list[str]:
    """一个维度的一行（用量 / 上限 / 百分比；不限时如实说"未设上限"）。"""
    if dimension.unlimited:
        return [f"- {subject}已用 {format_bytes(dimension.used_bytes)}（未设上限）。"]
    percent = f"{dimension.ratio * 100:.0f}%" if dimension.ratio is not None else "—"
    over = "，已超出上限" if dimension.status == STATUS_EXCEEDED else ""
    return [
        f"- {subject}已用 {format_bytes(dimension.used_bytes)} / "
        f"上限 {format_bytes(dimension.limit_bytes)}（{percent}{over}）。"
    ]


def format_bytes(size: int) -> str:
    """字节数 → 人读字符串（与 web 的 ``formatBytes`` 同一套单位与舍入）。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MiB"
    return f"{size / 1024 / 1024 / 1024:.2f} GiB"


def effective_user_limit_bytes(
    settings: Settings, *, role: str | None = None, override: int | None = None
) -> int:
    """**该用户实际生效**的单用户索引空间上限（字节）——唯一口径。[TASK-110]

    三条优先级（从高到低），刻意集中在这一个函数里：

    1. ``override``（后台对**单个人**改的配额）：运营的手工决定优先于一切；
    2. ``role`` 非空 → ``roles.QUOTA_BY_ROLE``（产品配额：公测 500 MB / 内测 1 GB / 管理员 5 GB）；
    3. 否则 ``Settings.storage_limit_per_user_bytes``（本地模式、未登录的隐式账户、
       手工构造的旧调用方）。

    为什么必须是一个函数：这个值有**两个**消费者——页面展示（``/api/auth/me`` 的
    ``capabilities.quotaBytes``、``/api/account/overview``）与**上传硬拒**。两处各算一遍
    必然会漂移，而漂移的表现是"页面说还有 200 MB、上传却被拒"，用户只能猜。

    ``0`` 表示不限（TASK-094 口径）。
    """
    if override is not None and override >= 0:
        return int(override)
    if role:
        return quota_bytes_for(role)
    return int(settings.storage_limit_per_user_bytes)


def enforce_upload_limit(
    manager: EngineManager,
    settings: Settings,
    *,
    project_id: str,
    db: MetaDB | None = None,
    user_id: str | None = None,
    role: str | None = None,
    override: int | None = None,
    incoming_bytes: int = 0,
) -> None:
    """上传前判定用户维度配额，超限**拒绝**（抛 :class:`ApiError`）。[TASK-110]

    与 :func:`warning_for` 的分工（用户 2026-09-15 拍板改口径）：

    - **检索**仍然只告警不阻断（TASK-094 §B2 不变——检索不会让占用变大，拒绝它毫无意义）；
    - **上传**超过角色配额时硬拒（413）——否则"配额"只是一句文案，用户能无限往内写。

    三个必读细节：

    1. ``incoming_bytes`` 计入本次批次：否则用户会在"刚好还差 1 KB"时传上一个 200 MB 的包，
       而且每批都刚好在限内；
    2. **失败开放**（fail-open）：统计失败、库读不到、目录读不到时**放行**。本函数在
       "数据传一半失败"这个已知风险（卡内 §7-3）与"完全不加限制"之间选后者：
       错误地拦住合法上传，伤害比偶尔多放一点空间大得多，而且后者是可通过设置项修正的；
    3. 不限（上限为 0）时不做任何目录遍历——与 TASK-094 的零成本口径一致。
    """
    limit = effective_user_limit_bytes(settings, role=role, override=override)
    if limit <= 0:
        return
    try:
        ids = visible_project_ids(manager, db, user_id)
        used = user_usage_bytes(manager, ids)
    except Exception as exc:  # noqa: BLE001 - 旁路：统计失败不得拦住上传（fail-open）
        logger.warning(
            "配额统计失败（放行本次上传）：project=%s → %s",
            project_id,
            redact_text(f"{type(exc).__name__}: {exc}"),
        )
        return
    projected = used + max(0, int(incoming_bytes))
    if projected <= limit:
        return
    raise ApiError(
        code="quota_exceeded",
        message=(
            f"索引空间已超限：本次上传后约 {format_bytes(projected)} / 上限 {format_bytes(limit)}"
            f"（已用 {format_bytes(used)}）。当前身份的额度是固定的（不支持购买）；"
            "请在控制台删除不再需要的项目后重试，或联系管理员调整配额。"
        ),
        status=413,
    )


def project_usage_bytes(manager: EngineManager, project_id: str) -> int:
    """单项目的索引数据磁盘占用（递归求和；目录不存在 → 0）。

    局部导入 :func:`nova_service.stats.dir_size_bytes`（而不是模块抬头）：``stats`` 反过来要用
    本模块的 :func:`status_from_sizes`（概览页的配额判定），模块级互相 import 会形成导入环
    （先被导入的那一侧在环里拿不到尚未定义的名字）。本仓库已有同类的局部导入先例
    （``mcp.build_answer_provider`` / ``auth._embedding_config``）。
    """
    from nova_service.stats import dir_size_bytes

    return dir_size_bytes(manager.project_dir(project_id))


def user_usage_bytes(manager: EngineManager, project_ids: Iterable[str]) -> int:
    """一个用户的索引数据总额（其**全部**项目之和）。"""
    return sum(project_usage_bytes(manager, project_id) for project_id in project_ids)


def visible_project_ids(
    manager: EngineManager, db: MetaDB | None, user_id: str | None
) -> list[str]:
    """当前身份可见的 projectId（**与 ops/projects 路由同一口径**）。

    - 无账户（本地模式 R34）或元数据库缺失 → 全量（``None`` 用户语义）；
    - 有账户 → 只取其已归属的项目（TASK-061 的逻辑授权层）。

    为什么复用同一口径而不是各写一份：用量统计若把别人的项目算进来，告警数字就是假的
    （DoD 的"多用户隔离：A 的用量不计入 B 的额度"正是钉这一点）。
    """
    all_ids = [str(item["projectId"]) for item in manager.list_projects()]
    if db is None or user_id is None:
        return all_ids
    owned = set(db.list_projects(user_id))
    return [project_id for project_id in all_ids if project_id in owned]


def check_quota(
    manager: EngineManager,
    settings: Settings,
    *,
    project_id: str,
    project_ids: Sequence[str] | None = None,
    user_limit: int | None = None,
) -> QuotaStatus:
    """判定当前用量落在哪个状态（**可能抛异常**：给测试与只读端点用）。

    检索路径请用 :func:`warning_for`——它吞掉异常（旁路纪律）。

    ``project_ids`` 是"该用户全部项目"的列表。**给了就用给的**（哪怕空列表——那如实地表示
    "这个账户没有项目"，用量就是 0）；不传（``None``）才退化成只算当前项目（调用方拿不到
    归属信息时至少给出项目维度的真话，**不编造**一个用户总额）。

    ``user_limit``：该用户实际生效的上限（TASK-110 的按角色配额；见
    :func:`effective_user_limit_bytes`）；不传用 ``Settings`` 兜底。
    """
    ids = list(project_ids) if project_ids is not None else [project_id]
    return status_from_sizes(
        {candidate: project_usage_bytes(manager, candidate) for candidate in ids},
        settings,
        project_id=project_id,
        user_limit=user_limit,
    )


def status_from_sizes(
    sizes: Mapping[str, int],
    settings: Settings,
    *,
    project_id: str,
    user_limit: int | None = None,
) -> QuotaStatus:
    """已知各项目占用时的判定（**不重复遍历目录**）。

    给已经拿到 ``describe_project`` 的 ``diskBytes`` 的调用方用（如 ``/api/account/overview``：
    它本来就有每个项目的占用，再走一遍 :func:`check_quota` 会把同一批目录重算一次）。
    两个入口共用 :func:`_judge`，因此"怎么才算 warning"只有一份实现。

    ``user_limit``（TASK-110）：该用户**实际生效**的上限（按角色 / 后台覆盖 / 本地模式兜底）。
    不传则回落 ``Settings`` 的全局默认（旧调用方与单测口径逐字不变）。
    """
    resolved_user_limit = (
        int(settings.storage_limit_per_user_bytes)
        if user_limit is None
        else int(user_limit)
    )
    return QuotaStatus(
        project_id=project_id,
        user=_judge(
            sum(max(0, int(value)) for value in sizes.values()),
            resolved_user_limit,
            settings.storage_warn_ratio,
        ),
        project=_judge(
            max(0, int(sizes.get(project_id, 0))),
            settings.storage_limit_per_project_bytes,
            settings.storage_warn_ratio,
        ),
        warn_ratio=settings.storage_warn_ratio,
    )


def _judge(used: int, limit: int, warn_ratio: float) -> QuotaDimension:
    """用量 → 三态（``limit <= 0`` → 不限，恒 ``ok``）。"""
    if limit <= 0:
        return QuotaDimension(used_bytes=used, limit_bytes=0, ratio=None, status=STATUS_OK)
    ratio = used / limit
    if used >= limit:
        status = STATUS_EXCEEDED
    elif ratio >= warn_ratio:
        status = STATUS_WARNING
    else:
        status = STATUS_OK
    return QuotaDimension(used_bytes=used, limit_bytes=limit, ratio=ratio, status=status)


def warning_for(
    manager: EngineManager,
    settings: Settings,
    *,
    project_id: str,
    db: MetaDB | None = None,
    user_id: str | None = None,
    role: str | None = None,
    override: int | None = None,
) -> str | None:
    """给 tool 返回用的告警 Markdown（**绝不让检索失败**，TASK-084 同一纪律）。

    三条防线：

    1. 上限为 0（不限）→ 直接返回 ``None``，连目录都不去遍历；
    2. 归属解析与目录求和包在 ``try`` 里：任何失败只记 WARN 并返回 ``None``；
    3. 文案生成同样包住——"提醒"这件事不该有让请求 500 的能力。

    调用方拿到的是 ``str | None``，直接接在 tool 文本之后即可（:func:`append_warning`）。

    ``role`` / ``override``（TASK-110）：按角色配额判定（否则告警说的上限会是全局默认值，
    与用户实际被硬拒的那根线不一致）。
    """
    limit = effective_user_limit_bytes(settings, role=role, override=override)
    if limit <= 0 and not settings.storage_quota_enabled:
        return None
    try:
        ids = visible_project_ids(manager, db, user_id)
        status = check_quota(
            manager, settings, project_id=project_id, project_ids=ids, user_limit=limit
        )
        return status.warning_markdown()
    except Exception as exc:  # noqa: BLE001 - 旁路：配额判定失败不得让检索失败
        logger.warning(
            "存储配额判定失败（检索照常）：%s → %s",
            project_id,
            redact_text(f"{type(exc).__name__}: {exc}"),
        )
        return None


def append_warning(text: str, warning: str | None) -> str:
    """把告警节追加到 tool 文本末尾（``None`` 时**原样返回**，不产生多余空行）。"""
    if not warning:
        return text
    return f"{text}\n\n{warning}"
