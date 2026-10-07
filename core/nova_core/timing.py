"""索引阶段计时（墙钟 + 主线程 CPU），随 ``IngestReport.timings`` 返回。

为什么内置而不是外挂探针：冷启动优化每一轮都卡在"不知道时间花在哪"——外挂探针要
monkeypatch 内部函数，验收时又必须关掉。这里的计时只读 ``perf_counter`` / ``thread_time``，
开销可忽略，常开；结果不参与报告相等比较（``compare=False``），也不进任何索引内容。

口径：

- ``wall_s``：该阶段的墙钟累计；
- ``cpu_s``：**调用线程**（ingest 主线程）在该阶段的 CPU 累计。``wall - cpu`` 大说明主线程
  在等（子进程预取、向量背压、磁盘），小说明主线程本身是瓶颈；
- 同名阶段多次进入时累加（如逐文件的 ``persist``）。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["StageClock", "StageTiming"]

#: ``(阶段名, 墙钟秒, 主线程 CPU 秒)``。
StageTiming = tuple[str, float, float]


class StageClock:
    """按阶段名累加墙钟与调用线程 CPU（单线程使用）。"""

    def __init__(self) -> None:
        self._order: list[str] = []
        self._wall: dict[str, float] = {}
        self._cpu: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        wall = time.perf_counter()
        cpu = time.thread_time()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - wall, time.thread_time() - cpu)

    def add(self, name: str, wall_s: float, cpu_s: float) -> None:
        if name not in self._wall:
            self._order.append(name)
            self._wall[name] = 0.0
            self._cpu[name] = 0.0
        self._wall[name] += wall_s
        self._cpu[name] += cpu_s

    def snapshot(self) -> tuple[StageTiming, ...]:
        return tuple(
            (name, round(self._wall[name], 4), round(self._cpu[name], 4)) for name in self._order
        )
