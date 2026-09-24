"""模拟盘持仓键唯一规范（纯标准库，沙箱子进程也可 import）。

Redis 账户 ``positions`` 的键形（历史遗留，必须统一读写）：

    SYMBOL            # 多头（交易 Lua update_balance 用原始 symbol，裸键）
    SYMBOL::short     # 空头（_update_balance_margin 用 ::side）

台账投影 / EOD / 对账重建一律经 :func:`build_position_key` 生成同一键形，
解析一律经 :func:`split_position_key`。历史上投影侧写的是 ``SYMBOL:short``
（单冒号），与撮合侧 ``SYMBOL::short`` 不一致，导致对账产生假差异并改写
Redis 键、使平仓查不到持仓；此模块收口为单一口径。

兼容读取：``SYMBOL::long`` / ``SYMBOL:long`` / ``SYMBOL:short`` 仍可解析
（存量数据/旧快照）。
"""

from __future__ import annotations

LONG = "long"
SHORT = "short"

_SHORT_SUFFIXES = ("::short", ":short")
_LONG_SUFFIXES = ("::long", ":long")


def split_position_key(pos_key: str) -> tuple[str, str]:
    """解析持仓键 -> (code, side)。兼容 ::short / :short / ::long / 裸键。"""
    text = str(pos_key or "").strip()
    for suffix in _SHORT_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)], SHORT
    for suffix in _LONG_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)], LONG
    return text, LONG


def build_position_key(symbol: str, side: str = LONG) -> str:
    """构造持仓键：多头=裸 symbol，空头=``SYMBOL::short``（唯一口径）。"""
    code = str(symbol or "").strip().upper()
    normalized = str(side or LONG).strip().lower()
    if normalized == SHORT:
        return f"{code}::{SHORT}"
    return code
