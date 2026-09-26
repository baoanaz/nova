"""LanceDB 的惰性加载点（P1-5）。

为什么单独一个模块：``import lancedb`` 的传递链（``lance_namespace_urllib3_client``、
pylance 等）实测 **2.6–3.2s**，而 CLI 的多数路径（``--help``、无变更增量、``sync`` 状态）
根本不碰向量库。模块顶层 import 会把这笔固定成本强加给**每一次**进程启动；
TASK-114 之前 ``zace_core.cli`` 的导入是 4.2s，其中 2.7s 就是它。

收成一个内聚点、由两个使用方（``vectors.store`` / ``vectors.cache``）共享：
类型注解走 ``TYPE_CHECKING``，运行期只在真正要连接向量库时调用 :func:`load`。
``lru_cache`` 让"已加载"的判断是一次函数调用，而不是每个使用方各写一份全局变量。
"""

from __future__ import annotations

from functools import lru_cache
from types import ModuleType

__all__ = ["load"]


@lru_cache(maxsize=1)
def load() -> ModuleType:
    """返回已导入的 ``lancedb`` 模块；首次调用才真正执行 import。"""
    import lancedb

    return lancedb
