from __future__ import annotations

import logging
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

# ============================================================================
# 市场定义
# ============================================================================


@dataclass(frozen=True)
class MarketDefinition:
    code: str           # 项目内部市场代码
    xcal_name: str      # exchange_calendars 日历名
    timezone: str       # 时区
    name_zh: str        # 中文名称
    open_time: time     # 开盘时间
    close_time: time    # 收盘时间
    sessions: list[dict[str, Any]] = field(default_factory=list)  # 交易时段
    default: bool = False  # 是否为默认市场

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


ALL_MARKETS: dict[str, MarketDefinition] = {
    # 中国市场
    "SSE": MarketDefinition(
        "SSE", "XSHG", "Asia/Shanghai", "上交所",
        time(9, 30), time(15, 0), default=True,
        sessions=[
            {"session_name": "AM", "start_time": "09:30:00", "end_time": "11:30:00", "cross_day": False},
            {"session_name": "PM", "start_time": "13:00:00", "end_time": "15:00:00", "cross_day": False},
        ],
    ),
    "SZSE": MarketDefinition(
        "SZSE", "XSHG", "Asia/Shanghai", "深交所",
        time(9, 30), time(15, 0),
        sessions=[
            {"session_name": "AM", "start_time": "09:30:00", "end_time": "11:30:00", "cross_day": False},
            {"session_name": "PM", "start_time": "13:00:00", "end_time": "15:00:00", "cross_day": False},
        ],
    ),
    "CFFEX": MarketDefinition(
        "CFFEX", "XSHG", "Asia/Shanghai", "中金所",
        time(9, 30), time(15, 0),
        sessions=[
            {"session_name": "AM", "start_time": "09:30:00", "end_time": "11:30:00", "cross_day": False},
            {"session_name": "PM", "start_time": "13:00:00", "end_time": "15:00:00", "cross_day": False},
        ],
    ),
    # 美国市场
    "XNYS": MarketDefinition(
        "XNYS", "XNYS", "America/New_York", "纽交所",
        time(9, 30), time(16, 0),
        sessions=[
            {"session_name": "REGULAR", "start_time": "09:30:00", "end_time": "16:00:00", "cross_day": False},
        ],
    ),
    "XNAS": MarketDefinition(
        "XNAS", "XNAS", "America/New_York", "纳斯达克",
        time(9, 30), time(16, 0),
        sessions=[
            {"session_name": "REGULAR", "start_time": "09:30:00", "end_time": "16:00:00", "cross_day": False},
        ],
    ),
    # 港股市场
    "XHKG": MarketDefinition(
        "XHKG", "XHKG", "Asia/Hong_Kong", "港交所",
        time(9, 30), time(16, 0),
        sessions=[
            {"session_name": "AM", "start_time": "09:30:00", "end_time": "12:00:00", "cross_day": False},
            {"session_name": "PM", "start_time": "13:00:00", "end_time": "16:00:00", "cross_day": False},
        ],
    ),
    # 日本市场
    "XTKS": MarketDefinition(
        "XTKS", "XTKS", "Asia/Tokyo", "东京交易所",
        time(9, 0), time(15, 0),
        sessions=[
            {"session_name": "AM", "start_time": "09:00:00", "end_time": "11:30:00", "cross_day": False},
            {"session_name": "PM", "start_time": "12:30:00", "end_time": "15:00:00", "cross_day": False},
        ],
    ),
    # 韩国市场
    "XKRX": MarketDefinition(
        "XKRX", "XKRX", "Asia/Seoul", "韩国交易所",
        time(9, 0), time(15, 30),
        sessions=[
            {"session_name": "REGULAR", "start_time": "09:00:00", "end_time": "15:30:00", "cross_day": False},
        ],
    ),
}

# 向后兼容：旧市场代码到 exchange_calendars 的映射
_MARKET_TO_XCAL = {code: m.xcal_name for code, m in ALL_MARKETS.items()}

