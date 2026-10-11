"""窄 FastAPI/APIRouter 静态注册候选，不等同运行时路由或调用边。"""

from __future__ import annotations

import ast
from collections import Counter

from nova_core.parsing.base import NAME_LIMIT, Extraction, FileContext, line_of

_FACTORIES = frozenset({"FastAPI", "APIRouter"})
_ROUTES = frozenset({"get", "post", "put", "delete", "patch", "head", "options",
                     "websocket", "route", "api_route"})


def _name(node, ctx):
    if node is None:
        return None
    if node.type in {"identifier", "dotted_name"}:
        return ctx.text(node)
    if node.type == "attribute":
        obj = _name(node.child_by_field_name("object"), ctx)
        prop = _name(node.child_by_field_name("attribute"), ctx)
        return f"{obj}.{prop}" if obj and prop else None
    return None


def _imports(node, ctx):
    module = node.child_by_field_name("module_name")
    module_name = ctx.text(module) if module else None
    result = []
    for part in node.named_children:
        if part == module:
            continue
        imported = part.child_by_field_name("name") if part.type == "aliased_import" else part
        alias = part.child_by_field_name("alias") if part.type == "aliased_import" else None
        name = _name(imported, ctx)
        local = _name(alias, ctx) if alias else name
        if name and local:
            factory = None
            if (node.type == "import_from_statement" and module_name == "fastapi"
                    and name in _FACTORIES):
                factory = local
            elif node.type == "import_statement" and name == "fastapi":
                factory = local + ".FastAPI"
            result.append((local, factory))
    return result


def _bound_names(node, ctx):
    if node is None:
        return set()
    name = _name(node, ctx)
    if name:
        return {name.partition(".")[0]}
    names = set()
    for child in node.named_children:
        names.update(_bound_names(child, ctx))
    return names


def extract_fastapi_bindings(root, ctx: FileContext, out: Extraction) -> None:
    """不导入 fastapi，不读取磁盘；provenance 始终 synthesized，Flow 不消费 registers。"""
    if not any(factory for node in root.named_children
               if node.type in {"import_statement", "import_from_statement"}
               for _local, factory in _imports(node, ctx)):
        return
    bound = Counter()
    factories = {}
    constructors = {}
    pending = list(root.named_children)
    while pending:
        node = pending.pop()
        if node.type in {"function_definition", "class_definition"}:
            name = _name(node.child_by_field_name("name"), ctx)
            if name:
                bound[name] += 1
            continue
        if node.type == "decorated_definition":
            definition = node.child_by_field_name("definition")
            if definition:
                pending.append(definition)
            continue
        if node.type in {"import_statement", "import_from_statement"}:
            for local, factory in _imports(node, ctx):
                bound[local] += 1
                if factory and node.parent == root:
                    factories[factory] = local
                    if node.type == "import_statement":
                        factories[local + ".APIRouter"] = local
            continue
        if node.type in {"assignment", "augmented_assignment", "named_expression"}:
            left = node.child_by_field_name("left") or node.child_by_field_name("name")
            bound.update(_bound_names(left, ctx))
            name = _name(left, ctx) if left and left.type == "identifier" else None
            right = node.child_by_field_name("right")
            if (name and right and right.type == "call" and node.parent
                    and node.parent.parent == root):
                target = _name(right.child_by_field_name("function"), ctx)
                constructors[name] = (target, line_of(node))
        if node.type in {"for_statement", "as_pattern"}:
            target = node.child_by_field_name("left") or node.child_by_field_name("alias")
            bound.update(_bound_names(target, ctx))
        if node.type == "delete_statement":
            for child in node.named_children:
                bound.update(_bound_names(child, ctx))
        pending.extend(node.named_children)
    instances = {name: line for name, (target, line) in constructors.items()
                 if target in factories and bound[factories[target]] == 1 and bound[name] == 1}
    for node in root.named_children:
        if node.type != "decorated_definition":
            continue
        definition = node.child_by_field_name("definition")
        if definition is None or definition.type != "function_definition":
            continue
        handler = _name(definition.child_by_field_name("name"), ctx)
        for decorator in node.named_children:
            if decorator.type != "decorator" or not decorator.named_children:
                continue
            call = decorator.named_children[0]
            if call.type != "call":
                continue
            target = _name(call.child_by_field_name("function"), ctx) or ""
            receiver, _, method = target.rpartition(".")
            if (receiver not in instances or method not in _ROUTES or not handler
                    or bound[handler] != 1):
                continue
            arguments = call.child_by_field_name("arguments")
            path_node = None
            if arguments:
                for arg in arguments.named_children:
                    if arg.type == "keyword_argument":
                        if _name(arg.child_by_field_name("name"), ctx) == "path":
                            path_node = arg.child_by_field_name("value")
                    elif path_node is None:
                        path_node = arg
            try:
                path = ast.literal_eval(ctx.text(path_node)) if path_node else None
            except (ValueError, SyntaxError, RecursionError):
                path = None
            if (not isinstance(path, str) or not path.startswith("/")
                    or len(handler) > NAME_LIMIT):
                out.add_unresolved(
                    handler, "dynamic/unsupported route binding", "reference", line_of(decorator),
                )
                continue
            out.add_edge(ctx.path, handler, "registers", instances[receiver], "synthesized")
