"""单次请求内的只读记忆层（检索链的重复读取优化）。

背景：图扩展、补检、两轮装填会对**同一批**符号/边/切片反复调用 ``Store`` 的只读方法
（``expand`` 的多个辅助函数对同一个 fqn 各查一次 ``exact_symbols``；补检后整个包再装填一遍，
逐个 ``chunk_by_id``）。每次都是一次独立的 SQLite 查询，实测第二题图扩展 1.6 s、补检 1.0 s。

做法：一次查询（``Engine._query_with_trace``）内给 ``Store`` 套一层按参数记忆的代理。
安全性依据：

- 查询链全程只读，且在同一次请求内，同参数的读取结果不会变化——**输出与不加缓存逐字一致**；
- 缓存的行类型全是 ``frozen`` dataclass；返回 ``list`` 时每次给调用方一份新列表，
  调用方对列表的原地修改（如 ``edges.extend``）不会污染缓存；
- 只记忆明确列出的纯读方法；写方法与其他属性一律直通，不缓存；
- 代理随请求结束丢弃，不跨请求、不跨写入，因此没有失效问题。
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any

__all__ = ["ReadCachedStore"]

#: 查询链上会被重复调用、且无副作用的 Store 读方法。
_MEMOIZED = frozenset(
    {
        "exact_symbols",
        "file_exists",
        "edges_for",
        "chunk_by_id",
        "chunks_by_ids",
        "chunk_covering",
        "spec_refs_for_spec",
        "spec_refs_for_symbols",
        "reexport_sources",
        "symbols_in_container",
        "unresolved_edges",
        "unresolved_refs",
        "chunks_with_marker",
    }
)


def _freeze(value: Any) -> Hashable:
    """把参数变成可哈希的 key；遇到无法哈希的类型抛 ``TypeError``（调用方据此跳过缓存）。"""
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    hash(value)
    return value


class ReadCachedStore:
    """``Store`` 的只读记忆代理（鸭子类型，接口与 ``Store`` 一致）。"""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._memo: dict[Hashable, Any] = {}
        self.hits = 0
        self.misses = 0

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._store, name)
        if name not in _MEMOIZED or not callable(attribute):
            return attribute
        return self._wrap(name, attribute)

    def _wrap(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        def cached(*args: Any, **kwargs: Any) -> Any:
            try:
                key: Hashable = (name, _freeze(args), _freeze(kwargs))
            except TypeError:
                return method(*args, **kwargs)
            if key in self._memo:
                self.hits += 1
                value = self._memo[key]
            else:
                self.misses += 1
                value = method(*args, **kwargs)
                self._memo[key] = value
            return list(value) if isinstance(value, list) else value

        return cached
