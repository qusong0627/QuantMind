"""错误自定位格式（T-P0-08）：统一的"错误信息即半个修复"三段式。

格式：``[CONTRACT:XX|RULE:XX] 描述 (ref=<关键ID>) → file:func``

目的（见 docs/可维护性与静默失效防治_设计方案.md §七）：关键路径的
警告/错误自带定位线索——契约/规则 ID、关键引用 ID（run_id/order_id）、
代码位置。排障时 ``grep "→"`` 直接指路，不再全库搜索，压缩 AI/人
定位缺陷的成本。

使用范围（P0 三处）：信号闸门（script_runner）/ 模拟执行与落账
（simulation engine / execution_engine）。新代码在关键路径应随手使用。
"""

from __future__ import annotations


def locate(tag: str, message: str, *, ref: str = "", where: str = "") -> str:
    """拼装自定位错误信息。

    tag   契约/规则标识，如 ``RULE:SIGNAL-GATE``、``CONTRACT:LEDGER``；
    ref   关键引用 ID（run_id / order_id / user_id），可省略；
    where ``file:func`` 定位（如 ``script_runner.py:_resolve_signal_sides``）。
    """
    text = f"[{str(tag).strip()}] {str(message).strip()}"
    ref_text = str(ref).strip()
    if ref_text:
        text = f"{text} (ref={ref_text})"
    where_text = str(where).strip()
    if where_text:
        text = f"{text} → {where_text}"
    return text
