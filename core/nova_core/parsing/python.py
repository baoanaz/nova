"""Python 抽取器（TASK-002）。

规则来源：docs/design/Module/01 §2.2（Python 表）+ 任务卡 TASK-002"Python 抽取规则"。

口径记录（评审与 TASK-006/007 消费关键，完整版见任务卡执行记录）：

- 符号：``function_definition``（含 async）/ ``class_definition`` / 模块级**带注解** assignment
  （kind=variable；无注解不建符号，避免把脚本级临时变量灌进符号表）；
- 嵌套函数成符号（建议口径）：fqn 用外层链，``Outer.inner`` / ``Class.method.inner``，
  kind=function；
- 类方法 kind=method；类属性 assignment 不成符号（V1 口径）；
- fqn 为文件内相对名（不含模块前缀；chunk_id 已含 path，D-04）；
- 装饰器区间计入符号 ``start_line``（切 chunk 时装饰器跟随函数）；装饰器表达式本身不产边；
- ``is_exported``：模块级符号按 ``__all__``（若定义）否则"不以 _ 开头"；方法/嵌套符号一律 False；
- 边：``calls``（裸名给短名、属性链给整条链，交 TASK-006 二阶段解析）、``imports``、``extends``；
- 文件级（模块级代码）边的 source_fqn = 文件 path（模块无符号节点）；
- unresolved：``getattr`` 家族、``importlib`` / ``__import__``、字符串/下标/调用结果等动态 callee
  如实标注，不猜。
"""

from __future__ import annotations

import tree_sitter as ts

from nova_core.parsing.base import (
    Extraction,
    FileContext,
    TreeSitterParser,
    end_line_of,
    line_of,
)
from nova_core.types import SymbolDef

_DEFINITION_TYPES = frozenset({"function_definition", "class_definition"})
_DYNAMIC_CALL_TARGETS = frozenset({"getattr", "setattr", "hasattr", "delattr"})
_DYNAMIC_IMPORT_CALLEES = frozenset(
    {"__import__", "importlib.import_module", "importlib.__import__"}
)
_IMPORT_NAME_TYPES = frozenset({"dotted_name", "aliased_import", "wildcard_import"})


