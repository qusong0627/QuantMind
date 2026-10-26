# -*- coding: utf-8 -*-
"""minibt 模拟盘桥：把当日实时快照补成「今天这根日线」。

minibt 策略的信号口径是「当根 K 线收盘价成交」。日线历史来自 QuantDB
（``daily_forward``，volume/amount 单位为股/元），当天收盘前这根 K 线
必须由实时快照合成，否则策略只能看到昨天收盘 —— 信号会整整晚一天。

单位契约（与写侧一致，见 ``redis_series_quote`` 与通达信桥实测文档）：
- 快照 ``Volume`` 为**手** → 本模块 ×100 归一为**股**；
- 快照 ``Amount`` 为**万元**（TDX 原始口径），与 QuantDB A 股日线
  ``amount``（实测 600036 2026-10-23：516,087 万元）同一把尺，不换算；
- 快照时间戳缺失 / 超龄 / 现价非法 → 抛 :class:`LiveBarUnavailable`，
  **绝不用旧价或持仓成本价伪造一根 K 线**。
"""

from __future__ import annotations

from datetime import date, datetime
from collections.abc import Mapping
from typing import Any

import pandas as pd

from backend.shared.minibt_qdb import _DAILY_COLUMNS

# 快照成交量单位是手（100 股），QuantDB 日线是股。
LOTS_TO_SHARES = 100
DEFAULT_LIVE_MAX_AGE_SECONDS = 180.0


class LiveBarUnavailable(RuntimeError):
    """实时快照不足以合成当日 K 线；``args[0]`` 是机器可读原因。"""


class TargetExtractError(RuntimeError):
    """回测结果里取不到目标持仓股数；``args[0]`` 是机器可读原因。"""


def extract_last_size(frame: Any) -> float:
    """回测结果 DataFrame → 最后一根的**带符号持仓股数**。

    ⚠️ 必须用 ``sizes`` 列，不能用 ``positions``：后者是方向（-1/0/1），
    2026-10-26 实测买 100 股的 ``positions`` 是 1.0、``sizes`` 才是 100；
    拿 ``positions`` 当股数会把 100 股下成 1 股。
    """
    if frame is None or len(frame) == 0:
        raise TargetExtractError("backtest_no_result")
    if "sizes" not in getattr(frame, "columns", []):
        raise TargetExtractError("result_missing_sizes")
    return float(frame["sizes"].astype(float).iloc[-1])


def snapshot_keys(symbol: str) -> list[str]:
    """标的 → 行情快照候选键（四形并存，写侧口径见 decision_context_source）。

    读侧不能只认一种：TDX 订阅引擎写小写前缀（``sh600036``），远端行情服实测
    写后缀式（``600036.SH``）。四形都发、先命中先赢，缺键才是真缺。
    """
    from backend.shared.stock_utils import StockCodeUtil

    suffix = (
        StockCodeUtil.to_suffix(str(symbol or "").strip()) or str(symbol or "").strip()
    )
    if not suffix:
        return []
    if "." not in suffix:
        raw = [suffix.lower(), suffix.upper()]
        keys = [f"market:snapshot:{item}" for item in raw]
    else:
        code, market = suffix.split(".", 1)
        keys = [
            f"market:snapshot:{suffix.upper()}",
            f"market:snapshot:{suffix.lower()}",
            f"market:snapshot:{market.lower()}{code}",
            f"market:snapshot:{market.upper()}{code}",
        ]
    return list(dict.fromkeys(keys))


def read_snapshot(client: Any, symbol: str) -> dict[str, Any] | None:
    """读单只标的快照 Hash；四形都 miss 返回 None（不抛、不用旧值兜底）。"""
    if client is None:
        return None
    for key in snapshot_keys(symbol):
        try:
            data = client.hgetall(key)
        except Exception:  # noqa: BLE001 - 单键读失败继续试下一形
            data = None
        if data:
            return {str(k): v for k, v in data.items()}
    return None


def series_keys(symbol: str) -> list[str]:
    """标的 → ``market:series`` 候选键（大写/小写前缀 + 后缀式）。"""
    from backend.shared.stock_utils import StockCodeUtil

    text = str(symbol or "").strip()
    if not text:
        return []
    prefix = StockCodeUtil.to_prefix(text)
    suffix = StockCodeUtil.to_suffix(text)
    candidates = [
        prefix,
        prefix.lower(),
        suffix,
        suffix.lower() if suffix else "",
    ]
    return list(dict.fromkeys(f"market:series:{item}" for item in candidates if item))


