"""精确相对 ESM 绑定的只读解析；保留原绑定以避免增量目标陈旧。"""

from __future__ import annotations

import posixpath
from dataclasses import replace

from nova_core.parsing.registry import detect_language
from nova_core.storage import EdgeRow, Store, SymbolRow

LANGUAGES = frozenset({"javascript", "typescript", "tsx"})
_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".mts", ".cjs", ".cts")
_CALLABLE = frozenset({"function", "method", "class"})


class ESMIndex:
    """请求内当前索引，不持久化，不读取文件系统/包配置。"""

    def __init__(self, store: Store):
        self.store = store
        self.files: dict[str, bool] = {}
        self.symbols: dict[str, list[SymbolRow]] = {}

    def rows(self, fqn: str) -> list[SymbolRow]:
        if fqn not in self.symbols:
            self.symbols[fqn] = [
                r for r in self.store.exact_symbols(fqn, limit=None) if r.fqn == fqn
            ]
        return self.symbols[fqn]

    def exists(self, path: str) -> bool:
        if path not in self.files:
            self.files[path] = self.store.file_exists(path)
        return self.files[path]

    def source_file(self, source: str) -> str | None:
        rows = self.rows(source)
        paths = {r.file_path for r in rows if r.file_path}
        if len(paths) == 1:
            return next(iter(paths))
        return source if detect_language(source) in LANGUAGES and "::" not in source else None

    def resolve(
        self, name: str, source_file: str | None, kind: str = "imports",
        *, seen: frozenset[str] = frozenset(),
    ) -> tuple[SymbolRow, ...]:
        if not source_file or detect_language(source_file) not in LANGUAGES:
            return ()
        if not name.startswith("js:") or "#" not in name or len(seen) >= 8:
            return ()
        module, imported = name[3:].rsplit("#", 1)
        if not module.startswith(("./", "../")) or imported == "*":
            return ()
        if any(char in module for char in ("\\", "?", "#", "%", "\x00")):
            return ()
        base = posixpath.normpath(posixpath.join(posixpath.dirname(source_file), module))
        if base.startswith("../") or base == ".." or base.startswith("/"):
            return ()
        suffix = posixpath.splitext(base)[1]
        paths = {base}
        if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            replacement = {".js": ".ts", ".jsx": ".tsx", ".mjs": ".mts", ".cjs": ".cts"}[suffix]
            paths.add(base[:-len(suffix)] + replacement)
        elif not suffix:
            paths.update(base + s for s in _SUFFIXES)
            paths.update(base + "/index" + s for s in _SUFFIXES)
        present = [p for p in paths if self.exists(p)]
        if len(present) != 1:
            return ()  # bundler/tsconfig 的优先级未知，不能任取一个模块
        path = present[0]
        rows = [r for r in self.rows(path + "::" + imported)
                if r.file_path == path and r.name == imported and r.is_exported]
        if len(rows) != 1:
            return ()
        return self._unwrap(rows[0], kind, seen)

    def _unwrap(self, row: SymbolRow, kind: str, seen: frozenset[str]) -> tuple[SymbolRow, ...]:
        if row.fqn in seen or len(seen) >= 8:
            return ()
        if kind == "calls" and any(
            e.source == row.fqn for e in self.store.edges_for(row.fqn, kinds=["binding_boundary"])
        ):
            return ()
        exports = [e for e in self.store.edges_for(row.fqn, kinds=["exports"])
                   if e.source == row.fqn and e.provenance == "parsed"]
        if exports:
            if len(exports) != 1:
                return ()
            edge = exports[0]
            if edge.target.startswith("js:"):
                return self.resolve(edge.target, row.file_path, kind, seen=seen | {row.fqn})
            targets = self.rows(edge.target)
            if len(targets) != 1:
                return ()
            return self._unwrap(targets[0], kind, seen | {row.fqn})
        if kind == "calls" and row.kind not in _CALLABLE:
            return ()
        return (row,)

    def edges(self, edges: list[EdgeRow]) -> list[EdgeRow]:
        resolved = []
        for edge in edges:
            if not edge.target.startswith("js:"):
                resolved.append(edge)
                continue
            rows = self.resolve(edge.target, self.source_file(edge.source), edge.kind)
            if len(rows) == 1:
                resolved.append(replace(edge, target=rows[0].fqn))
        return resolved
