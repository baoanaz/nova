"""邀请码的生成与核销（TASK-110 §3.1/§3.3）。

形态（用户 2026-09-15 拍板）：**首字母即类型 + 5 位随机**，共 6 位大写字母，如 ``B3NQ8W``。

为什么首字母定类型而不是"6 位纯随机 + 类型另存字段"（卡内 §7-1 曾建议后者）：用户拍板可读性
优先——管理员一眼能分辨自己发出的码是哪一类。代价是**类型可从码面推断**，因此：

- 码的随机部分仍有 26^5 ≈ 1.19 千万种，盲猜不可行；
- 但**必须先有码才谈得上注册**，且 `max_uses` / 有效期 / 撤销三件套仍然生效，
  所以"能看出类型"不构成越权路径（拿到 A 类码的人本来就被授予管理员）。

本模块**不 import FastAPI**（纯逻辑 + 一次数据库调用），因此单测可以直接驱动它，
不必起应用；HTTP 状态码的映射在 ``routers/auth`` 与 ``routers/admin`` 里做。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from nova_service.roles import KIND_TO_ROLE, KINDS

if TYPE_CHECKING:  # 仅类型：避免与 metadb 形成导入环（metadb 不 import 本模块）
    from nova_service.metadb import MetaDB

__all__ = [
    "ALPHABET",
    "CODE_LENGTH",
    "InviteRejected",
    "RANDOM_LENGTH",
    "REJECT_EXHAUSTED",
    "REJECT_EXPIRED",
    "REJECT_REVOKED",
    "REJECT_UNKNOWN",
    "generate_code",
    "is_well_formed",
    "kind_of",
    "normalize_code",
    "redeem",
    "role_for_code",
]

#: 码面字母表（**A-Z + 0-9**，不做 I/O 剔除）。
#:
#: 卡内 §1.1 的散文写的是"6 位大写字母"，但同卡 §7-1 与 §5 的**示例码面**都是 ``A7K2M9`` /
#: ``B3NQ8W`` / ``C05RT2``（含数字），用户 2026-09-15 也是看着这些示例拍的板。
#: 以示例为准：含数字把空间从 26^5 ≈ 1.19e7 提到 36^5 ≈ 6.0e7。
#:
#: 不剔除易混字符（``0``/``O``、``1``/``I``）：那会让"格式合法但永远核销不到"的码产生，
#: 用户会以为是系统 bug。
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
#: 码总长度（首字母 + 5 位随机）。
CODE_LENGTH = 6
#: 随机部分长度。
RANDOM_LENGTH = CODE_LENGTH - 1

#: 核销失败的原因（调用方据此给**具体**文案；码本身不回显给未登录者以外的场景）。
REJECT_UNKNOWN = "unknown"
REJECT_REVOKED = "revoked"
REJECT_EXPIRED = "expired"
REJECT_EXHAUSTED = "exhausted"


class InviteRejected(Exception):
    """邀请码不可用（不存在 / 已失效 / 已过期 / 已用尽）。

    为什么不区分成 HTTP 层的 400/403/404：注册是一个**未登录**端点，任何区分都会变成
    "这个码存不存在"的探测面。文案由路由层统一成一句可操作的话（"邀请码无效或已用尽"），
    细粒度原因只进日志。
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"邀请码不可用：{code}（{reason}）")
        self.code = code
        self.reason = reason


def normalize_code(raw: str | None) -> str:
    """用户输入 → 规范形态（去空白 + 转大写）。

    为什么要转大写：码面只有大写字母，而用户从聊天软件复制粘贴、手机自动首字母大写都会
    带上小写。把"大小写不符"变成"核销失败"是最没必要的摩擦（TASK-081 的密码放宽同一取向）。
    """
    return (raw or "").strip().upper()


def is_well_formed(code: str) -> bool:
    """是否形如 ``[A-C][A-Z]{5}``（**只看形状**，不代表码存在或可用）。"""
    if len(code) != CODE_LENGTH:
        return False
    if code[0] not in KINDS:
        return False
    return all(char in ALPHABET for char in code)


def kind_of(code: str) -> str | None:
    """码的首字母类型（形状不合法 → ``None``）。"""
    normalized = normalize_code(code)
    return normalized[0] if is_well_formed(normalized) else None


def role_for_code(code: str) -> str | None:
    """码 → 角色（形状不合法 → ``None``）。"""
    kind = kind_of(code)
    return None if kind is None else KIND_TO_ROLE[kind]


def generate_code(kind: str) -> str:
    """生成一个该类型的码（``secrets`` 保证不可预测）。

    唯一性由 ``invites.code`` 主键兜底：26^5 的空间里碰撞概率极低，但**不是零**，
    因此创建路径仍然捕获 ``IntegrityError`` 并重试（见 ``MetaDB.create_invite``）。
    """
    if kind not in KINDS:
        raise ValueError(f"未知邀请码类型：{kind}（合法值：{'/'.join(KINDS)}）")
    body = "".join(secrets.choice(ALPHABET) for _ in range(RANDOM_LENGTH))
    return f"{kind}{body}"


def redeem(db: MetaDB, code: str, *, user_id: str, now: int | None = None) -> str:
    """核销一个码并把使用记录登记给 ``user_id``；返回该码的**类型**。

    并发安全由 ``MetaDB.consume_invite`` 保证：核销是一条带 ``WHERE used_count < max_uses``
    的 UPDATE，**以影响行数判成败**。两个请求同时用 ``max_uses=1`` 的码时只有一个 UPDATE
    会改到行，另一个拿到 0 行 → :class:`InviteRejected`。这条纪律不能换成
    "先 SELECT 判再用"——那正是并发超发的经典写法。
    """
    normalized = normalize_code(code)
    ok, kind, reason = db.consume_invite(normalized, user_id=user_id, now=now)
    if not ok:
        raise InviteRejected(normalized, reason or REJECT_UNKNOWN)
    return kind or ""
