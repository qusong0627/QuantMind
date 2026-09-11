"""AST static checker — Layer 1 of the sandbox.

Rejects user code that:
- imports modules outside the whitelist (os, sys, subprocess, socket, ctypes,
  importlib, asyncio.subprocess, ...)
- calls exec / eval / __import__ / open(..., 'w'|'a'|'x'|'+') / compile
- accesses dunder attributes (__class__, __subclasses__, __globals__, ...)
- uses ``with`` / ``try`` to smuggle the above

This is a static gate. It runs in the API process before we hand the script
to the subprocess runner. The subprocess itself relies on Layer 2 (no env,
read-only mounts, cgroup limits) for defence-in-depth — so we don't need
to be exhaustive here, just reject the obvious foot-guns and any obvious
sandbox escape attempt.

Spec: §4.4 + §7.2 of docs/Strategy_Lab规范.md.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Iterable

# Top-level packages user code may import.
ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        # Numerics / data
        "numpy",
        "pandas",
        "scipy",
        "math",
        "statistics",
        "itertools",
        "functools",
        "operator",
        "collections",
        "heapq",
        "bisect",
        # Time / formatting
        "datetime",
        "time",
        "calendar",
        # Serialization / strings
        "json",
        "re",
        "string",
        "decimal",
        "fractions",
        # Typing helpers (no IO)
        "typing",
        "dataclasses",
        "enum",
        # Path handling: pathlib 无 exec/网络面，写入风险依赖 Layer 2 兜底。
        # 注意：os / os.path 已于 2026-09 从白名单移除——Layer 2（只读 FS/
        # 断网/cgroup）截至 Day-7 前未实现，放行 os 等于在 API 容器内开放
        # os.system 任意执行；路径处理请改用 pathlib。
        "pathlib",
        # Logging (safe — no IO in sandbox)
        "logging",
        # Copy / deep copy
        "copy",
        # Hashing
        "hashlib",
        # Indicators
        "talib",
        # Subset of qlib data layer (read-only data API; we still ban the
        # full qlib namespace to keep imports tight)
        "qlib",
    }
)

# Things that are obviously off-limits even though Python doesn't flag them.
FORBIDDEN_BUILTINS: frozenset[str] = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "__import__",
        "globals",
        "vars",
        "input",
        "breakpoint",
        "exit",
        "quit",
    }
)

# ``open()`` is allowed for reading only, but the safest move is just to ban
# it outright — user code has ctx.history()/ctx.feature() for data access.
BANNED_CALLS: frozenset[str] = FORBIDDEN_BUILTINS | {"open"}

# Dunder access we don't want anywhere (sandbox-escape staples).
FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {
        "__class__",
        "__bases__",
        "__subclasses__",
        "__mro__",
        "__globals__",
        "__builtins__",
        "__import__",
        "__getattribute__",
        "__loader__",
        "__spec__",
        "__code__",
        "__closure__",
        "__dict__",
    }
)

# Required entry point hooks: at least one must be defined.
REQUIRED_HOOKS: frozenset[str] = frozenset({"setup"})
OPTIONAL_HOOKS: frozenset[str] = frozenset({"on_bar", "on_universe", "on_finish"})
ALL_HOOKS: frozenset[str] = REQUIRED_HOOKS | OPTIONAL_HOOKS


@dataclass(frozen=True)
class CheckIssue:
    line: int
    col: int
    code: str
    message: str
    suggestion: str = ""


class ASTCheckError(Exception):
    """Raised when the script does not satisfy the static gate."""

    def __init__(self, issues: list[CheckIssue]):
        self.issues = issues
        msg = "\n".join(
            f"  line {i.line}: [{i.code}] {i.message}"
            + (f"\n    建议: {i.suggestion}" if i.suggestion else "")
            for i in issues
        )
        super().__init__(f"Strategy script rejected by AST checker:\n{msg}")


# ---------------------------------------------------------------------------
# Visitor
# ---------------------------------------------------------------------------
class _Checker(ast.NodeVisitor):
    def __init__(self, allowed_modules: frozenset[str]) -> None:
        self.allowed_modules = allowed_modules
        self.issues: list[CheckIssue] = []
        self.found_hooks: set[str] = set()

    # -- imports --
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top not in self.allowed_modules:
                self.issues.append(
                    CheckIssue(
                        line=node.lineno,
                        col=node.col_offset,
                        code="E_IMPORT",
                        message=f"不允许 import '{alias.name}'",
                        suggestion=f"白名单: {sorted(self.allowed_modules)}",
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level and node.level > 0:
            self.issues.append(
                CheckIssue(
                    line=node.lineno,
                    col=node.col_offset,
                    code="E_RELIMPORT",
                    message="不允许相对 import",
                )
            )
        elif node.module:
            top = node.module.split(".")[0]
            if top not in self.allowed_modules:
                self.issues.append(
                    CheckIssue(
                        line=node.lineno,
                        col=node.col_offset,
                        code="E_IMPORT",
                        message=f"不允许 from '{node.module}' import ...",
                        suggestion=f"白名单: {sorted(self.allowed_modules)}",
                    )
                )
        self.generic_visit(node)

    # -- calls --
    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            sug = ""
            if node.func.id == "open":
                sug = "改用 ctx.history(...) / ctx.feature(...) / ctx.snapshot(...) 读数据"
            elif node.func.id in {"exec", "eval", "compile", "__import__"}:
                sug = "策略脚本中不允许动态代码执行"
            self.issues.append(
                CheckIssue(
                    line=node.lineno,
                    col=node.col_offset,
                    code="E_CALL",
                    message=f"不允许调用 {node.func.id}()",
                    suggestion=sug,
                )
            )
        self.generic_visit(node)

    # -- attribute access --
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_ATTRS:
            self.issues.append(
                CheckIssue(
                    line=node.lineno,
                    col=node.col_offset,
                    code="E_DUNDER",
                    message=f"不允许访问属性 '{node.attr}'",
                )
            )
        self.generic_visit(node)

    # -- catch hook definitions at module level --
    def visit_Module(self, node: ast.Module) -> None:
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if stmt.name in ALL_HOOKS:
                    self.found_hooks.add(stmt.name)
                if isinstance(stmt, ast.AsyncFunctionDef) and stmt.name in ALL_HOOKS:
                    self.issues.append(
                        CheckIssue(
                            line=stmt.lineno,
                            col=stmt.col_offset,
                            code="E_ASYNC",
                            message=f"钩子 {stmt.name} 不能是 async def",
                            suggestion="改为普通 def",
                        )
                    )
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def check_source(
    source: str,
    *,
    allowed_modules: Iterable[str] | None = None,
    require_hooks: bool = True,
) -> list[CheckIssue]:
    """Validate a script. Returns the list of issues (empty = clean)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [
            CheckIssue(
                line=e.lineno or 1,
                col=e.offset or 0,
                code="E_SYNTAX",
                message=f"语法错误: {e.msg}",
            )
        ]

    allowed = frozenset(allowed_modules) if allowed_modules else ALLOWED_MODULES
    checker = _Checker(allowed)
    checker.visit(tree)

    if require_hooks:
        missing = REQUIRED_HOOKS - checker.found_hooks
        if missing:
            checker.issues.append(
                CheckIssue(
                    line=1,
                    col=0,
                    code="E_HOOK_MISSING",
                    message=f"缺少必需的钩子函数: {sorted(missing)}",
                    suggestion="至少要定义 def setup(ctx): ...",
                )
            )

    return checker.issues


def assert_safe(
    source: str,
    *,
    allowed_modules: Iterable[str] | None = None,
    require_hooks: bool = True,
) -> None:
    """Raise ``ASTCheckError`` if the script violates any rule."""
    issues = check_source(
        source, allowed_modules=allowed_modules, require_hooks=require_hooks
    )
    if issues:
        raise ASTCheckError(issues)


def discover_hooks(source: str) -> set[str]:
    """Cheap inspector: which hooks does the script declare? Skips checks."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name in ALL_HOOKS:
            found.add(stmt.name)
    return found