class PythonParser(TreeSitterParser):
    """Python（tree-sitter）抽取器。"""

    language = "python"
    grammar_module = "tree_sitter_python"

    def extract(self, root: ts.Node, ctx: FileContext, out: Extraction) -> None:
        exported = _module_exports(root, ctx)
        self._visit_block(
            root, ctx, out, scope=(), owner=ctx.path, in_class=False, exported=exported
        )

    # -- 语句块（module / class body / function body 共用） -------------------

    def _visit_block(
        self,
        block: ts.Node,
        ctx: FileContext,
        out: Extraction,
        *,
        scope: tuple[str, ...],
        owner: str,
        in_class: bool,
        exported: frozenset[str] | None,
    ) -> None:
        module_level = not scope
        for child in block.named_children:
            decorators, definition = _unwrap_decorated(child)
            if definition is not None:
                if definition.type == "function_definition":
                    self._function(definition, ctx, out, scope, in_class, decorators, exported)
                elif definition.type == "class_definition":
                    self._class(definition, ctx, out, scope, decorators, exported)
                else:
                    # 装饰器挂在非定义语句上（语法容许的极端形态），按普通语句扫
                    self._scan(definition, ctx, out, owner)
                continue
            if child.type == "function_definition":
                self._function(child, ctx, out, scope, in_class, (), exported)
                continue
            if child.type == "class_definition":
                self._class(child, ctx, out, scope, (), exported)
                continue
            if module_level and child.type == "expression_statement":
                self._module_variable(child, ctx, out, exported)
            self._scan(child, ctx, out, owner)

    def _function(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        in_class: bool,
        decorators: tuple[ts.Node, ...],
        exported: frozenset[str] | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:  # 防御：lambda 不会进这里，语法异常已在上层拦掉
            return
        name = ctx.text(name_node)
        fqn = ".".join((*scope, name))
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind="method" if in_class else "function",
                start_line=line_of(decorators[0]) if decorators else line_of(node),
                end_line=end_line_of(node),
                is_exported=_is_exported(name, scope, exported),
            )
        )
        for field in ("parameters", "return_type"):
            part = node.child_by_field_name(field)
            if part is not None:  # 默认值/注解里的调用归本函数
                self._scan(part, ctx, out, fqn)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_block(
                body, ctx, out, scope=(*scope, name), owner=fqn, in_class=False, exported=exported
            )

    def _class(
        self,
        node: ts.Node,
        ctx: FileContext,
        out: Extraction,
        scope: tuple[str, ...],
        decorators: tuple[ts.Node, ...],
        exported: frozenset[str] | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = ctx.text(name_node)
        fqn = ".".join((*scope, name))
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=fqn,
                kind="class",
                start_line=line_of(decorators[0]) if decorators else line_of(node),
                end_line=end_line_of(node),
                is_exported=_is_exported(name, scope, exported),
            )
        )
        supers = node.child_by_field_name("superclasses")
        if supers is not None:
            for base in supers.named_children:
                if base.type == "keyword_argument":
                    continue  # metaclass=... / **kwargs 不是基类
                target = ctx.text(_base_target(base))
                if target:
                    out.add_edge(fqn, target, "extends", line_of(base))
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_block(
                body, ctx, out, scope=(*scope, name), owner=fqn, in_class=True, exported=exported
            )

    def _module_variable(
        self, statement: ts.Node, ctx: FileContext, out: Extraction, exported: frozenset[str] | None
    ) -> None:
        """模块级带注解 assignment → symbol（kind=variable）。"""
        assignment = next(
            (child for child in statement.named_children if child.type == "assignment"), None
        )
        if assignment is None or assignment.child_by_field_name("type") is None:
            return
        left = assignment.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return  # 解包/下标/属性赋值：不成符号（无稳定 fqn）
        name = ctx.text(left)
        out.add_symbol(
            SymbolDef(
                name=name,
                fqn=name,
                kind="variable",
                start_line=line_of(assignment),
                end_line=end_line_of(assignment),
                is_exported=_is_exported(name, (), exported),
            )
        )

    # -- 表达式扫描（调用 / import） ------------------------------------------

    def _scan(self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str) -> None:
        """递归扫描非定义语句：收集 calls / imports，遇嵌套定义不下钻（由 _visit_block 负责）。"""
        node_type = node.type
        if node_type in _DEFINITION_TYPES or node_type == "decorated_definition":
            return
        if node_type == "call":
            self._call(node, ctx, out, owner)
        elif node_type == "import_statement":
            self._import_statement(node, ctx, out, owner)
        elif node_type == "import_from_statement":
            self._import_from(node, ctx, out, owner)
        for child in node.named_children:
            self._scan(child, ctx, out, owner)

    def _call(self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str) -> None:
        function = node.child_by_field_name("function")
        if function is None:  # 防御
            out.add_unresolved(owner, ctx.text(node), "call", line_of(node))
            return
        if function.type == "identifier":
            name = ctx.text(function)
            if name in _DYNAMIC_CALL_TARGETS:
                self._dynamic_call(node, ctx, out, owner)
                return
            out.add_edge(owner, name, "calls", line_of(node))
            return
        if function.type == "attribute":
            name = ctx.text(function)
            if name in _DYNAMIC_IMPORT_CALLEES:
                self._dynamic_import(node, ctx, out, owner)
                return
            out.add_edge(owner, name, "calls", line_of(node))
            return
        # 下标 / 调用结果 / lambda / 条件表达式……静态定型不了 → 如实进 unresolved
        out.add_unresolved(owner, ctx.text(function), "call", line_of(node))

    def _dynamic_call(self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str) -> None:
        """getattr(obj, "name") 家族：名字在第二参数时给出字面量，否则整表达式如实标注。"""
        literal = _string_argument(node, ctx, 1)
        out.add_unresolved(owner, literal or ctx.text(node), "call", line_of(node))

    def _dynamic_import(self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str) -> None:
        """importlib.import_module("x") / __import__("x")：模块名动态 → unresolved(kind=import)。"""
        literal = _string_argument(node, ctx, 0)
        out.add_unresolved(owner, literal or ctx.text(node), "import", line_of(node))

    def _import_statement(
        self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str
    ) -> None:
        for child in node.named_children:
            if child.type == "dotted_name":
                out.add_edge(owner, ctx.text(child), "imports", line_of(node))
            elif child.type == "aliased_import":
                real = child.child_by_field_name("name")
                if real is not None:
                    out.add_edge(owner, ctx.text(real), "imports", line_of(node))

    def _import_from(self, node: ts.Node, ctx: FileContext, out: Extraction, owner: str) -> None:
        module_node = node.child_by_field_name("module_name")
        module = ctx.text(module_node) if module_node is not None else ""
        for child in node.named_children:
            if module_node is not None and child.id == module_node.id:
                continue
            if child.type not in _IMPORT_NAME_TYPES:
                continue
            if child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
            else:
                name_node = child
            if name_node is None:
                continue
            name = "*" if name_node.type == "wildcard_import" else ctx.text(name_node)
            out.add_edge(owner, _join_module(module, name), "imports", line_of(node))


