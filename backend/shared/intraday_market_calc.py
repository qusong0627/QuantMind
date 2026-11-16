"""盘中结构/量能的**纯计算**（无 I/O，供任务与单测共用）。

数据形态来自 TDX 免费桥：

* ``get_pricevol`` 全市场：``{suffix: {LastClose, Now, Volume, Zaf}}``（一次 5,582 只）
* ``get_more_info`` 单股：``fLianB``（量比）、``vzangsu``（涨速）、``Zjl``（资金净流入，
  万元）、``Wtb``（委比）、``OpenZAF``（竞价涨幅）、``fHSL``（换手）等
* ``get_market_data`` 日线：``Date/Close/...``（连板计算）
* ``market:snapshot``：``Now/High/Low/Amount/bid1..5/ask1..5``（日内位置/盘口档位）

本模块不 import Redis/桥，涨跌停口径通过 ``limits_fn``/``threshold_fn`` 注入，
默认走 ``simulation.local_market_data`` 的**唯一实现**（延迟 import，避免测试期
拉进 pandas/QuantDB）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Callable

from backend.shared.stock_utils import StockCodeUtil

#: 与 ``market_breadth.price_tolerance`` 同源的价位容差（元，只吸浮点噪声）。
PRICE_EPS_YUAN = 0.004

_DEFAULT_NAMES = ""


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _suffix(code: Any) -> str:
    return StockCodeUtil.to_suffix(str(code or ""))


def _prefix(code: Any) -> str:
    return StockCodeUtil.to_prefix(str(code or ""))


def is_st_name(name: Any) -> bool:
    """名称含 ST → ST 股（沪深主板 ST 5% 板；创业板/科创板 ST 仍 20%）。"""
    return "ST" in str(name or "").upper()


def depth_levels(snap: Mapping[str, Any] | None) -> int:
    """快照实际有几档（买/卖任一档价>0 即算）；0 = 盘口不可用。"""
    if not isinstance(snap, Mapping):
        return 0
    levels = 0
    for i in range(1, 6):
        bid = _f(snap.get(f"bid{i}"))
        ask = _f(snap.get(f"ask{i}"))
        if (bid is not None and bid > 0) or (ask is not None and ask > 0):
            levels = i
    return levels


def _default_limits(
    symbol: str, pre_close: float, *, is_st: bool, trade_date: date
) -> tuple[float, float]:
    from backend.services.simulation.services.local_market_data import compute_limits

    return compute_limits(symbol, pre_close, is_st=is_st, trade_date=trade_date)


def compute_breadth(
    rows: Mapping[str, Mapping[str, Any]],
    names: Mapping[str, str],
    trade_date: date,
    seen_max: Mapping[str, float] | None = None,
    seen_min: Mapping[str, float] | None = None,
    *,
    limits_fn: Callable[..., tuple[float, float]] | None = None,
) -> tuple[dict[str, Any], dict[str, float], dict[str, float]]:
    """全市场快照 → 广度/涨跌停/炸板文档 + 新的日内极值表。

    ``seen_max``/``seen_min`` 是**当日采样极值**（任务每轮持久化）。炸板判定：
    曾触及涨停价、当前未封住 → 炸板；反之跌停打开同理。采样口径必然漏掉
    两次轮询之间的秒级触板，文档里 ``broke_sampled=True`` 明示这一点。

    返回 ``(doc, new_seen_max, new_seen_min)``；输入表不被修改。
    """
    limits = limits_fn or _default_limits
    max_map = {_suffix(k): float(v) for k, v in (seen_max or {}).items() if _f(v)}
    min_map = {_suffix(k): float(v) for k, v in (seen_min or {}).items() if _f(v)}

    up = down = flat = 0
    limit_up: list[dict[str, Any]] = []
    limit_down_codes: list[str] = []
    broke_codes: list[str] = []
    opened_down_codes: list[str] = []
    valid = 0

    for raw_code, row in (rows or {}).items():
        if not isinstance(row, Mapping):
            continue
        code = _suffix(raw_code)
        pre = _f(row.get("LastClose"))
        now = _f(row.get("Now"))
        if not code or pre is None or now is None or pre <= 0 or now <= 0:
            continue
        valid += 1
        name = str(names.get(code) or names.get(raw_code) or "")
        is_st = is_st_name(name)
        up_px, down_px = limits(code, pre, is_st=is_st, trade_date=trade_date)

        cur_max = max(max_map.get(code, now), now)
        cur_min = min(min_map.get(code, now), now)
        max_map[code] = cur_max
        min_map[code] = cur_min

        zaf = _f(row.get("Zaf"))
        if zaf is None:
            zaf = (now - pre) / pre * 100.0

        sealed_up = math.isfinite(up_px) and up_px > 0 and now >= up_px - PRICE_EPS_YUAN
        sealed_down = (
            math.isfinite(down_px)
            and down_px > 0
            and now <= down_px + PRICE_EPS_YUAN
        )
        touched_up = (
            math.isfinite(up_px)
            and up_px > 0
            and cur_max >= up_px - PRICE_EPS_YUAN
        )
        touched_down = (
            math.isfinite(down_px)
            and down_px > 0
            and cur_min <= down_px + PRICE_EPS_YUAN
        )

        if sealed_up:
            limit_up.append({"code": code, "name": name, "pct": zaf})
        elif touched_up:
            broke_codes.append(code)
        if sealed_down:
            limit_down_codes.append(code)
        elif touched_down:
            opened_down_codes.append(code)

        if zaf > 0:
            up += 1
        elif zaf < 0:
            down += 1
        else:
            flat += 1

    broken = len(broke_codes)
    denom = len(limit_up) + broken
    doc = {
        "n": valid,
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": len(limit_up),
        "limit_down": len(limit_down_codes),
        "broke": broken,
        "broke_rate": round(broken / denom * 100.0, 1) if denom else None,
        "opened_down": len(opened_down_codes),
        "sample": "full",
        "broke_sampled": True,
        "limit_up_codes": [row["code"] for row in limit_up],
        "limit_down_codes": sorted(limit_down_codes),
        "broke_codes": sorted(broke_codes),
        "opened_down_codes": sorted(opened_down_codes),
        "top_limit_up": sorted(limit_up, key=lambda r: r["pct"], reverse=True),
    }
    return doc, max_map, min_map


def detect_events(
    prev: Mapping[str, Any] | None,
    cur: Mapping[str, Any],
    now_ts: float,
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """上一轮 → 本轮的状态迁移事件（首轮 ``prev=None`` 不造事件）。"""
    if not isinstance(prev, Mapping) or not prev:
        return []

    def _set(doc: Mapping[str, Any], key: str) -> set[str]:
        raw = doc.get(key)
        return {str(x) for x in raw} if isinstance(raw, Sequence) else set()

    events: list[dict[str, Any]] = []
    for kind, key, label in (
        ("limit_up", "limit_up_codes", "涨停"),
        ("broke", "broke_codes", "炸板"),
        ("limit_down", "limit_down_codes", "跌停"),
        ("opened_down", "opened_down_codes", "跌停打开"),
    ):
        for code in sorted(_set(cur, key) - _set(prev, key)):
            name = str(names.get(code) or "")
            events.append(
                {
                    "ts": now_ts,
                    "code": code,
                    "kind": kind,
                    "text": f"{name}({code}) {label}" if name else f"{code} {label}",
                }
            )
    return events


def compute_metrics(
    codes: Sequence[str],
    names: Mapping[str, str],
    more_info: Mapping[str, Mapping[str, Any]],
    snaps: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """持仓+池 → 量比/涨速/日内位置/资金/委比/竞价/盘口档位（prefix 键）。"""
    out: dict[str, dict[str, Any]] = {}
    for raw in codes or ():
        prefix = _prefix(raw)
        suffix = _suffix(raw)
        if not prefix or prefix in out:
            continue
        mi = more_info.get(suffix) or more_info.get(prefix) or {}
        snap = snaps.get(suffix) or snaps.get(prefix) or {}
        if not isinstance(mi, Mapping):
            mi = {}
        if not isinstance(snap, Mapping):
            snap = {}
        now = _f(snap.get("Now")) or _f(snap.get("price"))
        high = _f(snap.get("High"))
        low = _f(snap.get("Low"))
        pos = None
        if now is not None and high is not None and low is not None and high > low:
            pos = max(0.0, min(1.0, (now - low) / (high - low)))
        row: dict[str, Any] = {
            "name": str(names.get(suffix) or names.get(prefix) or ""),
            "vr": _f(mi.get("fLianB")),
            "speed": _f(mi.get("vzangsu")),
            "zjl": _f(mi.get("Zjl")),
            "wtb": _f(mi.get("Wtb")),
            "open_zaf": _f(mi.get("OpenZAF")),
            "f_hsl": _f(mi.get("fHSL")),
            "pos": pos,
            "depth_levels": depth_levels(snap),
            "now": now,
            "high": high,
            "low": low,
            "amount": _f(snap.get("Amount")),
            "l2_tic_num": _f(mi.get("L2TicNum")),
            "l2_order_num": _f(mi.get("L2OrderNum")),
            "total_b_vol": _f(mi.get("TotalBVol")),
            "total_s_vol": _f(mi.get("TotalSVol")),
            "ts": _f(snap.get("timestamp")),
        }
        out[prefix] = {key: value for key, value in row.items() if value is not None}
    return out


def compute_sector_strength(
    rows: Mapping[str, Mapping[str, Any]],
    memberships: Mapping[str, Sequence[str]],
    limit_up_codes: Sequence[str],
    *,
    min_members: int = 3,
) -> list[dict[str, Any]]:
    """板块成分 × 全市场快照 → 板块涨幅/涨跌家数/涨停数（按涨幅降序）。"""
    limit_up = {_suffix(code) for code in limit_up_codes or ()}
    out: list[dict[str, Any]] = []
    for name, codes in (memberships or {}).items():
        if not name or not isinstance(codes, Sequence):
            continue
        values: list[float] = []
        up = down = lu = 0
        for raw in codes:
            code = _suffix(raw)
            row = rows.get(code) or rows.get(raw)
            if not isinstance(row, Mapping):
                continue
            zaf = _f(row.get("Zaf"))
            if zaf is None:
                continue
            values.append(zaf)
            if zaf > 0:
                up += 1
            elif zaf < 0:
                down += 1
            if code in limit_up:
                lu += 1
        if len(values) < min_members:
            continue
        out.append(
            {
                "name": name,
                "n": len(values),
                "chg": round(sum(values) / len(values), 2),
                "up": up,
                "down": down,
                "limit_up": lu,
            }
        )
    return sorted(out, key=lambda row: row["chg"], reverse=True)


def count_consecutive_boards(
    bars: Sequence[Mapping[str, Any]],
    symbol: str,
    is_st: bool,
    today: date,
    *,
    threshold_fn: Callable[..., float] | None = None,
) -> int | None:
    """日线 → 截至昨日的连续涨停天数（今日首板 = 1）。

    ``bars`` 为升序日线（``Date``=YYYYMMDD、``Close``），可含今日（会被剔除）。
    数据不足 2 根 → ``None``（不知道就写 ``—``，不猜 1 板）。
    """
    if threshold_fn is None:
        from backend.services.simulation.services.local_market_data import (
            limit_threshold,
        )

        threshold_fn = limit_threshold

    rows: list[tuple[date, float]] = []
    for bar in bars or ():
        if not isinstance(bar, Mapping):
            continue
        raw_date = str(bar.get("Date") or "")
        close = _f(bar.get("Close"))
        if not raw_date or close is None or close <= 0:
            continue
        try:
            day = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        except (TypeError, ValueError):
            continue
        if day >= today:
            continue
        rows.append((day, close))
    if len(rows) < 2:
        return None

    boards = 1
    for idx in range(len(rows) - 1, 0, -1):
        day, close = rows[idx]
        prev_close = rows[idx - 1][1]
        if prev_close <= 0:
            break
        threshold = float(threshold_fn(symbol, is_st=is_st, trade_date=day))
        ret = close / prev_close - 1.0
        if ret >= threshold:
            boards += 1
        else:
            break
    return boards


def qmt_tick_to_snapshot(
    tick: Mapping[str, Any] | None, symbol: Any
) -> dict[str, Any]:
    """QMT ``get_full_tick`` → 标准快照字段（与 hot set/订阅写侧同构）。

    QMT 是免费 5 档源：``bidPrice/bidVol/askPrice/askVol`` 各 5 档。
    量纲：QMT ``volume``=手（与 TDX 契约一致）、``amount``=元（TDX 契约=万元，
    这里 ÷1e4 归一）；``time`` 毫秒 → 秒。缺最新价/昨收 → 空表（不造帧）。
    """
    if not isinstance(tick, Mapping):
        return {}
    last = _f(tick.get("lastPrice"))
    if last is None:
        last = _f(tick.get("last"))
    pre = _f(tick.get("lastClose"))
    if pre is None:
        pre = _f(tick.get("pre_close"))
    if last is None or pre is None or last <= 0 or pre <= 0:
        return {}
    out: dict[str, Any] = {
        "Now": last,
        "Open": _f(tick.get("open")),
        "High": _f(tick.get("high")),
        "Low": _f(tick.get("low")),
        "PreClose": pre,
        "Volume": _f(tick.get("volume")),
        "source": "qmt_full_tick",
        "symbol": _prefix(symbol),
    }
    amount = _f(tick.get("amount"))
    out["Amount"] = round(amount / 1e4, 4) if amount is not None else None
    raw_ts = _f(tick.get("time"))
    if raw_ts is not None and raw_ts > 0:
        out["timestamp"] = int(raw_ts / 1000 if raw_ts > 1e12 else raw_ts)
    for side, price_key, vol_key in (
        ("bid", "bidPrice", "bidVol"),
        ("ask", "askPrice", "askVol"),
    ):
        prices = tick.get(price_key) or []
        vols = tick.get(vol_key) or []
        has_px = isinstance(prices, Sequence)
        has_vol = isinstance(vols, Sequence)
        for i in range(5):
            px = _f(prices[i]) if has_px and len(prices) > i else None
            vol = _f(vols[i]) if has_vol and len(vols) > i else None
            out[f"{side}{i + 1}"] = px
            out[f"{side}_vol{i + 1}"] = vol
    return {key: value for key, value in out.items() if value is not None}


__all__ = [
    "PRICE_EPS_YUAN",
    "compute_breadth",
    "compute_metrics",
    "compute_sector_strength",
    "count_consecutive_boards",
    "depth_levels",
    "detect_events",
    "is_st_name",
    "qmt_tick_to_snapshot",
]
