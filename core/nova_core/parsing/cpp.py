"""C++ 抽取器（TASK-004，全项目最大风险项 D-08）。

定位：**尽力而为 + unresolved 如实标注**——宁可缺边不可错边。
规则来源：docs/design/Module/01 §2.2（C++ 行）+ 任务卡 TASK-004 + D-08。

复用 TASK-003 的 `c.py`（include 解析 / 声明符工具 / 函数指针表 / 可见性过滤），
**不修改** `c.py` / `registry.py` / `base.py` / `__init__.py`。

V1 明确不做（Module/01 §2.2 与 Background/04 §6 对照表）：两阶段名称查找、模板实例化/特化语义、
用户定义转换序列、ADL、concepts、decltype 求值。

口径记录（完整版见任务卡执行记录）：

- 符号：``class_specifier`` / ``struct_specifier`` / ``union_specifier``（有 body）/
  ``namespace_definition`` / ``template_declaration``（**整体 1 symbol，不实例化**）/
  类外定义 ``A::B::foo`` /
  构造·析构·operator 重载 / 类内方法声明（无 body 也要成符号，否则 header-only 类没有方法 chunk）/
  typedef 与 using 别名 / 枚举 / 宏；
- fqn 用 ``::`` 连接作用域（``outer::inner::Widget::run``）；模板特化 ``template <>`` 保留符号但记
  unresolved（不静默丢弃）；显式实例化 ``template class Box<double>;`` **跳过**（不产符号不产边）；
- extends：``base_class_clause`` 全部基类（public/protected/private 一视同仁），目标名去模板实参；
- overrides：``override`` 方法先在同文件按 基类名+方法名 找候选 → ``overrides`` 边（synthesized）；
  找不到 → unresolved(kind='reference', name='Base::method')；跨文件基类属正常情况，如实标注；
- 调用：identifier / field_expression（``->`` 归一到 ``.``）/ template_function（去实参）/
  qualified_identifier；其余（括号表达式、成员指针、强制转换）→ unresolved；
- 诚实标注：函数指针参数 / lambda 变量 / ``std::function`` 变量的调用 → 知道目标未知 →
  unresolved（不硬连边）；
- 声明位置的 ``MACRO(args)`` 调用语句，以及**类型位置命中本文件宏名**的声明（宏生成代码）→
  unresolved(kind='reference')，不展开；
- ERROR/MISSING 恢复树中，不与最小错误区间相交的符号继续抽取；错误区间由 chunker
  自动作为 fallback block 保留，且诊断继续写入 ``parse_errors``；
- include 复用 TASK-003 口径（相对优先 / 唯一同名猜测 / 解不开 unresolved）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import tree_sitter as ts

from nova_core.parsing.base import (
    Extraction,
    FileContext,
    TreeSitterParser,
    end_line_of,
    line_of,
)
from nova_core.parsing.c import (
    FunctionPointerMap,
    declarator_name,
    is_function_pointer_declarator,
    parse_include_directive,
    resolve_include,
)
from nova_core.types import SymbolDef

_CLASS_TYPES = frozenset({"class_specifier", "struct_specifier", "union_specifier"})
_TAG_TYPES = _CLASS_TYPES | {"enum_specifier"}
_PREPROC_CONTAINERS = frozenset(
    {"preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif", "linkage_specification"}
)
_DECLARATION_TYPES = frozenset({"declaration", "field_declaration"})
#: 声明符链上可能出现的中间节点（C++ 比 C 多引用/模板/属性声明符）
_DECLARATOR_TYPES = frozenset(
    {
        "init_declarator",
        "function_declarator",
        "pointer_declarator",
        "reference_declarator",
        "parenthesized_declarator",
        "array_declarator",
        "attributed_declarator",
    }
)
_METHOD_NAME_TYPES = frozenset(
    {"identifier", "field_identifier", "type_identifier", "destructor_name", "operator_name"}
)
_HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".tpp", ".inc", ".def")


class CppParser(TreeSitterParser):
    """C++（tree-sitter）抽取器：尽力而为 + 诚实标注。"""

    language = "cpp"
    grammar_module = "tree_sitter_cpp"
    recover_syntax_errors = True

    #: 宏展开后的少量缺分号是已验证的正常恢复形态，保持 TASK-004 既有语义。
    max_tolerated_missing = 5

    def should_filter_recovered_syntax(self, errors: tuple[str, ...]) -> bool:
        missing_only = all(": missing " in message for message in errors)
        return not (missing_only and len(errors) <= self.max_tolerated_missing)

    def extract(self, root: ts.Node, ctx: FileContext, out: Extraction) -> None:
        pointers = CppPointerMap()
        deferred = Deferred(macro_names=_macro_names(root, ctx))
        self._walk(root, ctx, out, pointers, deferred, scope=(), in_class=False, bases=())
        _collect_cpp_pointers(root, ctx, pointers)
        self._scan_calls(root, ctx, out, pointers, scope=(), owner=ctx.path)
        self._resolve_overrides(out, deferred)
        for from_fqn, spelled, line in deferred.specializations:
            out.add_unresolved(from_fqn, spelled, "reference", line)
        _add_parse_errors(out, deferred)

    # -- pass 1：符号 ---------------------------------------------------------

    def _walk(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        pointers: CppPointerMap,
        deferred: Deferred,
        *,
        scope: tuple[str, ...],
        in_class: bool,
        bases: tuple[str, ...],
    ) -> None:
        for child in node.named_children:
            node_type = child.type
            if node_type == "namespace_definition":
                self._namespace(child, ctx, out, pointers, deferred, scope)
            elif node_type in _TAG_TYPES:
                self._tag(child, ctx, out, pointers, deferred, scope, in_class, bases)
            elif node_type == "template_declaration":
                self._template(child, ctx, out, pointers, deferred, scope, in_class, bases)
            elif node_type == "function_definition":
                self._function(child, ctx, out, scope, in_class)
            elif node_type in _DECLARATION_TYPES:
                self._declaration(child, ctx, out, pointers, deferred, scope, in_class, bases)
            elif node_type == "type_definition":
                self._typedef(child, ctx, out, in_class)
            elif node_type == "alias_declaration":
                self._alias(child, ctx, out, in_class)
            elif node_type == "preproc_include":
                self._include(child, ctx, out)
            elif node_type == "preproc_def":
                self._object_macro(child, ctx, out)
            elif node_type == "preproc_function_def":
                self._function_macro(child, ctx, out)
            elif node_type in _PREPROC_CONTAINERS:
                self._walk(
                    child,
                    ctx,
                    out,
                    pointers,
                    deferred,
                    scope=scope,
                    in_class=in_class,
                    bases=bases,
                )
            elif node_type == "expression_statement":
                self._declaration_position_statement(child, ctx, out)
            elif node_type == "template_instantiation":
                deferred.skipped_instantiations.append(line_of(child))

    def _namespace(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        pointers: CppPointerMap,
        deferred: Deferred,
        scope: tuple[str, ...],
    ) -> None:
        name_node = node.child_by_field_name("name")
        name = ctx.text(name_node) if name_node is not None else ""
        body = node.child_by_field_name("body")
        if name:
            out.add_symbol(
                SymbolDef(
                    name=name,
                    fqn="::".join((*scope, name)),
                    kind="namespace",
                    start_line=line_of(node),
                    end_line=_node_end_line(node, ctx),
                    is_exported=not scope,
                )
            )
        if body is not None:
            self._walk(
                body,
                ctx,
                out,
                pointers,
                deferred,
                scope=(*scope, name) if name else scope,
                in_class=False,
                bases=(),
            )

    def _tag(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        pointers: CppPointerMap,
        deferred: Deferred,
        scope: tuple[str, ...],
        in_class: bool,
        bases: tuple[str, ...],
        *,
        template_start: int | None = None,
    ) -> None:
        body = node.child_by_field_name("body")
        name_node = node.child_by_field_name("name")
        name = _type_name(name_node, ctx)
        if body is None or name is None:
            return  # 前向声明 / 匿名类型：不成符号（TASK-003 同口径）
        fqn = "::".join((*scope, name))
        kind = "enum" if node.type == "enum_specifier" else "struct"
        if node.type == "class_specifier":
            kind = "class"
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind=kind,
                start_line=template_start or line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=not in_class,
            )
        )
        if node.type == "enum_specifier":
            return
        base_names = _base_names(node, ctx)
        for base in base_names:
            out.add_edge(fqn, base, "extends", line_of(node))
        self._walk(
            body,
            ctx,
            out,
            pointers,
            deferred,
            scope=(*scope, name),
            in_class=True,
            bases=base_names,
        )

    def _template(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        pointers: CppPointerMap,
        deferred: Deferred,
        scope: tuple[str, ...],
        in_class: bool,
        bases: tuple[str, ...],
    ) -> None:
        parameters = node.child_by_field_name("parameters")
        inner = next(
            (
                child
                for child in node.named_children
                if child.type not in ("template_parameter_list", "comment")
            ),
            None,
        )
        if inner is None:
            return
        # 模板整体 1 symbol，不实例化：起始行取 template_declaration（含模板参数行）
        if inner.type in _CLASS_TYPES:
            self._tag(
                inner,
                ctx,
                out,
                pointers,
                deferred,
                scope,
                in_class,
                bases,
                template_start=line_of(node),
            )
            _record_specialization(node, parameters, inner, ctx, scope, deferred)
        elif inner.type == "function_definition":
            self._function(inner, ctx, out, scope, in_class, template_start=line_of(node))
            _record_specialization(node, parameters, inner, ctx, scope, deferred)
        elif inner.type in _DECLARATION_TYPES:
            self._declaration(
                inner,
                ctx,
                out,
                pointers,
                deferred,
                scope,
                in_class,
                bases,
                template_start=line_of(node),
            )
            _record_specialization(node, parameters, inner, ctx, scope, deferred)
        elif inner.type == "alias_declaration":
            self._alias(inner, ctx, out, in_class)

    def _function(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        in_class: bool,
        *,
        template_start: int | None = None,
    ) -> None:
        declarator = node.child_by_field_name("declarator")
        name, qualifier = _function_name(declarator, ctx)
        if name is None:
            return
        fqn = "::".join((*scope, qualifier, name)) if qualifier else "::".join((*scope, name))
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind="method" if (in_class or qualifier) else "function",
                start_line=template_start or line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=bool(qualifier is None and not in_class and not _has_static(node, ctx)),
            )
        )

    def _declaration(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        pointers: CppPointerMap,
        deferred: Deferred,
        scope: tuple[str, ...],
        in_class: bool,
        bases: tuple[str, ...],
        *,
        template_start: int | None = None,
    ) -> None:
        # 声明内联定义的 class/struct/enum（`class Nested { ... };`）
        for child in node.named_children:
            if child.type in _TAG_TYPES:
                self._tag(
                    child,
                    ctx,
                    out,
                    pointers,
                    deferred,
                    scope,
                    in_class,
                    bases,
                    template_start=template_start,
                )
        # 已知宏出现在类型位置 = 宏生成声明：不展开，如实进 unresolved（D-08 诚实纪律）
        type_node = node.child_by_field_name("type")
        if (
            type_node is not None
            and type_node.type in ("identifier", "type_identifier")
            and ctx.text(type_node) in deferred.macro_names
        ):
            out.add_unresolved(ctx.path, ctx.text(node), "reference", line_of(node))
        declarator = node.child_by_field_name("declarator")
        if declarator is None or not in_class:
            return  # 文件/命名空间作用域的函数声明（原型）不成符号（TASK-003 同口径）
        if is_function_pointer_declarator(declarator):
            return
        name, _ = _function_name(declarator, ctx)
        if name is None:
            return
        fqn = "::".join((*scope, name))
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind="method",
                start_line=template_start or line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=False,
            )
        )
        if _has_override(declarator, ctx) and bases:
            deferred.overrides.append((fqn, bases, name, line_of(node)))

    def _typedef(self, node: ts.Node, ctx: FileContext, out: Extraction, in_class: bool) -> None:
        name = declarator_name(node.child_by_field_name("declarator"), ctx)
        if name is None:
            return
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=name,
                kind="typedef",
                start_line=line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=not in_class,
            )
        )

    def _alias(self, node: ts.Node, ctx: FileContext, out: Extraction, in_class: bool) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=name,
                kind="typedef",  # using 别名与 typedef 同 kind（设计 kind 表无 alias）
                start_line=line_of(node),
                end_line=_node_end_line(node, ctx),
                is_exported=not in_class,
            )
        )

    def _include(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        if node.child_by_field_name("path") is None:
            return
        directive = parse_include_directive(ctx.text(node), line_of(node))
        if directive is None:
            out.add_unresolved(ctx.path, ctx.text(node), "import", line_of(node))
            return
        target = resolve_include(directive, ctx.path)
        if target is None:
            out.add_unresolved(ctx.path, directive.spelled, "import", line_of(node))
        else:
            out.add_edge(ctx.path, target, "imports", line_of(node))

    def _object_macro(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        out.add_symbol(_macro_symbol(name, node, ctx))
        value = node.child_by_field_name("value")
        if value is not None and "##" in ctx.text(value):
            out.parse_errors.append(
                f"L{line_of(node)}: macro {name} uses ## (token pasting); not expanded"
            )

    def _function_macro(self, node: ts.Node, ctx: FileContext, out: Extraction) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        out.add_symbol(_macro_symbol(name, node, ctx))
        parameters = node.child_by_field_name("parameters")
        value = node.child_by_field_name("value")
        reasons = []
        if parameters is not None and "..." in ctx.text(parameters):
            reasons.append("variadic")
        if value is not None and "##" in ctx.text(value):
            reasons.append("token pasting")
        if reasons:
            out.parse_errors.append(
                f"L{line_of(node)}: macro {name} uses {'/'.join(reasons)}; not expanded"
            )

    def _declaration_position_statement(
        self, node: ts.Node, ctx: FileContext, out: Extraction
    ) -> None:
        """类体/命名空间/文件作用域里的裸调用语句 = 宏生成的声明：不展开，如实进 unresolved。"""
        call = next(
            (child for child in node.named_children if child.type == "call_expression"), None
        )
        if call is None:
            return
        out.add_unresolved(ctx.path, ctx.text(node), "reference", line_of(node))

    # -- pass 2：调用边 -------------------------------------------------------

    def _scan_calls(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        pointers: CppPointerMap,
        *,
        scope: tuple[str, ...],
        owner: str,
    ) -> None:
        for child in node.named_children:
            node_type = child.type
            if node_type == "namespace_definition":
                name_node = child.child_by_field_name("name")
                name = ctx.text(name_node) if name_node is not None else ""
                body = child.child_by_field_name("body")
                if body is not None:
                    self._scan_calls(
                        body,
                        ctx,
                        out,
                        pointers,
                        scope=(*scope, name) if name else scope,
                        owner=owner,
                    )
                continue
            if node_type in _CLASS_TYPES:
                body = child.child_by_field_name("body")
                class_name = _type_name(child.child_by_field_name("name"), ctx)
                if body is not None:
                    self._scan_calls(
                        body,
                        ctx,
                        out,
                        pointers,
                        scope=(*scope, class_name) if class_name else scope,
                        owner=owner,
                    )
                continue
            if node_type == "function_definition":
                name, qualifier = _function_name(child.child_by_field_name("declarator"), ctx)
                if qualifier:
                    fqn = "::".join((*scope, qualifier, name)) if name else owner
                else:
                    fqn = "::".join((*scope, name)) if name else owner
                body = child.child_by_field_name("body")
                if body is not None:
                    self._scan_calls(body, ctx, out, pointers, scope=scope, owner=fqn)
                continue
            if node_type == "call_expression":
                self._call(child, ctx, out, owner, pointers)
            self._scan_calls(child, ctx, out, pointers, scope=scope, owner=owner)

    def _call(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        owner: str,
        pointers: CppPointerMap,
    ) -> None:
        function = node.child_by_field_name("function")
        line = line_of(node)
        if function is None:
            out.add_unresolved(owner, ctx.text(node), "call", line)
            return
        target = _callee_name(function, ctx)
        if target is None:
            # 括号表达式 / 成员指针 / 强制转换：静态定型不了 → 如实标注
            out.add_unresolved(owner, ctx.text(function), "call", line)
            return
        last = target.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        candidates = pointers.get(last)
        if candidates:
            for candidate in candidates:
                out.add_edge(owner, candidate, "calls", line, provenance="synthesized")
            return
        if pointers.is_unknown(last):
            out.add_unresolved(owner, target, "call", line)
            return
        out.add_edge(owner, target, "calls", line)

    # -- overrides 收尾 -------------------------------------------------------

    def _resolve_overrides(self, out: Extraction, deferred: Deferred) -> None:
        by_name: dict[str, list[SymbolDef]] = {}
        for symbol in out.symbols:
            by_name.setdefault(symbol.name, []).append(symbol)
        for method_fqn, bases, method_name, line in deferred.overrides:
            matched = False
            for base in bases:
                for candidate in by_name.get(method_name, ()):
                    if candidate.fqn == method_fqn:
                        continue
                    if _is_base_method(candidate.fqn, base, method_name):
                        out.add_edge(
                            method_fqn, candidate.fqn, "overrides", line, provenance="synthesized"
                        )
                        matched = True
            if not matched:
                for base in bases:
                    out.add_unresolved(method_fqn, f"{base}::{method_name}", "reference", line)


def _is_base_method(candidate_fqn: str, base: str, method: str) -> bool:
    """基类名可能只写短名（``Shape``）而候选 fqn 带命名空间（``app::Shape::area``）。"""
    suffix = f"{base}::{method}"
    return candidate_fqn == suffix or candidate_fqn.endswith(f"::{suffix}")


@dataclass
class Deferred:
    """符号表就绪后才能收口的项。"""

    #: 本文件已声明的宏名（类型位置命中 → 宏生成声明，不展开）
    macro_names: frozenset[str] = frozenset()
    #: (方法 fqn, 基类名元组, 方法短名, 行号)
    overrides: list[tuple[str, tuple[str, ...], str, int]] = field(default_factory=list)
    #: 模板特化 (from_fqn, 书写名, 行号)
    specializations: list[tuple[str, str, int]] = field(default_factory=list)
    #: 显式实例化行号（跳过，不产边）
    skipped_instantiations: list[int] = field(default_factory=list)


class CppPointerMap(FunctionPointerMap):
    """C++ 函数指针候选表：额外记录"知道是间接调用但目标未知"的键。"""

    def __init__(self) -> None:
        super().__init__()
        self._unknown: set[str] = set()

    def mark_unknown(self, key: str) -> None:
        if key:
            self._unknown.add(key)

    def is_unknown(self, key: str) -> bool:
        return key in self._unknown


# -- 复用/内部工具 -------------------------------------------------------------


def _macro_symbol(name: str, node: ts.Node, ctx: FileContext) -> SymbolDef:
    return SymbolDef(
        name=name,
        fqn=name,
        kind="macro",
        start_line=line_of(node),
        end_line=_node_end_line(node, ctx),
        is_exported=ctx.path.lower().endswith(_HEADER_SUFFIXES),
    )


def _node_end_line(node: ts.Node, ctx: FileContext) -> int:
    """符号结束行：预处理指令节点的行尾换行回退一行（TASK-003 同口径，本地实现避免改 c.py）。"""
    end = end_line_of(node)
    if ctx.text(node).endswith("\n"):
        end -= 1
    return max(end, line_of(node))


def _has_static(node: ts.Node, ctx: FileContext) -> bool:
    return any(
        child.type == "storage_class_specifier" and ctx.text(child) == "static"
        for child in node.children
    )


def _has_override(declarator: ts.Node | None, ctx: FileContext) -> bool:
    for node in _declarator_chain(declarator):
        for child in node.children:
            if child.type == "virtual_specifier" and ctx.text(child) == "override":
                return True
    return False


def _type_name(node: ts.Node | None, ctx: FileContext) -> str | None:
    """类型名：``template_type``（``Box<int>``）取 ``Box``，其余取原文。"""
    if node is None:
        return None
    if node.type == "template_type":
        inner = node.child_by_field_name("name")
        return ctx.text(inner) if inner is not None else None
    return ctx.text(node)


def _base_names(node: ts.Node, ctx: FileContext) -> tuple[str, ...]:
    """``class D : public B, protected ns::C`` → ('B', 'ns::C')（去模板实参）。"""
    clause = next((child for child in node.children if child.type == "base_class_clause"), None)
    if clause is None:
        return ()
    names = []
    for base in clause.named_children:
        if base.type in ("access_specifier", "comment"):
            continue
        if base.type == "template_type":
            name_node = base.child_by_field_name("name")
            spelled = ctx.text(name_node) if name_node is not None else ctx.text(base)
        else:
            spelled = ctx.text(base)
        normalized = spelled.replace(" ", "")
        if normalized and normalized not in names:
            names.append(normalized)
    return tuple(names)


def _record_specialization(
    node: ts.Node,
    parameters: ts.Node | None,
    inner: ts.Node,
    ctx: FileContext,
    scope: tuple[str, ...],
    deferred: Deferred,
) -> None:
    """``template <>`` 特化：保留符号（可检索）但如实记 unresolved（不谎称已建模）。"""
    if parameters is None or ctx.text(parameters).strip() != "<>":
        return
    name_node = inner.child_by_field_name("name") or inner.child_by_field_name("declarator")
    spelled = ctx.text(name_node) if name_node is not None else ctx.text(inner)
    fqn = "::".join((*scope, spelled.split("<")[0].split("(")[0].strip()))
    deferred.specializations.append((fqn, spelled, line_of(node)))


def _declarator_chain(node: ts.Node | None) -> list[ts.Node]:
    chain: list[ts.Node] = []
    current = node
    while current is not None and current.type in _DECLARATOR_TYPES:
        chain.append(current)
        inner = current.child_by_field_name("declarator")
        if inner is None:
            inner = next(
                (child for child in current.named_children if child.type in _DECLARATOR_TYPES),
                None,
            )
        current = inner
    return chain


def _function_name(
    declarator: ts.Node | None, ctx: FileContext
) -> tuple[str | None, str | None]:
    """函数声明符 → (短名, 限定前缀)；``A::B::foo`` → ('foo', 'A::B')。"""
    node = declarator
    for candidate in _declarator_chain(declarator):
        if candidate.type == "function_declarator":
            node = candidate
            break
    if node is None:
        return None, None
    inner = node.child_by_field_name("declarator")
    if inner is None:
        return None, None
    if inner.type == "qualified_identifier":
        scope_node = inner.child_by_field_name("scope")
        name_node = inner.child_by_field_name("name")
        return _leaf_name(name_node, ctx), ctx.text(scope_node) if scope_node is not None else None
    return _leaf_name(inner, ctx), None


def _leaf_name(node: ts.Node | None, ctx: FileContext) -> str | None:
    if node is None:
        return None
    if node.type == "template_function":
        return _leaf_name(node.child_by_field_name("name"), ctx)
    if node.type in _METHOD_NAME_TYPES:
        return ctx.text(node)
    return ctx.text(node)


def _callee_name(node: ts.Node, ctx: FileContext) -> str | None:
    """被调用者 → 目标名；``->`` 归一到 ``.``（与 TASK-006 的 name_tail 口径一致）。"""
    if node.type in ("identifier", "field_identifier", "operator_name", "type_identifier"):
        return ctx.text(node)
    if node.type == "template_function":
        return _callee_name(node.child_by_field_name("name") or node, ctx)
    if node.type == "qualified_identifier":
        scope_node = node.child_by_field_name("scope")
        name_node = node.child_by_field_name("name")
        name = _callee_name(name_node, ctx) if name_node is not None else None
        if name is None:
            return None
        return f"{ctx.text(scope_node)}::{name}" if scope_node is not None else name
    if node.type == "field_expression":
        argument = node.child_by_field_name("argument")
        field_node = node.child_by_field_name("field")
        base = _callee_name(argument, ctx) if argument is not None else None
        if base is None or field_node is None:
            return None
        return f"{base}.{ctx.text(field_node)}"
    if node.type == "parenthesized_expression":
        inner = next((child for child in node.named_children), None)
        if inner is not None and inner.type == "field_expression":
            operator = inner.child_by_field_name("operator")
            if operator is None or ctx.text(operator) in (".", "->"):
                return _callee_name(inner, ctx)
        return None
    return None


def _macro_names(node: ts.Node, ctx: FileContext) -> frozenset[str]:
    """本文件声明的宏名（含 ``#define`` 与函数宏）；用于识别宏生成的声明。"""
    names: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in ("preproc_def", "preproc_function_def"):
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                names.add(ctx.text(name_node))
        stack.extend(current.children)
    return frozenset(names)


