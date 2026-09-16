"""市场交易时段唯一实现（T-P3-07）——**纯函数，无 IO**。

口径（设计决策 2026-09-16）：**live_trade_config 的时间一律为策略市场本地时钟**——
A股 "14:45" = 北京时间、美股 "15:50" = 美东时间、港股 "15:50" = 香港时间。
本地钟点不随夏令时漂移（校验/表单复杂度归零）；"现在是否在时段内"由调度器按
市场时区换算后判断（见 ``simulation_hosted_scheduler``）。

市场键归一：CN/a_share/A/sse→CN；US/us_stock/nasdaq→US；HK/hong_kong→HK；
CRYPTO→CRYPTO；FUTURES/期货→FUTURES；未知/空 → CN（存量兼容，保守）。

时段语义（与配置词表 enabled_sessions 对齐）：
- CN：AM 上午盘 / PM 下午盘 / AFTER_HOURS 盘后固定价格（15:05–15:30，2026-07-06 新规）；
- HK：AM 早盘 / PM 午盘（13:00–16:00）；
- US：AM 常规时段（09:30–16:00 连续无午休）/ AFTER_HOURS 盘后延长时段（16:00–20:00）；
- FUTURES：AM 日盘（09:00–15:00，简化口径——各品种夜盘时段差异见品种规则，
  模板按日盘设计）/ NIGHT 夜盘（21:00–02:30，跨午夜）；
- CRYPTO：7×24（AM 00:00–23:59）。
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

# 市场 → {时段名: (本地开始 HH:MM, 本地结束 HH:MM)}；NIGHT 允许跨午夜
MARKET_SESSIONS: dict[str, dict[str, tuple[str, str]]] = {
    "CN": {
        "AM": ("09:30", "11:30"),
        "PM": ("13:00", "15:00"),
        "AFTER_HOURS": ("15:05", "15:30"),
    },
    "HK": {
        "AM": ("09:30", "12:00"),
        "PM": ("13:00", "16:00"),
    },
    "US": {
        "AM": ("09:30", "16:00"),
        "AFTER_HOURS": ("16:00", "20:00"),
    },
    "FUTURES": {
        "AM": ("09:00", "15:00"),
        "NIGHT": ("21:00", "02:30"),
    },
    "CRYPTO": {
        "AM": ("00:00", "23:59"),
    },
}

_MARKET_TZ: dict[str, str] = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
    "FUTURES": "Asia/Shanghai",
    "CRYPTO": "UTC",
}

_MARKET_CALENDAR: dict[str, str | None] = {
    "CN": "XSHG",
    "HK": "XHKG",
    "US": "XNYS",
    "FUTURES": None,  # 商品夜盘跨日历日，节假日以品种为准 → 回落工作日判断
    "CRYPTO": None,  # 7×24
}

_ALIASES: dict[str, str] = {
    "CN": "CN",
    "A": "CN",
    "A_SHARE": "CN",
    "ASHARE": "CN",
    "SSE": "CN",
    "SZSE": "CN",
    "US": "US",
    "US_STOCK": "US",
    "USSTOCK": "US",
    "NASDAQ": "US",
    "NYSE": "US",
    "HK": "HK",
    "HONG_KONG": "HK",
    "HONGKONG": "HK",
    "HKEX": "HK",
    "CRYPTO": "CRYPTO",
    "COIN": "CRYPTO",
    "FUTURES": "FUTURES",
    "FUT": "FUTURES",
    "期货": "FUTURES",
}


def normalize_market_key(raw: Any) -> str:
    """任意市场标识 → 规范键（CN/US/HK/CRYPTO/FUTURES）；未知/空 → CN（保守）。"""
    text = str(raw or "").strip().upper()
    if not text:
        return "CN"
    return _ALIASES.get(text, "CN")


def session_ranges_local(market: Any) -> dict[str, tuple[str, str]]:
    """市场 → 本地时钟时段表（拷贝，防调用方改写）。未知市场回落 CN。"""
    key = normalize_market_key(market)
    return dict(MARKET_SESSIONS.get(key, MARKET_SESSIONS["CN"]))


def market_timezone(market: Any) -> ZoneInfo:
    """市场 → 时区（调度器据此换算"现在"）。"""
    key = normalize_market_key(market)
    return ZoneInfo(_MARKET_TZ.get(key, "Asia/Shanghai"))


def market_calendar(market: Any) -> str | None:
    """市场 → exchange_calendars 标识（None = 回落工作日判断，如 7×24 或夜盘跨日）。"""
    return _MARKET_CALENDAR.get(normalize_market_key(market))


def in_session_hhmm(now_hhmm: str, start: str, end: str) -> bool:
    """HH:MM 是否落在 [start, end]（含端点）；支持跨午夜时段（start > end，如夜盘）。"""
    text = str(now_hhmm or "").strip()
    if not text or not start or not end:
        return False
    if start <= end:
        return start <= text <= end
    # 跨午夜：now ≥ start（当日）或 now ≤ end（次日凌晨）
    return text >= start or text <= end


def is_market_session_active(market: Any, session: Any, now: Any) -> bool:
    """市场时段在"现在"（aware datetime，任意时区）是否激活——按市场本地钟换算。"""
    key = normalize_market_key(market)
    sess = str(session or "").strip().upper()
    window = MARKET_SESSIONS.get(key, {}).get(sess)
    if window is None:
        return False
    local_now = now.astimezone(market_timezone(key))
    return in_session_hhmm(local_now.strftime("%H:%M"), window[0], window[1])