def read_latest_series(client: Any, symbol: str) -> dict[str, Any] | None:
    """读 ``market:series`` ZSET 最新一条，映射成快照同名字段。

    收盘后 Hash 快照有 TTL（300s）会消失，ZSET 序列仍在；没有这层回退，
    14:50 决策时会因为「快照刚过期」直接失败。序列成交量同为**手**，
    成交额同为**万元**（实测 source=tdx_bridge）。
    """
    import json

    if client is None:
        return None
    for key in series_keys(symbol):
        try:
            rows = client.zrevrange(key, 0, 0, withscores=True)
        except Exception:  # noqa: BLE001 - 单键读失败继续试下一形
            rows = None
        if not rows:
            continue
        member, score = rows[0]
        if isinstance(member, bytes):
            member = member.decode("utf-8", errors="ignore")
        try:
            payload = json.loads(member)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        price = payload.get("price")
        if price is None:
            price = payload.get("close")
        if price is None:
            continue
        return {
            "Now": price,
            "Open": payload.get("open"),
            "High": payload.get("high"),
            "Low": payload.get("low"),
            "Volume": payload.get("volume"),
            "Amount": payload.get("amount"),
            "timestamp": payload.get("timestamp") or score,
            "source": str(payload.get("source") or "series"),
        }
    return None


def read_live_quote(client: Any, symbol: str) -> dict[str, Any] | None:
    """快照 Hash 优先，缺键回退 series 最新一条。"""
    return read_snapshot(client, symbol) or read_latest_series(client, symbol)


def _f(snapshot: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in snapshot:
            continue
        raw = snapshot.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value == value and value not in (float("inf"), float("-inf")):
            return value
    return None


def snapshot_age_seconds(
    snapshot: Mapping[str, Any], *, now_ts: float | None = None
) -> float | None:
    """快照水印 → 年龄（秒）；无可用水印返回 None（宁可不判定）。"""
    ts = _f(snapshot, "timestamp", "ts")
    if ts is None or ts <= 0:
        return None
    if ts > 1e12:  # 毫秒时间戳
        ts /= 1000.0
    now_value = datetime.now().timestamp() if now_ts is None else float(now_ts)
    return now_value - ts


def build_live_bar(
    snapshot: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: date,
    now_ts: float | None = None,
    max_age_seconds: float = DEFAULT_LIVE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """实时快照 → 一根日线（字段与 QuantDB 日线一致）。"""
    if not isinstance(snapshot, Mapping) or not snapshot:
        raise LiveBarUnavailable("snapshot_missing")
    age = snapshot_age_seconds(snapshot, now_ts=now_ts)
    if age is None:
        raise LiveBarUnavailable("snapshot_timestamp_missing")
    if age > float(max_age_seconds):
        raise LiveBarUnavailable(f"snapshot_stale:{age:.0f}s")
    if age < -30.0:
        raise LiveBarUnavailable(f"snapshot_clock_skew:{age:.0f}s")

    close = _f(snapshot, "Now", "now", "price")
    pre_close = _f(snapshot, "PreClose", "pre_close")
    if close is None or close <= 0:
        raise LiveBarUnavailable("snapshot_price_invalid")
    open_price = _f(snapshot, "Open", "open")
    high = _f(snapshot, "High", "high")
    low = _f(snapshot, "Low", "low")
    if open_price is None or open_price <= 0:
        open_price = pre_close if pre_close and pre_close > 0 else close
    if high is None or high <= 0:
        high = max(open_price, close)
    if low is None or low <= 0:
        low = min(open_price, close)
    high = max(high, open_price, close)
    low = min(low, open_price, close)

    volume_lots = _f(snapshot, "Volume", "volume")
    volume = max(0.0, volume_lots * LOTS_TO_SHARES) if volume_lots is not None else 0.0
    amount = _f(snapshot, "Amount", "amount")
    if amount is None or amount < 0:
        # QuantDB A 股日线 amount 单位为万元（实测），缺字段时按同一口径补
        amount = close * volume / 10000.0

    return {
        "datetime": pd.Timestamp(trade_date),
        "open": float(open_price),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
        "amount": float(amount),
    }


def append_live_bar(
    df: pd.DataFrame,
    *,
    snapshot: Mapping[str, Any] | None,
    symbol: str,
    trade_date: date,
    now_ts: float | None = None,
    max_age_seconds: float = DEFAULT_LIVE_MAX_AGE_SECONDS,
) -> tuple[pd.DataFrame, str]:
    """历史日线 + 当日快照 → 含今天的日线。

    Returns:
        (新 DataFrame, bar_source)。``bar_source`` 为 ``history_has_today``
        （QuantDB 已含今天）或 ``live_bar_appended``。
    """
    if df is None or df.empty:
        raise LiveBarUnavailable("history_empty")
    if "datetime" not in df.columns:
        raise LiveBarUnavailable("history_missing_datetime")
    last_dt = pd.Timestamp(df["datetime"].iloc[-1])
    if last_dt.date() >= trade_date:
        return df, "history_has_today"
    row = build_live_bar(
        snapshot or {},
        symbol=symbol,
        trade_date=trade_date,
        now_ts=now_ts,
        max_age_seconds=max_age_seconds,
    )
    # pandas 3 CoW：concat 生成新对象，绝不改调用方 DataFrame
    merged = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    merged = merged[_DAILY_COLUMNS]
    try:
        merged.attrs["symbol"] = df.attrs.get("symbol", symbol)
    except Exception:
        pass
    return merged, "live_bar_appended"