def _collect_cpp_pointers(node: ts.Node, ctx: FileContext, pointers: CppPointerMap) -> None:
    """函数指针/lambda/std::function 变量：有候选 → 合成；知道目标未知 → 标 unknown（如实标注）。"""
    aliases = _function_pointer_aliases(node, ctx)
    _collect_cpp_pointers_inner(node, ctx, pointers, aliases)


def _function_pointer_aliases(node: ts.Node, ctx: FileContext) -> frozenset[str]:
    """函数指针 / ``std::function`` 别名（using/typedef）→ 其变量声明按间接调用处理。"""
    names: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "type_definition":
            declarator = current.child_by_field_name("declarator")
            if declarator is not None and is_function_pointer_declarator(declarator):
                alias = declarator_name(declarator, ctx)
                if alias:
                    names.add(alias)
        elif current.type == "alias_declaration":
            name_node = current.child_by_field_name("name")
            type_node = current.child_by_field_name("type")
            if name_node is not None:
                spelled = ctx.text(type_node).replace(" ", "") if type_node is not None else ""
                if "(*)" in spelled or "std::function<" in spelled:
                    names.add(ctx.text(name_node))
        stack.extend(current.children)
    return frozenset(names)


def _collect_cpp_pointers_inner(
    node: ts.Node, ctx: FileContext, pointers: CppPointerMap, aliases: frozenset[str]
) -> None:
    for child in node.named_children:
        if child.type in _DECLARATION_TYPES or child.type == "parameter_declaration":
            _collect_declaration_pointers(child, ctx, pointers, aliases)
        elif child.type == "assignment_expression":
            operator = child.child_by_field_name("operator")
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if (
                left is not None
                and right is not None
                and left.type == "identifier"
                and (operator is None or ctx.text(operator) == "=")
            ):
                targets = _function_targets(right, ctx)
                if targets:
                    pointers.add(ctx.text(left), targets)
                elif right.type == "lambda_expression":
                    pointers.mark_unknown(ctx.text(left))
        _collect_cpp_pointers_inner(child, ctx, pointers, aliases)


