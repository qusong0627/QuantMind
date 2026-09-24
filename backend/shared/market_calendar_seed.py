"""交易日历播种器：DB override（``qm_market_calendar_day``）的**生产者**。

被守护的故障
------------
``exchange_calendars`` 4.13.2（PyPI 最新）只印发到某个固定日期（容器内实测
XSHG → 2026-12-31，XHKG/XNYS → 2027-09-24）。越过那天 ``cal.is_session`` 抛
DateOutOfBounds → ``_is_trading_day_xcal`` 返回 None → ``trading_day_verdict``
退化成 ``SRC_WEEKDAY_FALLBACK``（只按周末判）→ 决策轮**拒绝**降级依据
（fail-closed）⇒ 不是乱下单，是**一轮决策都不出**：静默停摆。而 2027 年 A 股
节假日安排要等国务院年底才印发，升级库也修不了 —— 唯一能在期限前落地的修法
就是把次年交易日写进 DB override。体检 C13 一直在建议这条，但**没人能执行**：

``trading_day_verdict`` 的第一优先级就是 DB override，读路径、
``(tenant,user) > (tenant,'*') > ('default','*')`` 的作用域排序、upsert 语句
全都在 —— 唯独 ``TradingCalendarService.upsert_calendar_day`` 在仓库里
**零调用点**。层是死的，缺的是生产者（同 ``risk-rule-producer-absence`` 记忆：
规则恒不触发＝生产者缺席）。本模块补上它。

三条纪律
--------
1. **默认作用域是全局兜底** ``('default','*')``：它在任何 (tenant,user) 的作用域
   列表里都排最后，于是对**所有**租户生效，不需要给每个租户各写一份。
2. **纯层与 IO 层分开**：解析 / 构造 / sanity 全是不碰库的纯函数（可单测），
   ``apply_rows`` 是唯一写库点，且只走 ``TradingCalendarService.upsert_calendar_day``
   ——手写 SQL 会绕开 ``source`` / ``version`` / ``updated_at`` 口径。
3. **周末永不交易**：A 股交易所不跟随国务院的「调休补班」（企事业单位上班 ≠ 开市），
   故 ``is_trading_day = 工作日 and 不在休市清单``。照抄官方通知里「1 月 1 日至
   3 日放假」这类写法会把周末日期一并带进来，那只是冗余，不是错误（见 ``sanity``）。

sanity 闸门不是装饰：一份抄漏了春节的清单会让系统在春节里照常开仓，
一份多写了整个月的清单会让系统整月不出信号。两者都**拒绝落库**。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

#: 播种行的 ``source``（区别于手改的 ``manual`` 与未来其它来源）。
SEED_SOURCE = "holiday_seed"

#: ``--set`` 单日覆盖的 ``source``：语义就是「人工指定」，与表默认值同口径。
MANUAL_SOURCE = "manual"

#: 默认市场（全仓的 A 股口径；``signal_scores`` / 账户键 / qlib_paths 都用它）。
DEFAULT_MARKET = "CN"

#: 全局兜底作用域：任何 (tenant,user) 查 override 时都会退到这一档。
DEFAULT_TENANT = "default"
DEFAULT_USER = "*"

#: A 股全年交易日数的可接受区间。2015–2026 实测每年 **242–244**（SSE 日历），
#: 十二年只浮动 2 天，故只留 ±2 的余量。它拦得住「整体抄错」这类粗错，但**拦不住
#: 抄漏一个春节**（2027 年 261 个工作日 − 20 个休市 = 241；漏掉春节 5 天变 246，
#: 仍在区间内）——那一档由下面的「工作日休市数」拦。
TRADING_DAYS_MIN = 240
TRADING_DAYS_MAX = 246

#: 全年**工作日**休市天数区间。同期实测 17–19（261 个工作日 − 242~244 个交易日），
#: 留 ±3 余量。漏掉一个春节/国庆（5 个工作日）立刻跌到 15 以下 —— 这是**块级**
#: 漏抄的探测器，比交易日总数敏感。
WEEKDAY_HOLIDAYS_MIN = 16
WEEKDAY_HOLIDAYS_MAX = 24

#: 提示里最多列几个日期（避免把整份清单刷进一行）。
_MAX_LISTED = 5


@dataclass(frozen=True)
class HolidaySet:
    """一份休市清单的解析结果。

    ``problems`` 是**阻断**（落库前一律拒），``notes`` 只是提示 —— 二者分开是因为
    「周末日期冗余」这类情况来自照抄官方通知的惯常写法，拒了它等于拒了正确输入。
    """

    dates: frozenset[date]
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DayRow:
    trade_date: date
    is_trading_day: bool


#: 同一物理市场的**多个查询键**。判定层不做别名归一（``TradingCalendarService._normalize_market``
#: 只做大写 + 从股票代码推断），于是同一市场在仓库里多个键并存：
#:
#: * ``CN``   —— 决策轮（``decision_round_io._is_trading_day``，真钱 fail-closed 闸门）、
#:   决策轮停滞可见性（``decision_round_runner``）、前端市场维度；
#: * ``SSE``  —— celery 自动推理（``tasks/celery_tasks.py``）、数据状态扫描
#:   （``data_status_scanner._resolve_calendar_market`` 缺省即 SSE）；
#: * ``SZSE`` —— 由股票代码推断的路径（``resolve_market_from_symbol('000001.SZ')``）。
#:
#: **只播一个键，其余的照样降级**（例如自动推理在春节照跑）——所以播种按组进行。
#: 交易所节假日三家一致（SSE/SZSE 同规、XNYS/XNAS 同规），一份清单可以喂满一组。
MARKET_KEY_GROUPS: dict[str, tuple[str, ...]] = {
    "CN": ("CN", "SSE", "SZSE"),
    "HK": ("HK", "XHKG"),
    "US": ("US", "XNYS", "XNAS"),
}


def market_keys(market: str) -> tuple[str, ...]:
    """市场的查询键组（未知市场就它自己一个键）。"""
    want = str(market or "").strip().upper()
    return MARKET_KEY_GROUPS.get(want, (want,))


def market_calendar_name(market: str) -> str:
    """市场代码 → 真日历名（``CN`` → ``SSE``）；未知市场抛 ``ValueError``。

    不自己维护一张映射表：``TRADED_MARKET_XCALS`` 已是全仓口径（C13 体检、
    ``xcal_coverage`` 都读它），两处各写一份必然漂移。
    """
    from backend.shared.trading_calendar import TRADED_MARKET_XCALS

    want = str(market or "").strip().upper()
    for code, name in TRADED_MARKET_XCALS:
        if code.upper() == want:
            return name
    known = "、".join(code for code, _ in TRADED_MARKET_XCALS)
    raise ValueError(f"未知市场 {market!r}（可选：{known}）")


# --------------------------------------------------------------------------
# 纯层
# --------------------------------------------------------------------------
def parse_holidays(text: str) -> HolidaySet:
    """解析休市清单：每行一个 ``YYYY-MM-DD``，``#`` 起为注释，空行忽略。

    含混的行**报出来**而不是跳过 —— 静默跳过一行等于少一天休市，等于那天照常开仓。
    """
    dates: set[date] = set()
    problems: list[str] = []
    notes: list[str] = []
    duplicates: list[str] = []

    for lineno, raw in enumerate(str(text or "").splitlines(), start=1):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        try:
            day = date.fromisoformat(body)
        except ValueError:
            problems.append(f"第 {lineno} 行不是 YYYY-MM-DD 日期：{body!r}")
            continue
        if day in dates:
            duplicates.append(day.isoformat())
            continue
        dates.add(day)

    if not dates:
        problems.append("清单里一条日期都没有（只剩注释/空行）")
    if duplicates:
        notes.append(f"{len(duplicates)} 个重复日期（{'、'.join(sorted(duplicates))}），已去重")
    return HolidaySet(frozenset(dates), tuple(problems), tuple(notes))


def build_day_rows(year: int, holidays: Iterable[date]) -> list[DayRow]:
    """全年每一天的判定行：``工作日 and 不在休市清单``。"""
    closed = frozenset(holidays)
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    rows: list[DayRow] = []
    cursor = start
    while cursor <= end:
        rows.append(
            DayRow(
                trade_date=cursor,
                is_trading_day=cursor.weekday() < 5 and cursor not in closed,
            )
        )
        cursor += timedelta(days=1)
    return rows


def _list_days(days: Sequence[date]) -> str:
    shown = "、".join(d.isoformat() for d in days[:_MAX_LISTED])
    return f"{shown} 等 {len(days)} 天" if len(days) > _MAX_LISTED else shown


def sanity(
    *, year: int, holidays: Iterable[date], rows: Sequence[DayRow]
) -> tuple[list[str], list[str]]:
    """落库前的闸门：``(problems, notes)``。``problems`` 非空即**拒绝写入**。"""
    holidays = frozenset(holidays)
    problems: list[str] = []
    notes: list[str] = []

    outside = sorted(d for d in holidays if d.year != year)
    if outside:
        problems.append(f"休市日期不在 {year} 年内（抄错年份？）：{_list_days(outside)}")

    on_weekend = sorted(d for d in holidays if d.weekday() >= 5)
    if on_weekend:
        notes.append(
            f"休市清单里 {_list_days(on_weekend)} 本就落在周末"
            "（照抄官方通知的常见写法，不影响判定）"
        )

    trading_days = [r for r in rows if r.is_trading_day]
    if not (TRADING_DAYS_MIN <= len(trading_days) <= TRADING_DAYS_MAX):
        problems.append(
            f"{year} 年交易日 {len(trading_days)} 天，超出观测区间 "
            f"{TRADING_DAYS_MIN}–{TRADING_DAYS_MAX}（A 股 2015–2026 实测 242–244）"
            "——休市清单大概率抄漏或抄重"
        )

    weekday_holidays = [d for d in sorted(holidays) if d.weekday() < 5]
    if not (WEEKDAY_HOLIDAYS_MIN <= len(weekday_holidays) <= WEEKDAY_HOLIDAYS_MAX):
        problems.append(
            f"全年工作日休市 {len(weekday_holidays)} 天，超出区间 "
            f"{WEEKDAY_HOLIDAYS_MIN}–{WEEKDAY_HOLIDAYS_MAX}（同期实测约 18–20）"
            "——清单不完整或休市范围写错"
        )

    per_month: dict[int, int] = dict.fromkeys(range(1, 13), 0)
    for row in rows:
        if row.is_trading_day:
            per_month[row.trade_date.month] += 1
    for month, count in per_month.items():
        if count == 0:
            problems.append(f"{year}-{month:02d} 整月没有一个交易日——休市清单覆盖了整个月")

    return problems, notes


def summarise(rows: Sequence[DayRow]) -> dict[str, int]:
    """行数摘要（预演输出用）：总天数 / 交易日 / 休市（含周末）。"""
    trading = sum(1 for r in rows if r.is_trading_day)
    return {
        "days": len(rows),
        "trading_days": trading,
        "closed_days": len(rows) - trading,
    }


# --------------------------------------------------------------------------
# IO 层（唯一写库点）
# --------------------------------------------------------------------------
async def ensure_pool_usable() -> None:
    """探一下连接池；连接若被绑在**已关闭的 event loop** 上就重建它。

    asyncpg 的连接与其创建时的 loop 绑定，跨 ``asyncio.run`` 复用会抛
    ``attached to a different loop`` / ``Event loop is closed``。这类报错发生在
    **首次真正取连接时**（即写到一半），看起来像随机故障。CLI 进程通常不受影响，
    但凡是被 in-process 调用的场景（测试、被 import 的运维脚本）都会撞上。

    注意：探针失败**不区分**原因（也可能真的是库不通），此处只负责「换一条路
    再试」；真实的库故障会在随后的读写里照样抛出来，不在这里被吞掉。
    """
    from sqlalchemy import text as _sql_text

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_sql_text("SELECT 1"))
    except Exception:  # noqa: BLE001
        await close_database()


async def apply_rows(
    *,
    market: str,
    rows: Sequence[DayRow],
    tenant_id: str = DEFAULT_TENANT,
    user_id: str = DEFAULT_USER,
    source: str = SEED_SOURCE,
    version: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """把判定行 upsert 进 ``qm_market_calendar_day``，返回写入行数（``行数 × 键数``）。

    只走 ``TradingCalendarService.upsert_calendar_day``（全仓唯一写入口，且是
    ``ON CONFLICT`` upsert）：同一份清单重复播种是幂等的，``source`` / ``version``
    会被刷新成最新一次，便于日后回答「这一行是谁写的」。

    ``market`` 会展开成**整组查询键**（``market_keys``）：同一物理市场在决策轮、
    celery 自动推理、代码推断三条路径上用的是不同的键，只写一个键等于只补了三分
    之一——另外两条路径照样在节假日降级/误判。
    """
    from backend.shared.trading_calendar import TradingCalendarService

    await ensure_pool_usable()
    keys = market_keys(market)
    service = TradingCalendarService()
    for key in keys:
        for row in rows:
            await service.upsert_calendar_day(
                market=key,
                trade_date=row.trade_date,
                is_trading_day=row.is_trading_day,
                tenant_id=tenant_id,
                user_id=user_id,
                source=source,
                version=version,
                metadata_json=metadata,
            )
    return len(rows) * len(keys)