# -- 模块级工具 ---------------------------------------------------------------


def _unwrap_decorated(node: ts.Node) -> tuple[tuple[ts.Node, ...], ts.Node | None]:
    """拆 decorated_definition → (装饰器列表, 被装饰的定义)；非装饰定义返回 ((), None)。"""
    if node.type != "decorated_definition":
        return (), None
    definition = node.child_by_field_name("definition")
    if definition is None:
        return (), None
    decorators = tuple(child for child in node.named_children if child.type == "decorator")
    return decorators, definition


def _base_target(base: ts.Node) -> ts.Node:
    """基类表达式取目标节点：``Generic[T]`` 取 ``Generic``（下钻 subscript.value）。"""
    if base.type == "subscript":
        value = base.child_by_field_name("value")
        if value is not None:
            return value
    return base


def _string_argument(node: ts.Node, ctx: FileContext, index: int) -> str | None:
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return None
    named = [child for child in arguments.named_children if child.type != "comment"]
    if len(named) <= index:
        return None
    return _string_literal(named[index], ctx)


def _string_literal(node: ts.Node, ctx: FileContext) -> str | None:
    """字符串字面量 → 内容；f-string / bytes / 非字符串节点 → None。"""
    if node.type != "string":
        return None
    parts = []
    for child in node.named_children:
        if child.type == "interpolation":
            return None  # f-string：内容动态，不猜
        if child.type == "string_content":
            parts.append(ctx.text(child))
    return "".join(parts)


def _join_module(module: str, name: str) -> str:
    """``from <module> import <name>`` → 目标名；相对 import 保留前导点。"""
    if not module:
        return name
    if module.endswith("."):
        return f"{module}{name}"
    return f"{module}.{name}"


def _module_exports(root: ts.Node, ctx: FileContext) -> frozenset[str] | None:
    """模块级 ``__all__``（列表/元组/集合的字符串字面量）；未定义 → None。"""
    for statement in root.named_children:
        if statement.type != "expression_statement":
            continue
        assignment = next(
            (child for child in statement.named_children if child.type == "assignment"), None
        )
        if assignment is None:
            continue
        left = assignment.child_by_field_name("left")
        if left is None or ctx.text(left) != "__all__":
            continue
        right = assignment.child_by_field_name("right")
        if right is None or right.type not in {"list", "tuple", "set"}:
            return None
        names = {
            literal
            for element in right.named_children
            if (literal := _string_literal(element, ctx)) is not None
        }
        return frozenset(names)
    return None


def _is_exported(name: str, scope: tuple[str, ...], exported: frozenset[str] | None) -> bool:
    """入口点候选：仅模块级符号；有 __all__ 按 __all__，否则"不以 _ 开头"。"""
    if scope:
        return False
    if exported is not None:
        return name in exported
    return not name.startswith("_")