def _collect_declaration_pointers(
    node: ts.Node, ctx: FileContext, pointers: CppPointerMap, aliases: frozenset[str]
) -> None:
    type_node = node.child_by_field_name("type")
    type_text = ctx.text(type_node).replace(" ", "") if type_node is not None else ""
    indirect_type = type_text in aliases or "std::function<" in type_text
    bare_declarator = node.child_by_field_name("declarator")
    if bare_declarator is not None and not bare_declarator.type == "init_declarator":
        # 无初始化的声明（含函数指针参数）：知道是间接调用但目标未知
        if indirect_type or is_function_pointer_declarator(bare_declarator):
            name = declarator_name(bare_declarator, ctx)
            if name:
                pointers.mark_unknown(name)
    for declarator in node.named_children:
        if declarator.type != "init_declarator":
            continue
        name = declarator_name(declarator, ctx)
        if name is None:
            continue
        value = declarator.child_by_field_name("value")
        is_pointer = indirect_type or is_function_pointer_declarator(declarator)
        if value is None:
            # 无初始化的函数指针声明（常见：函数指针参数）→ 目标未知
            if is_pointer:
                pointers.mark_unknown(name)
            continue
        targets = _function_targets(value, ctx)
        if is_pointer:
            if targets:
                pointers.add(name, targets)
            else:
                pointers.mark_unknown(name)
        elif value.type == "lambda_expression":
            pointers.mark_unknown(name)


def _function_targets(node: ts.Node, ctx: FileContext) -> tuple[str, ...]:
    """初始化表达式 → 候选函数名（``func`` / ``&func`` / ``Class::func`` / 取址）。"""
    if node.type in ("identifier", "qualified_identifier", "field_identifier"):
        return (ctx.text(node).replace(" ", ""),)
    if node.type in ("unary_expression", "pointer_expression"):
        operator = node.child_by_field_name("operator")
        argument = node.child_by_field_name("argument")
        if argument is not None and (operator is None or ctx.text(operator) == "&"):
            return _function_targets(argument, ctx)
        return ()
    if node.type == "parenthesized_expression":
        inner = next((child for child in node.named_children), None)
        return _function_targets(inner, ctx) if inner is not None else ()
    if node.type == "cast_expression":
        value = node.child_by_field_name("value")
        return _function_targets(value, ctx) if value is not None else ()
    return ()


def _add_parse_errors(out: Extraction, deferred: Deferred) -> None:
    for line in deferred.skipped_instantiations:
        out.parse_errors.append(f"L{line}: explicit template instantiation skipped (not modeled)")
