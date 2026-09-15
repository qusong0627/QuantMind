"""策略代码统一安全闸门（AST 静态检查，执行前/保存前唯一卡点）。

单一实现：复用 Strategy Lab 的 ``ast_checker``（模块白名单 + 禁用内建 +
禁用 dunder 属性），本模块只做"平台口径"封装：

- ``allowed_modules`` 在白名单基础上放行平台包与 minibt —— 存量模板与用户
  策略合法引用它们（兼容性检查 2026-09-15：87 个模板 + 78 条存量策略 0 误杀）；
- ``require_hooks=False`` —— 沙箱代码存在 on_tick / STRATEGY_CONFIG / minibt
  多种形态，不强制 Lab SDK 的 ``setup`` 钩子。

背景（T-P0-02，2026-09-15 诊断）：``/real-trading/start`` 的沙箱提交路径此前
完全绕过校验，用户可在 trade 服务进程内执行任意代码。现所有
``sandbox_manager.submit_strategy`` 调用方（启动、重启恢复）以及
``user_strategy_loader.save_strategy`` 统一经过本闸门。
"""

from __future__ import annotations

from backend.services.engine.strategy_lab.runner.ast_checker import (
    ALLOWED_MODULES,
    check_source,
)

# 平台包：模板/用户策略的合法引用（minibt DSL、backend.shared 数据适配等）
_PLATFORM_MODULES = frozenset({"backend", "minibt"})

ALLOWED = frozenset(ALLOWED_MODULES) | _PLATFORM_MODULES

# 沙箱代码体积上限（防御性；重启恢复快照曾按 8000 字符截断，这里是硬闸）
MAX_CODE_BYTES = 512 * 1024


def validate_strategy_code(source: str | None) -> None:
    """校验策略代码；不通过抛 ``ValueError``（含可读的 issue 列表）。

    调用点：``trade/sandbox/manager.py::submit_strategy``（唯一必经卡点）、
    ``user_strategy_loader.save_strategy``（保存时早期反馈）。
    """
    if source is None or not str(source).strip():
        raise ValueError("策略代码为空")
    text = str(source)
    if len(text.encode("utf-8", errors="ignore")) > MAX_CODE_BYTES:
        raise ValueError(f"策略代码超过 {MAX_CODE_BYTES // 1024}KB 上限")
    issues = check_source(text, allowed_modules=ALLOWED, require_hooks=False)
    if issues:
        detail = "; ".join(f"[{i.code}] L{i.line}: {i.message}" for i in issues[:5])
        raise ValueError(f"策略代码未通过安全检查: {detail}")
