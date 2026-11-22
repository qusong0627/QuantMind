"""`request.security` 数据面：解析请求的周期、聚合 K 线、写 `.ohlcv`。

PyneCore 的 `ScriptRunner(security_data=...)` 支持两种键：`"SYMBOL:TF"` /
`"SYMBOL"` / `"TF"`。本模块只支持**同标的**请求，返回 `{tf: path}` 即可
（解析器第三档按 TF 命中）。

支持范围（2026-11-21 语料实测）：
- 日线族：1D / 1W / 1M / 3M / 12M（由日线聚合）
- 分钟族：1 / 5 / 15 / 30 / 60（A股/加密有分钟落盘；美股/港股没有）
- 240 / 360 / 480 / 720：仅加密（24/7），A股/美股/港股按交易日无法映射，明确拒绝

解析不到的周期（43 条动态变量）返回 `unresolved`，由调用方标记 deferred，
不得静默当成图表周期——那会把「另一条周期」悄悄算成当前周期。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.services.agent_arena import pyne_overrides as po

FREQ_TO_TF = {"1d": "1D", "5m": "5", "1m": "1"}
CALENDAR_TFS = {"1W", "1M", "3M", "12M"}
MINUTE_TFS = {"1", "5", "15", "30", "60", "240", "360", "480", "720"}
_DAILY_FAMILY = {"1D"} | CALENDAR_TFS
_MARKET_TFS: dict[str, set[str]] = {
    # A 股有 5m/1m，可构造 1/5/15/30/60；>60 分钟无法映射交易日
    "cn": _DAILY_FAMILY | {"1", "5", "15", "30", "60"},
    "hk": set(_DAILY_FAMILY),
    "us": set(_DAILY_FAMILY),
    "futures": set(_DAILY_FAMILY),
    "crypto": _DAILY_FAMILY | MINUTE_TFS,
}


def supported_markets(timeframes: set[str] | list[str] | tuple[str, ...]) -> list[str]:
    """给定 security 周期集合，返回可构造数据的市场列表（空集合 = 全部市场）。"""
    tfs = set(timeframes)
    if not tfs:
        return ["cn", "hk", "us", "futures", "crypto"]
    return [m for m, allowed in _MARKET_TFS.items() if tfs <= allowed]


_INPUT_TF_RE = re.compile(r"(\w+)\s*=\s*input\.timeframe\(\s*['\"]([^'\"]+)['\"]")
_INPUT_CALL_RE = re.compile(r"(\w+)\s*=\s*(input(?:\.\w+)?)\(([^)]*)\)")
_SEC_RE = re.compile(r"request\.security\s*\((.*?)\)\s*(?:\[|\n|$)", re.DOTALL)
_LIT_RE = re.compile(r"^['\"]([^'\"]+)['\"]$")
_SAME_SYMBOL_RE = re.compile(r"^(?:syminfo\.(?:tickerid|ticker)|symbol|_symbol|sym)$")
# ticker.heikinashi(syminfo.tickerid) 这类图表类型包装：PyneCore 会剥掉外层
# 按基础标的取数，security 子进程再逐 bar 变换——仍算同标的。
_SAME_SYMBOL_WRAP_RE = re.compile(
    r"^ticker\.\w+\(\s*(?:syminfo\.(?:tickerid|ticker)|symbol|_symbol|sym)\s*\)$"
)
# `_ticker = syminfo.tickerid` 这类别名：request.security(_ticker, ...) 仍算同标的。
_SYMBOL_ALIAS_RE = re.compile(
    r"(\w+)\s*=\s*(?:syminfo\.(?:tickerid|ticker)|symbol|_symbol|sym)\b"
)


def _is_same_symbol(expr: str) -> bool:
    s = expr.strip()
    return bool(_SAME_SYMBOL_RE.match(s) or _SAME_SYMBOL_WRAP_RE.match(s))


def _looks_like_tf(expr: str) -> bool:
    s = expr.strip().strip("'\"")
    return bool(
        s == "timeframe.period"
        or re.fullmatch(r"\d+[DWM]", s)
        or re.fullmatch(r"\d+", s)
        or s in ("D", "W", "M")
    )


def _function_tf_args(source: str) -> dict[str, set[str]]:
    """函数参数当 timeframe 用：``def f(sym, _res)`` + ``f(syminfo.tickerid, 'D')``
    → ``_res`` 绑定 ``{"D"}``（同一函数多次调用会合并多个周期）。

    只收「字面量 / timeframe.period」实参；动态表达式仍留给 unresolved。
    """
    defs = {
        m.group(1): m.group(2)
        for m in re.finditer(r"def\s+(\w+)\s*\(([^)]*)\)", source)
    }
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"\b(\w+)\s*\(([^)]*)\)", source):
        fn = m.group(1)
        if fn not in defs:
            continue
        params = [
            p.split(":")[0].split("=")[0].strip()
            for p in po._split_args(defs[fn])  # noqa: SLF001
        ]
        args = po._split_args(m.group(2))  # noqa: SLF001
        for param, arg in zip(params, args, strict=False):
            val = arg.strip().strip("'\"")
            if param and _looks_like_tf(val):
                out.setdefault(param, set()).add(val)
    return out


def _input_tf_bindings(source: str) -> dict[str, set[str]]:
    """``x = input.timeframe(...)`` / ``x = input(...)`` 的周期默认值。

    兼容 ``input.timeframe('1W')`` 与 ``input.timeframe(title=..., defval='1W')``；
    只收看起来像周期的字面量（D/W/M、nD/nW/nM、数字），其它 input 不误收。
    """
    out: dict[str, set[str]] = {}
    for name, fn, args in _INPUT_CALL_RE.findall(source):
        if fn not in ("input", "input.timeframe", "input.string", "input.int"):
            continue
        m = re.search(r"defval\s*=\s*['\"]([^'\"]+)['\"]", args)
        val = m.group(1) if m else None
        if val is None:
            m2 = re.match(r"\s*['\"]?([^,'\")]+)", args)
            val = m2.group(1).strip() if m2 else None
        if val and _looks_like_tf(val):
            out.setdefault(name, set()).add(val.strip().strip("'\""))
    return out


def _timeframe_bindings(source: str) -> dict[str, set[str]]:
    """变量 → 可能的周期集合（input 默认值 + 函数参数实参）。"""
    out = _input_tf_bindings(source)
    for name, tf in _INPUT_TF_RE.findall(source):
        out.setdefault(name, set()).add(tf)
    for name, values in _function_tf_args(source).items():
        out.setdefault(name, set()).update(values)
    return out


def _function_symbol_aliases(source: str) -> set[str]:
    """``def f(_ticker, ...)`` + ``f(syminfo.tickerid, ...)`` → ``_ticker`` 视为同标的。

    编译产物里常见「函数参数接收 ticker」的写法；不做完整数据流分析，只认
    「定义参数位置 ↔ 调用实参是 syminfo.ticker(id)」的机械对应。
    """
    defs = {
        m.group(1): m.group(2)
        for m in re.finditer(r"def\s+(\w+)\s*\(([^)]*)\)", source)
    }
    aliases: set[str] = set()
    for m in re.finditer(r"\b(\w+)\s*\(([^)]*)\)", source):
        fn = m.group(1)
        if fn not in defs:
            continue
        params = [
            p.split(":")[0].split("=")[0].strip()
            for p in po._split_args(defs[fn])  # noqa: SLF001
        ]
        args = po._split_args(m.group(2))  # noqa: SLF001
        for param, arg in zip(params, args, strict=False):
            if param and _is_same_symbol(arg):
                aliases.add(param)
    return aliases


class SecurityUnavailable(RuntimeError):
    """该策略在当前市场/周期下无法构造 security 数据。"""


def normalize_tf(raw: Any) -> str:
    """TradingView 周期写法 → 本模块规范键。"""
    s = str(raw or "").strip().strip("'\"")
    if s in ("D", "1D"):
        return "1D"
    if s in ("W", "1W"):
        return "1W"
    if s in ("M", "1M"):
        return "1M"
    if re.fullmatch(r"\d+[DWM]", s):
        return s
    if s.isdigit():
        return str(int(s))
    return s


def _call_parts(call: str) -> tuple[str | None, str | None]:
    """拆 request.security 的 symbol / timeframe（位置或关键字）。"""
    sym: str | None = None
    tf: str | None = None
    positional: list[str] = []
    for part in po._split_args(call):  # noqa: SLF001 - 同包纯函数
        if "=" in part and not part.lstrip().startswith(("'", '"')):
            key, value = part.split("=", 1)
            key = key.strip()
            if key == "symbol":
                sym = value.strip()
            elif key == "timeframe":
                tf = value.strip()
            continue
        positional.append(part.strip())
    if sym is None and positional:
        sym = positional[0]
    if tf is None and len(positional) > 1:
        tf = positional[1]
    return sym, tf


def requested_timeframes(
    source: str, chart_tf: str = "1D"
) -> tuple[dict[str, str], list[str]]:
    """返回 (``{原始周期写法: 规范化周期}``, 无法解析的表达式)。

    security_data 的键必须匹配 PyneCore 解析出的**原始** timeframe 字符串：
    源码写 ``'D'`` 就要用 ``"D"`` 命中，不能只给 ``"1D"``；同一规范化周期
    可由多个原始写法共用一份 `.ohlcv`。``timeframe.period`` 归入图表周期
    （同周期仍需一份 `.ohlcv` 供 security 子进程读）。
    """
    defaults = _timeframe_bindings(source)
    aliases = set(_SYMBOL_ALIAS_RE.findall(source)) | _function_symbol_aliases(source)
    wanted: dict[str, str] = {}
    unresolved: list[str] = []
    for call in _SEC_RE.findall(source):
        sym, tf = _call_parts(call)
        if sym is None or tf is None:
            unresolved.append((call or "")[:40])
            continue
        if not (_is_same_symbol(sym) or sym.strip() in aliases):
            unresolved.append(f"symbol={sym.strip()[:30]}")
            continue
        expr = tf.strip()
        if expr == "timeframe.period":
            wanted[chart_tf] = chart_tf
            continue
        m = _LIT_RE.match(expr)
        if m:
            raw = m.group(1)
            wanted[raw] = normalize_tf(raw)
            continue
        if expr in defaults:
            for raw in defaults[expr]:
                if raw == "timeframe.period":
                    wanted[chart_tf] = chart_tf
                else:
                    wanted[raw] = normalize_tf(raw)
            continue
        unresolved.append(expr[:30])
    return wanted, unresolved


def _aggregate(bars: list[dict], key_fn: Callable[[str], Any]) -> list[dict]:
    """按 key 分组合并 OHLCV；首开/最高/最低/末收/量求和，date 取组内最后一根。"""
    out: list[dict] = []
    cur_key: Any = None
    agg: dict | None = None
    for b in bars:
        key = key_fn(str(b.get("date") or ""))
        if key is None:
            continue
        if agg is None or key != cur_key:
            if agg is not None:
                out.append(agg)
            agg = dict(b)
            cur_key = key
        else:
            agg["high"] = max(float(agg["high"]), float(b["high"]))
            agg["low"] = min(float(agg["low"]), float(b["low"]))
            agg["close"] = float(b["close"])
            agg["volume"] = float(agg.get("volume") or 0) + float(b.get("volume") or 0)
            agg["date"] = b["date"]
    if agg is not None:
        out.append(agg)
    return out


def aggregate_calendar(bars: list[dict], tf: str) -> list[dict]:
    def key(date: str):
        try:
            d = datetime.fromisoformat(date[:10]).date()
        except ValueError:
            return None
        if tf == "1W":
            return d.isocalendar()[:2]
        if tf == "1M":
            return (d.year, d.month)
        if tf == "3M":
            return (d.year, (d.month - 1) // 3)
        if tf == "12M":
            return (d.year,)
        return None

    return _aggregate(bars, key)


def aggregate_minutes(bars: list[dict], minutes: int) -> list[dict]:
    step = max(1, int(minutes))

    def key(date: str):
        try:
            dt = datetime.fromisoformat(date)
        except ValueError:
            return None
        return int(dt.timestamp()) // (step * 60)

    return _aggregate(bars, key)


def _write_ohlcv(
    path: Path,
    bars: list[dict],
    period: str,
    market: str,
    *,
    symbol: str,
    name: str,
) -> None:
    from dataclasses import replace

    from pynecore.core.ohlcv import OHLCVWriter

    from backend.services.agent_arena.market_lab import (  # noqa: PLC0415
        _bars_ohlcv,
        _syminfo,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    candles = _bars_ohlcv(bars, market)
    with OHLCVWriter(path, period=period, truncate=True) as writer:
        for candle in candles:
            writer.write(candle)
    # security 子进程必须读同目录的 .toml（SymInfo.load_toml）；缺了直接
    # FileNotFoundError，整个 run 被杀。period 必须与 .ohlcv 周期一致。
    info = replace(_syminfo(symbol, name, market), period=period)
    info.save_toml(path.with_suffix(".toml"))


def build_security_data(
    script: Path,
    *,
    symbol: str,
    market: str,
    chart_freq: str,
    chart_bars: list[dict],
    out_dir: Path,
) -> dict[str, Path]:
    """解析脚本 → 聚合 → 写 `.ohlcv`；返回 `{tf: path}`。

    不可构造时抛 :class:`SecurityUnavailable`（调用方标 unsupported，不静默降级）。
    """
    from backend.services.agent_arena import market_lab as ml  # noqa: PLC0415

    source = Path(script).read_text(encoding="utf-8", errors="replace")
    chart_tf = FREQ_TO_TF.get(chart_freq)
    if chart_tf is None:
        raise SecurityUnavailable(f"未知图表周期: {chart_freq}")
    wanted, unresolved = requested_timeframes(source, chart_tf)
    if unresolved:
        raise SecurityUnavailable(
            "无法解析 request.security 周期: " + ", ".join(unresolved[:5])
        )
    if not wanted:
        return {}

    out_dir = Path(out_dir)
    try:
        name = ml.stock_name(symbol, market)
    except Exception:  # noqa: BLE001 - 名称缺失不影响 security 数据
        name = symbol
    by_norm: dict[str, Path] = {}
    for norm in sorted(set(wanted.values())):
        if norm == chart_tf:
            bars = chart_bars
        elif norm in CALENDAR_TFS:
            daily = (
                chart_bars
                if chart_freq == "1d"
                else ml.load_klines(symbol, adj="unadjusted", market=market, freq="1d")
            )
            bars = aggregate_calendar(daily, norm)
        elif norm in MINUTE_TFS:
            minutes = int(norm)
            if market in ("cn", "hk", "us") and minutes > 60:
                raise SecurityUnavailable(
                    f"交易日市场无法构造 {norm} 分钟 security 数据（需 24/7 连续行情）"
                )
            src_freq = "1m" if minutes <= 1 else "5m"
            if src_freq not in ml.available_freqs(market):
                raise SecurityUnavailable(f"{market} 无 {src_freq} 分钟数据")
            src = ml.load_klines(symbol, adj="unadjusted", market=market, freq=src_freq)
            bars = aggregate_minutes(src, minutes)
        else:
            raise SecurityUnavailable(f"不支持的 security 周期: {norm}")
        if not bars:
            raise SecurityUnavailable(f"{norm} 聚合后无 K 线")
        path = out_dir / f"sec_{norm}.ohlcv"
        _write_ohlcv(path, bars, norm, market, symbol=symbol, name=name)
        by_norm[norm] = path

    # 键用源码里的原始写法（'D' / '1D' / 'W' / '60'），PyneCore 按原串匹配
    return {raw: by_norm[norm] for raw, norm in wanted.items()}
