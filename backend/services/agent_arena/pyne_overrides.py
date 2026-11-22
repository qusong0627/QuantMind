"""Pyne 策略装饰器覆盖（纯函数）：把外部语料的成本/仓位口径统一到平台模板。

设计目的
--------
`pynecore-strategies` 的 449 条编译产物保留了各自在 TradingView 上的
`@script.strategy(...)` 设置：commission 0–0.2%、171 条 fixed qty、initial_capital
100–1,000,000、slippage 0–10。直接横比等于「449 种下注口径」——本模块只做一件事：
在**运行时**复制一份候选脚本，重写 decorator 的七个 kwarg，原文件一个字节不动。

为什么不改导入文件
------------------
同一策略要在 A 股（0.05%/边）、港股（0.15%/边，含印花税）、美股（0.05%/边）
分别跑；导入时固定成本会让跨市场对比失真。复制一份 ~20KB 的脚本成本可忽略。

为什么不走 .toml
----------------
`market_lab._run_script` 会主动删掉脚本旁的 `<script>.toml`（防止历史 input 覆盖）；
且多进程批量回测共用同一脚本时 TOML 会互相踩。decorator 是唯一稳定的覆盖点。

本模块不 import pynecore / duckdb，纯字符串处理，容器内单测可直接跑。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# 平台统一口径（与 lab_strategies/*.py 的 decorator 一致）
_BASE_SETTINGS: dict[str, str] = {
    "initial_capital": "100000",
    "default_qty_type": "strategy.percent_of_equity",
    "default_qty_value": "95",
    "commission_type": "strategy.commission.percent",
    "commission_value": "0.05",
    "slippage": "1",
    "margin_long": "0",
    "margin_short": "0",
}

# 港股印花税 0.1% + 交易征费/联交所费，取 0.15%/边（保守）；A股/美股沿用平台 0.05%
_MARKET_COMMISSION: dict[str, str] = {
    "cn": "0.05",
    "hk": "0.15",
    "us": "0.05",
    "futures": "0.05",
    "crypto": "0.05",
}

_MARKET_CURRENCY: dict[str, str] = {
    "cn": "currency.CNY",
    "hk": "currency.HKD",
    "us": "currency.USD",
    "futures": "currency.CNY",
    "crypto": "currency.USDT",
}

# 只替换、不注入：`currency` 在源码里出现才说明 `from pynecore.lib import currency`
# 已存在；凭空注入会 NameError。没有 currency 的脚本按 syminfo 本币结算，无需覆盖。
_REPLACE_ONLY: frozenset[str] = frozenset({"currency"})


def preset_for_market(market: str) -> dict[str, str]:
    """市场 → decorator 覆盖表（返回新 dict，调用方可安全修改）。"""
    key = str(market or "cn").strip().lower()
    out = dict(_BASE_SETTINGS)
    out["commission_value"] = _MARKET_COMMISSION.get(key, "0.05")
    out["currency"] = _MARKET_CURRENCY.get(key, "currency.NONE")
    return out


def _scan_decorator(source: str) -> tuple[int, int] | None:
    """定位第一个 ``@script.strategy(...)`` 的参数区间 ``[start, end)``。

    ``start`` 指向左括号之后，``end`` 指向匹配的右括号之前。跳过字符串里的括号；
    找不到 decorator 返回 None（调用方原样返回）。
    """
    marker = "@script.strategy("
    pos = source.find(marker)
    if pos < 0:
        return None
    start = pos + len(marker)
    depth = 1
    quote: str | None = None
    escaped = False
    i = start
    while i < len(source):
        ch = source[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return start, i
        i += 1
    return None


def _split_args(text: str) -> list[str]:
    """按顶层逗号拆分 decorator 参数，字符串/嵌套括号内的逗号不拆。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    for ch in text:
        if quote is not None:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _format_value(key: str, value: str, existing: str) -> str:
    """按原值写法格式化覆盖值（目前只有 currency 需要区分）。"""
    if key != "currency":
        return value
    code = value.rsplit(".", 1)[-1].strip("'\"")
    # 原 decorator 写的是 currency.USD → 保持表达式；写的是 'USD' 字符串 →
    # 保持字符串。否则替换成货币表达式会 NameError（源码未必 import currency）。
    if existing.strip().startswith("currency."):
        return f"currency.{code}"
    return f"'{code}'"


def apply_strategy_settings(source: str, settings: Mapping[str, str]) -> str:
    """返回替换 decorator kwarg 后的新源码；没有 decorator 时原样返回。

    只动 ``key=value`` 形式的顶层参数；位置参数（标题、shorttitle）与未知 kwarg
    保持原样。缺失的 key 追加在参数列表末尾。
    """
    bounds = _scan_decorator(source)
    if bounds is None:
        return source
    start, end = bounds
    args = _split_args(source[start:end])
    seen: set[str] = set()
    for i, arg in enumerate(args):
        if "=" not in arg:
            continue
        key, existing = arg.split("=", 1)
        key = key.strip()
        if key in settings:
            args[i] = f"{key}={_format_value(key, settings[key], existing)}"
            seen.add(key)
    for key, value in settings.items():
        if key not in seen and key not in _REPLACE_ONLY:
            args.append(f"{key}={value}")
    return source[:start] + ", ".join(args) + source[end:]


def materialize_script(
    script: Path,
    settings: Mapping[str, str] | None,
    out_dir: Path,
) -> Path:
    """把 ``script`` 复制到 ``out_dir`` 并应用 settings；返回新路径。

    settings 为 None/空时只复制不修改（调用方用于统一临时路径语义）。
    """
    src = Path(script)
    text = src.read_text(encoding="utf-8")
    if settings:
        text = apply_strategy_settings(text, settings)
    dst = Path(out_dir) / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    return dst
