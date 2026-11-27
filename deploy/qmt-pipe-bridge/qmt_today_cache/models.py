# -*- coding: utf-8 -*-
"""今日行情缓存的规范化与纯函数（无 I/O，Windows/Linux 可测）。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

#: A 股时间固定 UTC+8（Windows 上不依赖 tzdata/zoneinfo）。
CN_TZ = timezone(timedelta(hours=8))

SYMBOL_RE = re.compile(r"^[0-9A-Za-z]{1,12}(?:\.[A-Za-z]{2})?$")
DATE_RE = re.compile(r"^\d{8}$")

#: QMT tick 里下游会用到的字段（保留原字段名，缓存命中时形状不变）。
TICK_KEEP = (
    "timetag",
    "time",
    "lastPrice",
    "lastClose",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "pvolume",
    "transactionNum",
    "stockStatus",
    "lastSettlementPrice",
    "settlementPrice",
    "openInt",
    "bidPrice",
    "askPrice",
    "bidVol",
    "askVol",
)

#: 缓存聚合时段：比报单时段宽一点，覆盖集合竞价与收盘缓冲。
CACHE_SESSIONS = ((9, 0, 11, 40), (12, 50, 15, 10))

BAR1M_FIELDS = (
    "date",
    "symbol",
    "minute_ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "vwap",
    "cum_volume",
    "cum_amount",
    "source",
    "final",
)


def safe_float(value, default=None):
    """转 float；空值/非法返回 ``default``。"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    """转 int；非法返回 ``default``。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_symbol(code) -> str:
    """统一大写/去空白；非法返回空串。QMT/QuantDB 用后缀式。"""
    text = str(code or "").strip().upper()
    if not text or not SYMBOL_RE.match(text):
        return ""
    return text


def is_valid_symbol(code) -> bool:
    return bool(normalize_symbol(code))


def is_valid_date(date_text) -> bool:
    return bool(DATE_RE.match(str(date_text or "").strip()))


def parse_timetag_ms(tick) -> int | None:
    """QMT tick 的 ``timetag``（YYYYMMDD HH:MM:SS）→ epoch ms。

    也接受 ``time`` 为毫秒/秒时间戳。解析失败返回 None。
    """
    if not isinstance(tick, dict):
        return None
    raw = str(tick.get("timetag") or "").strip()
    for fmt in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=CN_TZ)
            return int(dt.timestamp() * 1000)
        except (TypeError, ValueError):
            continue
    raw_time = tick.get("time")
    if raw_time is not None:
        value = safe_float(raw_time)
        if value is not None and value > 0:
            # QMT 可能是毫秒；小于 10^11 视为秒。
            return int(value if value > 1e11 else value * 1000)
    return None


def tick_ts_ms(tick, now_ms: int) -> int:
    """tick 时间戳；解析失败用 now_ms（只影响新鲜度判断，不伪造 bar 时间）。"""
    return parse_timetag_ms(tick) or int(now_ms)


def normalize_tick(tick) -> dict | None:
    """QMT tick → 可缓存的浅拷贝；无有效 lastPrice 返回 None。"""
    if not isinstance(tick, dict):
        return None
    last = safe_float(tick.get("lastPrice"))
    if last is None or last <= 0:
        return None
    out = {}
    for key in TICK_KEEP:
        if key not in tick:
            continue
        value = tick.get(key)
        if isinstance(value, (list, tuple)):
            out[key] = list(value)
        else:
            out[key] = value
    # 有些上游把 bidPrice/askPrice 平铺成 bid1..5；保持原样即可，下游按字段取。
    if "lastPrice" not in out:
        out["lastPrice"] = last
    return out


def tick_cum_volume(tick) -> float:
    return safe_float(tick.get("volume"), 0.0) or 0.0


def tick_cum_amount(tick) -> float:
    return safe_float(tick.get("amount"), 0.0) or 0.0


def tick_last_price(tick) -> float | None:
    return safe_float(tick.get("lastPrice"))


def minute_start_ms(ts_ms: int) -> int:
    value = int(ts_ms)
    return value - (value % 60000)


def date_str(ts_ms: int) -> str:
    """epoch ms → 交易日 YYYYMMDD（Asia/Shanghai）。"""
    return datetime.fromtimestamp(float(ts_ms) / 1000.0, CN_TZ).strftime("%Y%m%d")


def day_bounds_ms(date_text: str) -> tuple[int, int]:
    """YYYYMMDD → 当日 00:00:00 / 23:59:59.999（Asia/Shanghai）。"""
    if not is_valid_date(date_text):
        raise ValueError("bad date: %r" % (date_text,))
    year, month, day = int(date_text[:4]), int(date_text[4:6]), int(date_text[6:8])
    start = datetime(year, month, day, 0, 0, 0, tzinfo=CN_TZ)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def parse_range_ms(start_value, end_value, now_ms: int) -> tuple[int, int]:
    """cache_today_1m 的时间参数 → (start_ms, end_ms)。

    * 8 位 ``YYYYMMDD``：start 取当日 00:00，end 取当日 23:59:59.999；
    * 数字/数字串 → epoch ms（小于 10^11 视为秒）；
    * 空值 → 今天。
    """
    today = date_str(now_ms)
    start_ms = _parse_one_ms(start_value, end=False)
    end_ms = _parse_one_ms(end_value, end=True)
    if start_ms is None:
        start_ms = day_bounds_ms(today)[0]
    if end_ms is None:
        end_ms = day_bounds_ms(today)[1]
    if end_ms < start_ms:
        start_ms, end_ms = end_ms, start_ms
    return int(start_ms), int(end_ms)


def _parse_one_ms(value, *, end: bool) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        bounds = day_bounds_ms(text)
        return bounds[1] if end else bounds[0]
    number = safe_float(text)
    if number is None or number <= 0:
        return None
    return int(number if number > 1e11 else number * 1000)


def in_cache_session(now_ms: int) -> bool:
    """是否缓存采集时段（周一至周五，9:00-11:40 / 12:50-15:10）。"""
    dt = datetime.fromtimestamp(float(now_ms) / 1000.0, CN_TZ)
    if dt.weekday() >= 5:
        return False
    hm = (dt.hour, dt.minute)
    return any((sh, sm) <= hm < (eh, em) for sh, sm, eh, em in CACHE_SESSIONS)


def public_bar(row: dict) -> dict:
    """去掉内部字段，返回给 RPC 的 bar。"""
    return {k: row.get(k) for k in BAR1M_FIELDS if k in row}


def vwap_of(amount: float, volume: float, fallback: float = 0.0) -> float:
    if volume and volume > 0:
        return float(amount) / float(volume)
    return float(fallback or 0.0)
