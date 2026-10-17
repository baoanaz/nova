"""会话级去重：同一用户短时间内重复搜索时，已经返回过的源码块只留一行占位。

Agent 常常把同一个问题换几种说法连搜几次，每次都会把同一批函数完整返回一遍，
白白消耗上下文。这里记住"最近返回过哪些 (项目, 路径, 行区间)"，再次出现时正文换成一行说明，
编号、路径、行号和理由仍保留，Agent 知道它在哪、需要时可以自己 read。

不做持久化、不跨用户；窗口外（默认 10 分钟）或超出容量的记录自动失效，
所以 Agent 上下文被压缩后重新搜索也能拿回完整内容。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import replace

from nova_core.types import ContextPack, EvidenceItem

__all__ = ["RecentEvidence", "STUB_NOTE"]

STUB_NOTE = "（此片段刚在本会话的上一次结果中返回过，内容省略；需要原文请直接 read 该行区间）"
_DEFAULT_TTL_S = 600.0
_DEFAULT_CAPACITY = 2000

_Key = tuple[str, str, str, int, int]


class RecentEvidence:
    """线程安全的最近返回记录（LRU + TTL）。"""

    def __init__(
        self,
        *,
        ttl_s: float = _DEFAULT_TTL_S,
        capacity: int = _DEFAULT_CAPACITY,
        clock=time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._clock = clock
        self._seen: OrderedDict[_Key, float] = OrderedDict()
        self._lock = threading.Lock()

    def dedupe(self, owner: str, project_id: str, pack: ContextPack) -> int:
        """把 ``pack`` 里近期返回过的代码块正文换成占位；返回被占位的条数。

        只处理有明确行区间的 code/test 证据（文档块按标题路径定位，换措辞后常是同一段，
        不在这里处理）。本次返回的块同时登记为"已返回"。
        """
        now = self._clock()
        replaced = 0
        with self._lock:
            self._expire(now)
            items: list[EvidenceItem] = []
            for item in pack.evidence:
                if item.lines is None or item.type == "spec":
                    items.append(item)
                    continue
                key: _Key = (owner, project_id, item.path, item.lines[0], item.lines[1])
                if key in self._seen:
                    items.append(replace(item, content=STUB_NOTE, elided_lines=0))
                    replaced += 1
                else:
                    items.append(item)
                self._seen[key] = now
                self._seen.move_to_end(key)
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
        pack.evidence = items
        return replaced

    def _expire(self, now: float) -> None:
        while self._seen:
            key, stamp = next(iter(self._seen.items()))
            if now - stamp <= self._ttl_s:
                break
            del self._seen[key]