# 向后兼容：旧时区映射
_DEFAULT_MARKET_TZ = {code: m.timezone for code, m in ALL_MARKETS.items()}

# 向后兼容：旧会话默认值
_DEFAULT_SESSIONS = {code: m.sessions for code, m in ALL_MARKETS.items()}

# 股票代码前缀到市场的映射
_SYMBOL_PREFIX_TO_MARKET = {
    "SH": "SSE", "SZ": "SZSE", "BJ": "SSE",  # 北交所也用 SSE 日历
    "US": "XNYS", "NASDAQ": "XNAS",
    "HK": "XHKG",
    "JP": "XTKS", "T": "XTKS",
    "KR": "XKRX", "KS": "XKRX", "KQ": "XKRX",
}


# 交易所惯用名 -> ALL_MARKETS 的规范代码。
# 前端 config/marketConfig.ts 传的是惯用名（SSE / HKEX / NYSE），而 ALL_MARKETS 的键是
# exchange_calendars 的代码（SSE / XHKG / XNYS）。少了这层别名，get_market('NYSE') 会抛错、
# 被调用方吞掉后退化成「只看周末」，于是美股假日（如 2026-07-03 独立日）被当成交易日 —— 静默错。
_MARKET_ALIASES = {
    "NYSE": "XNYS", "US": "XNYS", "NASD": "XNAS", "NASDAQ": "XNAS",
    "HKEX": "XHKG", "HK": "XHKG",
    "SSE": "SSE", "SH": "SSE", "SZSE": "SZSE", "SZ": "SZSE",
}


def get_market(market_code: str) -> MarketDefinition:
    """获取市场定义，支持惯用名(如 NYSE/HKEX)与 exchange_calendars 代码(如 XNYS)"""
    code = str(market_code or "").strip().upper()
    if code in ALL_MARKETS:
        return ALL_MARKETS[code]
    alias = _MARKET_ALIASES.get(code)
    if alias and alias in ALL_MARKETS:
        return ALL_MARKETS[alias]
    # 尝试通过 xcal_name 反查
    for m in ALL_MARKETS.values():
        if m.xcal_name == code:
            return m
    raise ValueError(f"未知市场代码: {market_code}. 可用: {list(ALL_MARKETS.keys())}")


def resolve_market_from_symbol(symbol: str) -> str | None:
    """根据股票代码推断市场代码"""
    sym = str(symbol or "").strip().upper()
    # 前缀格式: SH600036 -> SSE
    for prefix, market in _SYMBOL_PREFIX_TO_MARKET.items():
        if sym.startswith(prefix):
            return market
    # 后缀格式: 600036.SH -> SSE
    if "." in sym:
        suffix = sym.split(".")[-1]
        if suffix in _SYMBOL_PREFIX_TO_MARKET:
            return _SYMBOL_PREFIX_TO_MARKET[suffix]
    return None


def _get_xcal_calendar(market_code: str):
    """获取 exchange_calendars 的 Calendar 对象"""
    try:
        import exchange_calendars as xcals
        market = get_market(market_code)
        return xcals.get_calendar(market.xcal_name)
    except ImportError:
        logger.warning("exchange_calendars 未安装")
        return None
    except Exception as e:
        logger.warning("exchange_calendars 获取日历失败 market=%s: %s", market_code, e)
        return None


# ============================================================================
# TradingCalendarService
# ============================================================================


@dataclass
class SessionWindow:
    session_name: str
    start_at: datetime
    end_at: datetime
    cross_day: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_name": self.session_name,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "cross_day": self.cross_day,
        }


