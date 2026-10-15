"""进程内限流与并发闸门（单进程部署，无外部依赖）。

服务此前没有任何速率保护：登录可被无限次试密码，``ask`` 在用服务端默认 LLM key 时可被无限调用
（成本直接落在部署者身上），索引并发只受线程数限制（2C2G 的机器上并发 embedding 会把进程拖垮）。
这里给出最小实现：滑动窗口计数器 + 有界信号量。窗口状态只在内存里，重启即清零。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from nova_service.errors import ApiError

__all__ = [
    "LOGIN_MAX_FAILURES",
    "LOGIN_WINDOW_S",
    "SlidingWindowLimiter",
    "enforce_ask_rate",
    "too_many",
]

#: 键数量上限：超过时顺带清理已过期的键，避免被构造的大量不同账户名撑爆内存。
_MAX_KEYS = 10_000


class SlidingWindowLimiter:
    """每个 key 在 ``window_s`` 秒内最多 ``limit`` 次。``limit <= 0`` = 不限。"""

    def __init__(
        self, limit: int, window_s: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = int(limit)
        self._window = float(window_s)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def retry_after(self, key: str) -> float | None:
        """当前是否已超限；超限返回需要等待的秒数，否则 ``None``（**不记录**本次）。"""
        if self._limit <= 0:
            return None
        with self._lock:
            hits = self._fresh(key)
            if len(hits) < self._limit:
                return None
            return max(0.0, hits[0] + self._window - self._clock())

    def hit(self, key: str) -> None:
        """记录一次。"""
        if self._limit <= 0:
            return
        with self._lock:
            if len(self._hits) >= _MAX_KEYS:
                self._purge()
            self._fresh(key)
            self._hits.setdefault(key, deque()).append(self._clock())

    def acquire(self, key: str) -> float | None:
        """原子地「检查并记录」：放行返回 ``None``，超限返回需等待的秒数（且不记录）。"""
        if self._limit <= 0:
            return None
        with self._lock:
            hits = self._fresh(key)
            if len(hits) >= self._limit:
                return max(0.0, hits[0] + self._window - self._clock())
            if len(self._hits) >= _MAX_KEYS:
                self._purge()
            self._hits.setdefault(key, deque()).append(self._clock())
            return None

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _fresh(self, key: str) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        cutoff = self._clock() - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
            return deque()
        return hits

    def _purge(self) -> None:
        for key in list(self._hits):
            self._fresh(key)


#: 登录失败：同一 (来源 IP, 账户名) 5 分钟内最多 8 次失败。按 IP 分键，攻击者不能靠狂试
#: 别人的账户名把本人锁在门外（uvicorn 默认信任本机反代的 ``X-Forwarded-For``）。
#: 限流器按应用实例持有（``app.state.login_limiter``），不用模块级全局，测试互不污染。
LOGIN_MAX_FAILURES = 8
LOGIN_WINDOW_S = 300.0


def too_many(code: str, message: str, wait_s: float) -> ApiError:
    seconds = max(1, int(wait_s + 0.999))
    return ApiError(code=code, message=f"{message}（请 {seconds} 秒后重试）", status=429)


def enforce_ask_rate(limiter: SlidingWindowLimiter | None, user_id: str | None) -> None:
    """``ask`` 的按用户限流：超限抛 429 ``rate_limited``。本地模式（无用户）与未启用时放行。"""
    if limiter is None or not user_id:
        return
    wait = limiter.acquire(user_id)
    if wait is not None:
        raise too_many("rate_limited", "问答请求过于频繁", wait)
