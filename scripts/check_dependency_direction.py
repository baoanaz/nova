#!/usr/bin/env python3
"""依赖方向强制检查（D-33 / D-34 / D-35）。

规则：
1. nova-core 的依赖清单禁止出现 HTTP 服务框架 / 用户体系 / 租户依赖（纯库纪律，D-34）；
2. nova-core 源码禁止 import 服务框架与上层包（nova_service / nova_web）；
3. nova-service 源码禁止 import web/client 层。

CI 中运行：uv run python scripts/check_dependency_direction.py
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 服务框架 / 用户体系（core 禁止项）。名称按 PEP 503 规范化为小写连字符形式。
FORBIDDEN_CORE_DEPS = {
    "fastapi",
    "starlette",
    "uvicorn",
    "flask",
    "django",
    "sanic",
    "litestar",
    "aiohttp",
    "passlib",
    "argon2-cffi",
    "bcrypt",
    "itsdangerous",
    "pyjwt",
    "python-jose",
    "authlib",
}
FORBIDDEN_CORE_IMPORTS = {
    "fastapi",
    "starlette",
    "uvicorn",
    "flask",
    "django",
    "sanic",
    "litestar",
    "itsdangerous",
    "jwt",
    "argon2",
    "passlib",
    "bcrypt",
    "nova_service",
    "nova_web",
}
FORBIDDEN_SERVICE_IMPORTS = {"nova_web", "nova_client"}

violations: list[str] = []


def normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def top_level_imports(py_file: Path) -> set[str]:
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except SyntaxError as exc:  # 语法错误交给 ruff/pytest 处理，这里跳过
        print(f"[warn] skip unparsable file: {py_file} ({exc})")
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def check_core_deps() -> None:
    pyproject = ROOT / "core" / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    for dep in data.get("project", {}).get("dependencies", []):
        pkg = normalize(dep.split("==")[0].split(">=")[0].split("<")[0].split(";")[0].strip())
        if pkg in FORBIDDEN_CORE_DEPS:
            violations.append(f"core/pyproject.toml: 依赖 {pkg!r} 违反纯库纪律（D-34）")


def check_imports(pkg_dir: Path, forbidden: set[str], label: str) -> None:
    if not pkg_dir.exists():
        return
    for py_file in sorted(pkg_dir.rglob("*.py")):
        for name in sorted(top_level_imports(py_file) & forbidden):
            violations.append(f"{py_file.relative_to(ROOT)}: import {name!r} 违反方向（{label}）")


def main() -> int:
    check_core_deps()
    check_imports(ROOT / "core" / "nova_core", FORBIDDEN_CORE_IMPORTS, "core 纯库 D-34")
    check_imports(
        ROOT / "service" / "nova_service", FORBIDDEN_SERVICE_IMPORTS, "service 不上探 D-33"
    )
    if violations:
        print("依赖方向检查失败：")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("依赖方向检查通过（core 纯库 / service 不上探）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