class TradingCalendarService:
    async def ensure_tables(self) -> None:
        stmts = [
            """
            CREATE TABLE IF NOT EXISTS qm_market_calendar_day (
                market VARCHAR(32) NOT NULL,
                trade_date DATE NOT NULL,
                is_trading_day BOOLEAN NOT NULL,
                timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
                source VARCHAR(64) NOT NULL DEFAULT 'manual',
                version VARCHAR(64),
                tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
                user_id VARCHAR(64) NOT NULL DEFAULT '*',
                metadata_json JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (market, trade_date, tenant_id, user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS qm_market_trading_session (
                market VARCHAR(32) NOT NULL,
                session_name VARCHAR(64) NOT NULL,
                start_time TIME NOT NULL,
                end_time TIME NOT NULL,
                cross_day BOOLEAN NOT NULL DEFAULT FALSE,
                trade_date_rule VARCHAR(64) NOT NULL DEFAULT 'TRADE_DATE',
                timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
                tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
                user_id VARCHAR(64) NOT NULL DEFAULT '*',
                metadata_json JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (market, session_name, tenant_id, user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS qm_market_calendar_exception (
                id BIGSERIAL PRIMARY KEY,
                market VARCHAR(32) NOT NULL,
                trade_date DATE NOT NULL,
                action VARCHAR(16) NOT NULL,
                reason TEXT,
                tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
                user_id VARCHAR(64) NOT NULL DEFAULT '*',
                approved_by VARCHAR(128),
                metadata_json JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS qm_market_calendar_version (
                market VARCHAR(32) NOT NULL,
                year INTEGER NOT NULL,
                checksum VARCHAR(128) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'draft',
                source VARCHAR(64),
                published_at TIMESTAMPTZ,
                metadata_json JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (market, year)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_calendar_day_query
            ON qm_market_calendar_day (market, tenant_id, user_id, trade_date)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_calendar_exception_query
            ON qm_market_calendar_exception (market, tenant_id, user_id, trade_date)
            """,
        ]
        async with get_session() as session:
            for stmt in stmts:
                await session.execute(text(stmt))

    @staticmethod
    def _normalize_scope(tenant_id: str | None, user_id: str | None) -> tuple[str, str]:
        tenant = str(tenant_id or "default").strip() or "default"
        user = str(user_id or "").strip()
        if not user:
            raise ValueError("user_id is required")
        return tenant, user

    @staticmethod
    def _normalize_market(market: str) -> str:
        m = str(market or "").strip().upper()
        if not m:
            raise ValueError("market is required")
        # 自动解析: 如果是股票代码，推断市场
        if "." in m or m.isdigit() or len(m) > 6:
            resolved = resolve_market_from_symbol(m)
            if resolved:
                return resolved
        return m

    @staticmethod
    def _normalize_trade_date(value: date | datetime | str) -> date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        text_value = str(value).strip()
        if not text_value:
            raise ValueError("trade_date is required")
        return date.fromisoformat(text_value[:10])

    @staticmethod
    def _parse_time_value(v: Any) -> time:
        if isinstance(v, time):
            return v
        raw = str(v).strip()
        if len(raw) == 5:
            raw = f"{raw}:00"
        return time.fromisoformat(raw)

    @staticmethod
    def _safe_timezone(tz_name: str | None, market: str) -> str:
        candidate = str(tz_name or "").strip()
        if not candidate:
            try:
                candidate = _DEFAULT_MARKET_TZ.get(market, "Asia/Shanghai")
            except Exception:
                candidate = "Asia/Shanghai"
        try:
            ZoneInfo(candidate)
            return candidate
        except Exception:
            logger.warning("Invalid timezone %s for market %s, fallback Asia/Shanghai", candidate, market)
            return "Asia/Shanghai"

    # =========================================================================
    # 主逻辑：exchange_calendars 为主数据源
    # =========================================================================

    def _is_trading_day_xcal(self, market: str, d: date) -> bool | None:
        """通过 exchange_calendars 判断交易日。返回 None 表示不可用"""
        cal = _get_xcal_calendar(market)
        if cal is None:
            return None
        try:
            return bool(cal.is_session(d))
        except Exception:
            return None

    def _next_trading_day_xcal(self, market: str, d: date) -> date | None:
        """通过 exchange_calendars 获取下一交易日"""
        cal = _get_xcal_calendar(market)
        if cal is None:
            return None
        try:
            next_session = cal.next_session(d)
            if next_session is not None:
                return next_session.date() if hasattr(next_session, 'date') else next_session
            # fallback: 暴力搜索
            cursor = d + timedelta(days=1)
            for _ in range(370):
                if cal.is_session(cursor):
                    return cursor
                cursor += timedelta(days=1)
            return None
        except Exception:
            return None

    def _prev_trading_day_xcal(self, market: str, d: date) -> date | None:
        """通过 exchange_calendars 获取上一交易日"""
        cal = _get_xcal_calendar(market)
        if cal is None:
            return None
        try:
            prev_session = cal.previous_session(d)
            if prev_session is not None:
                return prev_session.date() if hasattr(prev_session, 'date') else prev_session
            # fallback: 暴力搜索
            cursor = d - timedelta(days=1)
            for _ in range(370):
                if cal.is_session(cursor):
                    return cursor
                cursor -= timedelta(days=1)
            return None
        except Exception:
            return None

    # =========================================================================
    # DB Override 层：查询用户自定义覆盖
    # =========================================================================

    async def _find_db_override(self, *, market: str, trade_date: date, tenant_id: str, user_id: str) -> bool | None:
        """查询 DB 中是否有手动设置的日期覆盖。返回 None 表示无覆盖"""
        tenant, user = self._normalize_scope(tenant_id, user_id)
        scope = [
            (tenant, user, 1),
            (tenant, "*", 2),
            ("default", "*", 3),
        ]
        async with get_session(read_only=True) as session:
            try:
                row = await session.execute(
                    text(
                        """
                        SELECT is_trading_day
                        FROM qm_market_calendar_day
                        WHERE market = :market
                          AND trade_date = :trade_date
                          AND (
                            (tenant_id = :tenant_1 AND user_id = :user_1)
                            OR (tenant_id = :tenant_2 AND user_id = :user_2)
                            OR (tenant_id = :tenant_3 AND user_id = :user_3)
                          )
                        ORDER BY CASE
                            WHEN tenant_id = :tenant_1 AND user_id = :user_1 THEN 1
                            WHEN tenant_id = :tenant_2 AND user_id = :user_2 THEN 2
                            ELSE 3
                        END
                        LIMIT 1
                        """
                    ),
                    {
                        "market": market,
                        "trade_date": trade_date,
                        "tenant_1": scope[0][0],
                        "user_1": scope[0][1],
                        "tenant_2": scope[1][0],
                        "user_2": scope[1][1],
                        "tenant_3": scope[2][0],
                        "user_3": scope[2][1],
                    },
                )
                item = row.mappings().first()
                return bool(item["is_trading_day"]) if item else None
            except Exception as exc:
                logger.warning("query qm_market_calendar_day failed: %s", exc)
                return None

    # =========================================================================
    # 公开 API
    # =========================================================================

    async def is_trading_day(
        self,
        *,
        market: str,
        trade_date: date | datetime | str,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        mkt = self._normalize_market(market)
        d = self._normalize_trade_date(trade_date)

        # 1. 先查 DB override
        db_override = await self._find_db_override(
            market=mkt, trade_date=d, tenant_id=tenant_id, user_id=user_id
        )
        if db_override is not None:
            return db_override

        # 2. 主数据源：exchange_calendars
        xcal_result = self._is_trading_day_xcal(mkt, d)
        if xcal_result is not None:
            return xcal_result

        # 3. Fallback：周末判断
        if d.weekday() >= 5:
            return False
        logger.warning("calendar unavailable for market=%s date=%s, fallback to weekday check", mkt, d)
        return True

    async def next_trading_day(
        self,
        *,
        market: str,
        trade_date: date | datetime | str,
        tenant_id: str,
        user_id: str,
    ) -> date:
        mkt = self._normalize_market(market)
        cursor = self._normalize_trade_date(trade_date) + timedelta(days=1)

        # 1. exchange_calendars 为主
        xcal_next = self._next_trading_day_xcal(mkt, self._normalize_trade_date(trade_date))
        if xcal_next is not None:
            # 检查是否有 DB override 需要跳过
            db_override = await self._find_db_override(
                market=mkt, trade_date=xcal_next, tenant_id=tenant_id, user_id=user_id
            )
            if db_override is True:
                return xcal_next
            # 如果被 DB override 标记为非交易日，继续找下一个
            if db_override is False:
                # 暴力搜索
                for _ in range(370):
                    is_td = await self.is_trading_day(
                        market=mkt, trade_date=cursor, tenant_id=tenant_id, user_id=user_id
                    )
                    if is_td:
                        return cursor
                    cursor += timedelta(days=1)
            return xcal_next

        # 2. Fallback：暴力搜索
        for _ in range(370):
            if await self.is_trading_day(
                market=mkt, trade_date=cursor, tenant_id=tenant_id, user_id=user_id
            ):
                return cursor
            cursor += timedelta(days=1)
        raise RuntimeError(f"next_trading_day not found in 370 days for market={mkt}")

    async def prev_trading_day(
        self,
        *,
        market: str,
        trade_date: date | datetime | str,
        tenant_id: str,
        user_id: str,
    ) -> date:
        mkt = self._normalize_market(market)
        cursor = self._normalize_trade_date(trade_date) - timedelta(days=1)

        # 1. exchange_calendars 为主
        xcal_prev = self._prev_trading_day_xcal(mkt, self._normalize_trade_date(trade_date))
        if xcal_prev is not None:
            db_override = await self._find_db_override(
                market=mkt, trade_date=xcal_prev, tenant_id=tenant_id, user_id=user_id
            )
            if db_override is True:
                return xcal_prev
            if db_override is False:
                for _ in range(370):
                    is_td = await self.is_trading_day(
                        market=mkt, trade_date=cursor, tenant_id=tenant_id, user_id=user_id
                    )
                    if is_td:
                        return cursor
                    cursor -= timedelta(days=1)
            return xcal_prev

        # 2. Fallback
        for _ in range(370):
            if await self.is_trading_day(
                market=mkt, trade_date=cursor, tenant_id=tenant_id, user_id=user_id
            ):
                return cursor
            cursor -= timedelta(days=1)
        raise RuntimeError(f"prev_trading_day not found in 370 days for market={mkt}")

    async def get_sessions(
        self,
        *,
        market: str,
        trade_date: date | datetime | str,
        tenant_id: str,
        user_id: str,
    ) -> list[SessionWindow]:
        mkt = self._normalize_market(market)
        d = self._normalize_trade_date(trade_date)

        # 1. 尝试从 DB 获取自定义会话
        rows = await self._find_sessions(market=mkt, tenant_id=tenant_id, user_id=user_id)
        if not rows:
            # 2. 使用市场默认会话
            try:
                market_def = get_market(mkt)
                rows = market_def.sessions or _DEFAULT_SESSIONS.get(mkt, [])
            except ValueError:
                rows = _DEFAULT_SESSIONS.get(mkt, [])

        tz_name = self._safe_timezone(
            str(rows[0].get("timezone") or "") if rows else None,
            mkt,
        )
        tz = ZoneInfo(tz_name)
        windows: list[SessionWindow] = []
        for row in rows:
            s_name = str(row.get("session_name") or "").strip().upper()
            if not s_name:
                continue
            start_t = self._parse_time_value(row.get("start_time", "09:30:00"))
            end_t = self._parse_time_value(row.get("end_time", "15:00:00"))
            cross_day = bool(row.get("cross_day", False))
            start_at = datetime.combine(d, start_t, tzinfo=tz)
            end_at = datetime.combine(d, end_t, tzinfo=tz)
            if cross_day or end_at <= start_at:
                end_at += timedelta(days=1)
            windows.append(
                SessionWindow(
                    session_name=s_name,
                    start_at=start_at,
                    end_at=end_at,
                    cross_day=bool(cross_day),
                )
            )
        windows.sort(key=lambda x: x.start_at)
        return windows

    async def is_trading_time(
        self,
        *,
        market: str,
        dt: datetime | None,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        mkt = self._normalize_market(market)
        try:
            market_def = get_market(mkt)
            market_tz = ZoneInfo(market_def.timezone)
        except ValueError:
            market_tz = ZoneInfo(_DEFAULT_MARKET_TZ.get(mkt, "Asia/Shanghai"))

        now_dt = dt or datetime.now(market_tz)
        local_dt = now_dt.astimezone(market_tz)
        local_date = local_dt.date()
        if not await self.is_trading_day(
            market=mkt,
            trade_date=local_date,
            tenant_id=tenant_id,
            user_id=user_id,
        ):
            return {
                "is_trading_time": False,
                "matched_session": None,
                "timezone": getattr(local_dt.tzinfo, "key", str(local_dt.tzinfo)),
            }
        sessions = await self.get_sessions(
            market=mkt,
            trade_date=local_date,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        for window in sessions:
            if window.start_at <= local_dt <= window.end_at:
                return {
                    "is_trading_time": True,
                    "matched_session": window.session_name,
                    "timezone": getattr(local_dt.tzinfo, "key", str(local_dt.tzinfo)),
                }
        return {
            "is_trading_time": False,
            "matched_session": None,
            "timezone": getattr(local_dt.tzinfo, "key", str(local_dt.tzinfo)),
        }

    async def batch_is_trading_day(
        self,
        *,
        market: str,
        dates: list[date | datetime | str],
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw in dates:
            d = self._normalize_trade_date(raw)
            is_td = await self.is_trading_day(
                market=market,
                trade_date=d,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            result.append({"date": d.isoformat(), "is_trading_day": is_td})
        return result

    def get_calendar_for_market(self, market: str):
        """获取 exchange_calendars 的 Calendar 对象，供外部直接使用"""
        mkt = self._normalize_market(market)
        cal = _get_xcal_calendar(mkt)
        if cal is None:
            raise RuntimeError(f"无法获取市场 {market} 的日历")
        return cal

    async def _find_sessions(
        self,
        *,
        market: str,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        tenant, user = self._normalize_scope(tenant_id, user_id)
        async with get_session(read_only=True) as session:
            try:
                rows = await session.execute(
                    text(
                        """
                        WITH ranked AS (
                            SELECT
                                session_name,
                                start_time,
                                end_time,
                                cross_day,
                                trade_date_rule,
                                timezone,
                                tenant_id,
                                user_id,
                                CASE
                                    WHEN tenant_id = :tenant_1 AND user_id = :user_1 THEN 1
                                    WHEN tenant_id = :tenant_2 AND user_id = :user_2 THEN 2
                                    WHEN tenant_id = :tenant_3 AND user_id = :user_3 THEN 3
                                    ELSE 99
                                END AS rank_no
                            FROM qm_market_trading_session
                            WHERE market = :market
                              AND (
                                (tenant_id = :tenant_1 AND user_id = :user_1)
                                OR (tenant_id = :tenant_2 AND user_id = :user_2)
                                OR (tenant_id = :tenant_3 AND user_id = :user_3)
                              )
                        ),
                        best_scope AS (
                            SELECT MIN(rank_no) AS best_rank
                            FROM ranked
                        )
                        SELECT
                            session_name,
                            start_time,
                            end_time,
                            cross_day,
                            trade_date_rule,
                            timezone,
                            tenant_id,
                            user_id
                        FROM ranked
                        WHERE rank_no = (SELECT best_rank FROM best_scope)
                        ORDER BY session_name ASC
                        """
                    ),
                    {
                        "market": market,
                        "tenant_1": tenant,
                        "user_1": user,
                        "tenant_2": tenant,
                        "user_2": "*",
                        "tenant_3": "default",
                        "user_3": "*",
                    },
                )
                return [dict(r) for r in rows.mappings().all()]
            except Exception as exc:
                logger.warning("query qm_market_trading_session failed: %s", exc)
                return []

    async def upsert_calendar_day(
        self,
        *,
        market: str,
        trade_date: date | datetime | str,
        is_trading_day: bool,
        tenant_id: str,
        user_id: str,
        timezone_name: str | None = None,
        source: str = "manual",
        version: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> None:
        tenant, user = self._normalize_scope(tenant_id, user_id)
        mkt = self._normalize_market(market)
        d = self._normalize_trade_date(trade_date)
        tz = self._safe_timezone(timezone_name, mkt)
        payload = metadata_json or {}
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_market_calendar_day (
                        market, trade_date, is_trading_day, timezone, source, version,
                        tenant_id, user_id, metadata_json, updated_at
                    ) VALUES (
                        :market, :trade_date, :is_trading_day, :timezone, :source, :version,
                        :tenant_id, :user_id, CAST(:metadata_json AS JSONB), NOW()
                    )
                    ON CONFLICT (market, trade_date, tenant_id, user_id)
                    DO UPDATE SET
                        is_trading_day = EXCLUDED.is_trading_day,
                        timezone = EXCLUDED.timezone,
                        source = EXCLUDED.source,
                        version = EXCLUDED.version,
                        metadata_json = EXCLUDED.metadata_json,
                        updated_at = NOW()
                    """
                ),
                {
                    "market": mkt,
                    "trade_date": d,
                    "is_trading_day": bool(is_trading_day),
                    "timezone": tz,
                    "source": str(source or "manual"),
                    "version": version,
                    "tenant_id": tenant,
                    "user_id": user,
                    "metadata_json": json.dumps(payload, ensure_ascii=False),
                },
            )

    async def upsert_trading_sessions(
        self,
        *,
        market: str,
        sessions: list[dict[str, Any]],
        tenant_id: str,
        user_id: str,
        timezone_name: str | None = None,
    ) -> None:
        if not sessions:
            return
        tenant, user = self._normalize_scope(tenant_id, user_id)
        mkt = self._normalize_market(market)
        tz = self._safe_timezone(timezone_name, mkt)
        async with get_session() as session:
            for item in sessions:
                session_name = str(item.get("session_name") or "").strip().upper()
                if not session_name:
                    continue
                start_t = self._parse_time_value(item.get("start_time", "09:30:00"))
                end_t = self._parse_time_value(item.get("end_time", "15:00:00"))
                cross_day = bool(item.get("cross_day", False))
                rule = str(item.get("trade_date_rule") or "TRADE_DATE").strip().upper()
                meta = item.get("metadata_json") or {}
                await session.execute(
                    text(
                        """
                        INSERT INTO qm_market_trading_session (
                            market, session_name, start_time, end_time, cross_day, trade_date_rule,
                            timezone, tenant_id, user_id, metadata_json, updated_at
                        ) VALUES (
                            :market, :session_name, :start_time, :end_time, :cross_day, :trade_date_rule,
                            :timezone, :tenant_id, :user_id, CAST(:metadata_json AS JSONB), NOW()
                        )
                        ON CONFLICT (market, session_name, tenant_id, user_id)
                        DO UPDATE SET
                            start_time = EXCLUDED.start_time,
                            end_time = EXCLUDED.end_time,
                            cross_day = EXCLUDED.cross_day,
                            trade_date_rule = EXCLUDED.trade_date_rule,
                            timezone = EXCLUDED.timezone,
                            metadata_json = EXCLUDED.metadata_json,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "market": mkt,
                        "session_name": session_name,
                        "start_time": start_t,
                        "end_time": end_t,
                        "cross_day": cross_day,
                        "trade_date_rule": rule,
                        "timezone": tz,
                        "tenant_id": tenant,
                        "user_id": user,
                        "metadata_json": json.dumps(meta, ensure_ascii=False),
                    },
                )


calendar_service = TradingCalendarService()
