"""交易日历播种器（``market_calendar_seed`` 纯层 + ``seed_trading_calendar`` CLI）单测。

被守护的故障：2027-01-01 XSHG 硬期限
-------------------------------------
``exchange_calendars`` 4.13.2（PyPI 最新）的 XSHG 只印发到 **2026-12-31**，
越过那天 ``cal.is_session`` 抛 DateOutOfBounds → ``_is_trading_day_xcal`` 吞掉
返回 None → ``trading_day_verdict`` 退化成 ``SRC_WEEKDAY_FALLBACK``（只按周末判）
→ 决策轮**拒绝**降级依据（fail-closed）⇒ 不是乱下单，是**一轮决策都不出**：
静默停摆。而 2027 年的国务院节假日安排要到 2026 年底才印发，升级库也修不了。

``trading_day_verdict`` 的第一优先级本来就是 DB override（``SRC_DB_OVERRIDE``，
表 ``qm_market_calendar_day``）：表在、读路径在、优先级排序在——**唯独没有生产者**
（``TradingCalendarService.upsert_calendar_day`` 仓库内零调用点，同
``risk-rule-producer-absence`` 记忆「规则恒不触发＝生产者缺席」）。本套件守护的
就是把这个生产者补上，且补得可查、可拒、可回读：

1. **纯层**（不碰库）：节假日文件解析、全年行构造、sanity 闸门；
2. **真库**：越过覆盖年限的日期，播种前是降级依据、播种后是 DB override
   （**含反向对照**——不播种时它必须还不是 override，否则测试是空过的）；
3. **CLI**：默认只预演不写库、``--apply`` 才写、坏文件一律拒、``--verify`` 走
   真实读路径回读。

真库用例一律用 ``t-*`` 随机租户作用域并在 ``finally`` 里按**精确值**清库
（不用 LIKE：``_`` 是通配符，会误伤真租户——见集成测试污染真账的记忆）。

播种是**按组**的：判定层不做 market 别名归一，同一物理市场三个键并存（``CN``
决策轮 / ``SSE`` celery 自动推理 / ``SZSE`` 代码推断），只写一个键等于只补了三分
之一。因此真库用例既钉「整组都被写成权威判定」，也钉「回读是逐键的」。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.shared import market_calendar_seed as seed
from backend.shared.database_manager_v2 import get_session
from backend.shared.trading_calendar import (
    SRC_DB_OVERRIDE,
    SRC_WEEKDAY_FALLBACK,
    TradingCalendarService,
)

# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _scope() -> tuple[str, str]:
    """随机 ``t-*`` 租户作用域：与真库数据天然隔离，出事也只脏自己那几行。"""
    return f"t-seedcal-{uuid.uuid4().hex[:8]}", "u1"


async def _fresh_pool() -> None:
    """每次 ``asyncio.run`` 前刷池：asyncpg 连接被绑在**创建它的那个 loop** 上，
    前一个 ``asyncio.run`` 结束后池里的连接全绑在已关闭的 loop 上，直接复用必炸
    （``attached to a different loop`` / ``Event loop is closed``）——
    与 ``test_fill_quality._ensure_fresh_db_pool`` 同范式（该文件注明同源于
    ``test_hot_set_builder._ensure_db_pool``；本仓惯例是各测试文件自带一份，
    不做跨测试模块 import）。
    """
    from backend.shared.database_manager_v2 import close_database

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
        return
    except Exception:  # noqa: BLE001
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(text("SELECT 1"))


def _sync(coro_factory) -> object:
    """在自己的 loop 里先刷池再跑协程（见 ``_fresh_pool``）。"""

    async def _wrap():
        await _fresh_pool()
        return await coro_factory()

    return asyncio.run(_wrap())


def _purge_sync(tenant: str) -> None:
    _sync(lambda: _purge(tenant))


async def _purge(tenant: str) -> None:
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM qm_market_calendar_day WHERE tenant_id = :t"),
            {"t": tenant},
        )


async def _purge_market_key(tenant: str, market: str) -> None:
    """只删**某一个查询键**的行，保留同组其余键。

    用途是判别式测试：把非主键的那几个键删掉，若回读只看主键就会假通过
    （见 ``test_verify_checks_every_query_key_not_just_one``）。
    """
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM qm_market_calendar_day WHERE tenant_id = :t AND market = :m"),
            {"t": tenant, "m": market},
        )


async def _purge_test_version(day: date) -> None:
    """按**测试自己的标记**兜底清库：``version='test'`` 是本套件专用值，
    运维播种写的是 sha256 前 12 位，二者不会撞。

    为什么要有这一条：变异探针会故意把 ``apply_rows`` 的 tenant 改掉（P9），
    那一版代码把行写进全局兜底 ``('default','*')``，而按 tenant 的清库只删 t-*，
    于是**真库被探针污染**、后续用例因环境而红（本套件第一次跑探针时正是如此）。
    按标记清一次，让用例的结果只反映被测代码，不反映前一条探针的残留。
    """
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM qm_market_calendar_day WHERE trade_date = :d AND version = 'test'"),
            {"d": day},
        )


def _count_rows(tenant: str) -> int:
    async def _run() -> int:
        async with get_session(read_only=True) as session:
            row = await session.execute(
                text("SELECT COUNT(*) FROM qm_market_calendar_day WHERE tenant_id = :t"),
                {"t": tenant},
            )
            return int(row.scalar_one())

    return _sync(_run)


def _verdict(market: str, day: date, tenant: str, user: str) -> tuple[bool, str]:
    async def _run() -> tuple[bool, str]:
        return await TradingCalendarService().trading_day_verdict(
            market=market, trade_date=day, tenant_id=tenant, user_id=user
        )

    return _sync(_run)


def _first_day_beyond_library_coverage(market: str = "CN") -> date:
    """真日历覆盖的**次日**（往上取到第一个工作日）。

    不写死日期：库升级后这里自动跟着前移，测试不会因为「换了个库」而变成
    测一个已经落在覆盖范围内的日期（那样它就再也证明不了任何事）。
    """
    from backend.shared.trading_calendar import xcal_coverage

    last, reason = xcal_coverage()[seed.market_calendar_name(market)]
    assert last is not None, f"取不到真日历覆盖末日：{reason}"
    day = last + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


# --------------------------------------------------------------------------
# 1. 纯层：节假日文件解析
# --------------------------------------------------------------------------
class TestParseHolidays:
    def test_parses_dates_comments_and_blanks(self) -> None:
        parsed = seed.parse_holidays(
            """
            # 2027 年休市安排（国务院办公厅通知）
            2027-01-01          # 元旦
            2027-02-05

            2027-02-08  # 春节
            """
        )
        assert parsed.dates == frozenset(
            {date(2027, 1, 1), date(2027, 2, 5), date(2027, 2, 8)}
        )
        assert parsed.problems == ()

    def test_a_malformed_line_is_a_problem_not_a_silent_skip(self) -> None:
        """含混的输入必须**报出来**：静默跳过一行 = 少一天休市 = 那天照常开仓。"""
        parsed = seed.parse_holidays("2027-01-01\n2027/02/05\n二〇二七年二月八日\n")

        assert parsed.dates == frozenset({date(2027, 1, 1)})
        assert len(parsed.problems) == 2
        assert any("2027/02/05" in p for p in parsed.problems)
        assert any("二〇二七年二月八日" in p for p in parsed.problems)

    def test_a_duplicate_is_a_note_not_a_problem(self) -> None:
        parsed = seed.parse_holidays("2027-01-01\n2027-01-01\n")

        assert parsed.dates == frozenset({date(2027, 1, 1)})
        assert parsed.problems == ()
        assert any("重复" in n for n in parsed.notes)

    def test_an_empty_file_is_a_problem(self) -> None:
        parsed = seed.parse_holidays("# 只有注释\n")

        assert parsed.dates == frozenset()
        assert any("一条日期都没有" in p for p in parsed.problems)


# --------------------------------------------------------------------------
# 2. 纯层：全年行构造
# --------------------------------------------------------------------------
class TestBuildDayRows:
    HOLIDAYS = frozenset({date(2027, 1, 1), date(2027, 2, 5)})

    def test_every_day_of_the_year_is_present_and_ordered(self) -> None:
        rows = seed.build_day_rows(2027, self.HOLIDAYS)

        days = [r.trade_date for r in rows]
        assert len(days) == 365
        assert days == sorted(days)
        assert days[0] == date(2027, 1, 1)
        assert days[-1] == date(2027, 12, 31)
        assert len(set(days)) == 365

    def test_holiday_and_weekend_are_not_trading_days(self) -> None:
        by_day = {r.trade_date: r.is_trading_day for r in seed.build_day_rows(2027, self.HOLIDAYS)}

        assert by_day[date(2027, 1, 1)] is False  # 元旦（周五）
        assert by_day[date(2027, 2, 5)] is False  # 春节（周五）
        assert by_day[date(2027, 1, 4)] is True  # 周一
        # 2027-01-02 是周六：**交易所周末永不交易**（国务院的「调休补班」是企事业单位
        # 上班，股市不开市），所以即使不在休市清单里也必须是 False
        assert by_day[date(2027, 1, 2)] is False
        assert by_day[date(2027, 1, 3)] is False

    def test_leap_year_has_366_days(self) -> None:
        assert len(seed.build_day_rows(2028, frozenset())) == 366


# --------------------------------------------------------------------------
# 3. 纯层：sanity 闸门
# --------------------------------------------------------------------------
def _realistic_holidays(year: int) -> frozenset[date]:
    """一个「形状真实」的休市清单：全年约 20 个工作日休市，分布在各月。

    形状按 2015–2026 实测（SSE 每年 242–244 个交易日、约 19–21 个工作日休市）
    构造，不是为了贴合某一年，而是为了让 sanity 的**正常通过**分支也有覆盖。
    """
    days: set[date] = set()
    for month, count in (
        (1, 2),
        (2, 5),
        (4, 2),
        (5, 3),
        (6, 1),
        (9, 1),
        (10, 5),
        (12, 1),
    ):
        cursor = date(year, month, 1)
        added = 0
        while added < count:
            if cursor.weekday() < 5:
                days.add(cursor)
                added += 1
            cursor += timedelta(days=1)
    return frozenset(days)


class TestSanity:
    def test_a_realistic_year_passes(self) -> None:
        """正向对照：没有这条，后面每条「拒了」都可能只是闸门永远在拒。"""
        holidays = _realistic_holidays(2027)
        rows = seed.build_day_rows(2027, holidays)

        problems, notes = seed.sanity(year=2027, holidays=holidays, rows=rows)

        assert problems == [], problems
        assert notes == []

    def test_missing_a_whole_holiday_block_is_rejected(self) -> None:
        """抄漏一整个春节（5 个工作日）。

        注意这条**只能**由「工作日休市数」拦：抄漏春节那天数变成 241+5=246 个
        交易日，仍在 240–246 的区间内（见 ``market_calendar_seed`` 常量处的注释）。
        """
        holidays = _realistic_holidays(2027) - {
            d for d in _realistic_holidays(2027) if d.month == 2
        }
        rows = seed.build_day_rows(2027, holidays)

        problems, _ = seed.sanity(year=2027, holidays=holidays, rows=rows)

        assert any("工作日休市" in p for p in problems), problems

    def test_a_grossly_wrong_trading_day_count_is_rejected(self) -> None:
        """整体抄错（这里把 40 个工作日的额外休市塞进去）必须被交易日区间拦住。"""
        extra = [
            row.trade_date
            for row in seed.build_day_rows(2027, _realistic_holidays(2027))
            if row.is_trading_day
        ][:40]
        holidays = _realistic_holidays(2027) | set(extra)
        rows = seed.build_day_rows(2027, holidays)

        problems, _ = seed.sanity(year=2027, holidays=holidays, rows=rows)

        assert any("交易日" in p and "区间" in p for p in problems), problems

    def test_a_holiday_outside_the_year_is_rejected(self) -> None:
        holidays = _realistic_holidays(2027) | {date(2028, 1, 3)}
        rows = seed.build_day_rows(2027, holidays)

        problems, _ = seed.sanity(year=2027, holidays=holidays, rows=rows)

        assert any("2028-01-03" in p for p in problems), problems

    def test_a_holiday_on_a_weekend_is_only_a_note(self) -> None:
        """国务院通知常写「1 月 1 日至 3 日放假」，照抄会带上周末日期。

        它不影响判定（周末本来就不是交易日），所以是**提示**不是**拒绝**——
        否则照抄官方通知反而会被自己的闸门挡住。
        """
        holidays = _realistic_holidays(2027) | {date(2027, 1, 2)}
        rows = seed.build_day_rows(2027, holidays)

        problems, notes = seed.sanity(year=2027, holidays=holidays, rows=rows)

        assert problems == [], problems
        assert any("2027-01-02" in n and "周末" in n for n in notes), notes

    def test_a_month_without_a_single_trading_day_is_rejected(self) -> None:
        march_weekdays = {
            row.trade_date
            for row in seed.build_day_rows(2027, frozenset())
            if row.trade_date.month == 3 and row.is_trading_day
        }
        holidays = _realistic_holidays(2027) | march_weekdays
        rows = seed.build_day_rows(2027, holidays)

        problems, _ = seed.sanity(year=2027, holidays=holidays, rows=rows)

        assert any("2027-03" in p and "整月" in p for p in problems), problems

    def test_too_few_weekday_holidays_is_rejected(self) -> None:
        """几乎不休市 = 大概率抄漏了：A 股每年约 19–21 个工作日休市。"""
        rows = seed.build_day_rows(2027, frozenset())

        problems, _ = seed.sanity(year=2027, holidays=frozenset(), rows=rows)

        assert any("休市" in p for p in problems), problems


# --------------------------------------------------------------------------
# 4. 真库：播种把「降级判定」变成「权威判定」
# --------------------------------------------------------------------------
class TestSeededOverrideWins:
    """整段跑在**同一个** ``asyncio.run`` 里：跨 loop 复用 asyncpg 池必炸（见 ``_fresh_pool``），
    而且清库放在同一个 loop 的 ``finally`` 里，断言失败也不会留脏行。"""

    def test_out_of_coverage_day_becomes_an_authoritative_override(self) -> None:
        market = "CN"
        tenant, user = _scope()
        day = _first_day_beyond_library_coverage(market)

        async def _main() -> None:
            await _fresh_pool()
            try:
                # 反向对照：还没播种时，它必须是**降级依据**（否则这条用例证不了任何事）
                before = await TradingCalendarService().trading_day_verdict(
                    market=market, trade_date=day, tenant_id=tenant, user_id=user
                )
                assert before == (True, SRC_WEEKDAY_FALLBACK), before

                seeded = await seed.apply_rows(
                    market=market,
                    rows=[seed.DayRow(trade_date=day, is_trading_day=False)],
                    tenant_id=tenant,
                    user_id=user,
                    source=seed.SEED_SOURCE,
                    version="test",
                )
                # 行数 × 键数：一次播种要覆盖整组查询键（CN/SSE/SZSE），不是一行
                assert seeded == len(seed.market_keys(market))

                after = await TradingCalendarService().trading_day_verdict(
                    market=market, trade_date=day, tenant_id=tenant, user_id=user
                )
                assert after == (False, SRC_DB_OVERRIDE), after
            finally:
                await _purge(tenant)

        asyncio.run(_main())

    def test_seeding_cn_also_covers_the_sse_and_szse_keys(self) -> None:
        """播种 ``CN`` 必须一并写 ``SSE`` / ``SZSE``：同一物理市场，三个查询键。

        ``TradingCalendarService._normalize_market`` 只做大写 + 从股票代码推断，
        **没有别名归一**，于是同一市场在三条链路上各查各的键：决策轮查 ``CN``
        （``decision_round_io._is_trading_day``，真钱 fail-closed 闸门）、celery
        自动推理查 ``SSE``（``tasks/celery_tasks.py``）、代码推断查 ``SZSE``
        （``resolve_market_from_symbol('000001.SZ')``）。只播一个键，另外两条链路
        照样退化成周末兜底——春节照跑自动推理，而这条缺口在界面上完全不可见。

        反向对照逐键做：播种**前**每个键都必须是降级依据（否则这条用例可能只是
        吃了库里别的残留行）。
        """
        day = _first_day_beyond_library_coverage("CN")
        tenant, user = _scope()
        keys = seed.market_keys("CN")
        assert {"CN", "SSE", "SZSE"} <= set(keys), keys

        async def _main() -> None:
            await _fresh_pool()
            try:
                await _purge_test_version(day)
                service = TradingCalendarService()
                for key in keys:
                    before = await service.trading_day_verdict(
                        market=key, trade_date=day, tenant_id=tenant, user_id=user
                    )
                    assert before == (True, SRC_WEEKDAY_FALLBACK), (key, before)

                await seed.apply_rows(
                    market="CN",
                    rows=[seed.DayRow(trade_date=day, is_trading_day=False)],
                    tenant_id=tenant,
                    user_id=user,
                    source=seed.SEED_SOURCE,
                    version="test",
                )

                for key in keys:
                    after = await service.trading_day_verdict(
                        market=key, trade_date=day, tenant_id=tenant, user_id=user
                    )
                    assert after == (False, SRC_DB_OVERRIDE), (key, after)
            finally:
                await _purge(tenant)
                await _purge_test_version(day)

        asyncio.run(_main())

    def test_the_override_does_not_leak_to_other_scopes(self) -> None:
        """播种到 (t-x,u1) 不能顺带改掉别的租户/别的用户的判定。

        传播面完全由「写哪个作用域」决定：写 ``('default','*')`` 才全局生效
        （CLI 默认，因为它在任何作用域列表里排最后）；写具体租户就只对该租户生效。
        这条钉住后者 —— 否则给一个客户补日历会把**所有**租户的判定一起改掉。
        """
        market = "CN"
        tenant_a, user_a = _scope()
        tenant_b, user_b = _scope()
        day = _first_day_beyond_library_coverage(market)

        async def _main() -> None:
            await _fresh_pool()
            try:
                await _purge_test_version(day)  # 清掉可能的探针残留（见该函数说明）
                await seed.apply_rows(
                    market=market,
                    rows=[seed.DayRow(trade_date=day, is_trading_day=False)],
                    tenant_id=tenant_a,
                    user_id=user_a,
                    source=seed.SEED_SOURCE,
                    version="test",
                )
                service = TradingCalendarService()
                for tenant, user in ((tenant_a, "u2"), (tenant_b, user_b)):
                    verdict = await service.trading_day_verdict(
                        market=market, trade_date=day, tenant_id=tenant, user_id=user
                    )
                    assert verdict == (True, SRC_WEEKDAY_FALLBACK), (tenant, user, verdict)
            finally:
                await _purge(tenant_a)
                await _purge_test_version(day)

        asyncio.run(_main())

    def test_reseeding_the_same_day_is_idempotent(self) -> None:
        market = "CN"
        tenant, user = _scope()
        day = _first_day_beyond_library_coverage(market)
        rows = [seed.DayRow(trade_date=day, is_trading_day=False)]

        keys = len(seed.market_keys(market))

        async def _main() -> None:
            await _fresh_pool()
            try:
                for version in ("v1", "v2"):
                    await seed.apply_rows(
                        market=market,
                        rows=rows,
                        tenant_id=tenant,
                        user_id=user,
                        source=seed.SEED_SOURCE,
                        version=version,
                    )
                    async with get_session(read_only=True) as session:
                        row = await session.execute(
                            text("SELECT COUNT(*) FROM qm_market_calendar_day WHERE tenant_id = :t"),
                            {"t": tenant},
                        )
                        # 每次播种后都是「1 天 × 键数组」行：第二次没多写一行（upsert 生效）
                        assert int(row.scalar_one()) == keys
                async with get_session(read_only=True) as session:
                    version_row = await session.execute(
                        text(
                            "SELECT DISTINCT version FROM qm_market_calendar_day "
                            "WHERE tenant_id = :t"
                        ),
                        {"t": tenant},
                    )
                    assert [r.version for r in version_row] == ["v2"], "version 应被刷新成最后一次"
            finally:
                await _purge(tenant)

        asyncio.run(_main())


# --------------------------------------------------------------------------
# 5. 体检（C13）看到的是真覆盖，不是测试残留
# --------------------------------------------------------------------------
class TestHealthCountsOverrides:
    """C13 必须把 DB override 算进「覆盖到哪天」——否则运维照建议补完年后，
    体检会**继续报红**（真日历 2027-01-01 必然过期），一个修不好的红灯很快
    就会被人忽略。反过来，它**不能**认测试租户写的行：那会让体检假绿。

    这条是**真库 + 真上下文**（``_build_context``，与体检 CLI 同一条查询路径），
    因为过滤条件只在 SQL 里生效，假上下文看不出来。
    """

    def test_c13_sees_the_override_horizon_and_ignores_test_tenants(self) -> None:
        from backend.scripts.diagnose.health import (
            _build_context,
            check_c13_trading_calendar_coverage,
        )
        from backend.shared.trading_calendar import xcal_coverage

        tenant, user = _scope()
        day = date(2031, 1, 2)  # 远超真日历覆盖：任何把它算进去的读法都会立刻「转绿」
        real_last = xcal_coverage()[seed.market_calendar_name("CN")][0]
        assert real_last is not None

        async def _main() -> None:
            await _fresh_pool()
            try:
                await seed.apply_rows(
                    market="CN",
                    rows=[seed.DayRow(trade_date=day, is_trading_day=True)],
                    tenant_id=tenant,
                    user_id=user,
                    source=seed.SEED_SOURCE,
                    version="test",
                )
                ctx = _build_context("CN")
                # 正向对照：行**确实在库里**（不做测试卫生过滤就能看见）——
                # 没有这一步，「没被算进去」也可能只是因为压根没写进去。
                raw = ctx.query(
                    "SELECT market, MAX(trade_date) AS last_day FROM qm_market_calendar_day "
                    "WHERE market = 'CN' GROUP BY market"
                )
                assert [r["last_day"] for r in raw] == [day], raw

                result = await check_c13_trading_calendar_coverage(ctx)
                assert "XSHG_override_last_session" not in result.metrics, result.metrics
                assert (
                    result.metrics["XSHG_effective_last_session"] == real_last.isoformat()
                ), result.metrics
            finally:
                await _purge(tenant)

        asyncio.run(_main())


# --------------------------------------------------------------------------
# 6. 对外同步端点：SQL 打得中真表
# --------------------------------------------------------------------------
class TestPublicSyncCalendarEndpoint:
    """``/api/v1/public/sync/calendar`` 的取数语句必须能打到**真表**上。

    这条守的是一次真实事故：该端点把列名写成 ``day``（实表是 ``trade_date``），
    于是每次调用都 500、**从未返回过一行**——而它那一整份加固测试只钉「匿名要
    401」，一个永远 500 的端点在那些用例下照样全绿。假库也测不出来（假库回固定
    行，不看 SQL）。所以这里真连库跑一次，并且做**反向对照**：同一个 SQL 把列名
    换回 ``day`` 必须真的抛错（否则这条用例证明不了它修的是什么）。
    """

    def test_endpoint_sql_runs_against_the_real_table(self) -> None:
        from backend.services.api.routers import public_sync
        from backend.shared.database_manager_v2 import close_database, get_session

        tenant, user = _scope()
        day = _first_day_beyond_library_coverage("CN")

        async def _main() -> tuple[dict, str]:
            await _fresh_pool()
            try:
                await seed.apply_rows(
                    market="CN",
                    rows=[seed.DayRow(trade_date=day, is_trading_day=False)],
                    tenant_id=tenant,
                    user_id=user,
                    source=seed.SEED_SOURCE,
                    version="test",
                )
                # 逐参数显式传：FastAPI 的 Query(...) 默认值在**直调**时是个对象
                # （恒真），不传就会把 Query 实例当参数发给库——本仓踩过这个坑
                payload = await public_sync.sync_calendar(
                    start_date=day,
                    end_date=day,
                    tenant_id=tenant,
                    user_id=user,
                )
                # 反向对照：同一条语句把列名换回 day 必须炸（证明这条用例不是空过）
                from sqlalchemy import text

                broken = ""
                try:
                    async with get_session(read_only=True) as session:
                        await session.execute(
                            text(
                                "SELECT trade_date, market, is_trading_day FROM "
                                "qm_market_calendar_day WHERE day >= :d"
                            ),
                            {"d": day},
                        )
                except Exception as exc:  # noqa: BLE001 - 这里就是要它炸
                    broken = type(exc).__name__
                return payload, broken
            finally:
                await _purge(tenant)
                await close_database()

        payload, broken = asyncio.run(_main())

        rows = payload["data"]
        # 播种是按查询键组的 —— 同一天在 CN/SSE/SZSE 三个键下各一行
        assert len(rows) == len(seed.market_keys("CN")), rows
        assert [r["trade_date"] for r in rows] == [day] * len(rows), rows
        assert {r["market"] for r in rows} == set(seed.market_keys("CN")), rows
        assert all(r["is_trading_day"] is False for r in rows)
        assert set(rows[0]) == {
            "trade_date",
            "market",
            "is_trading_day",
            "source",
            "version",
        }, rows[0]
        assert broken, "把列名换回 day 竟然没炸——真表上真的存在 day 列？"


# --------------------------------------------------------------------------
# 7. CLI
# --------------------------------------------------------------------------
def _write_holidays(tmp_path: Path, year: int) -> Path:
    path = tmp_path / f"holidays-{year}.txt"
    lines = ["# 测试用休市清单"]
    lines += [d.isoformat() for d in sorted(_realistic_holidays(year))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestCli:
    @staticmethod
    def _run(*argv: str) -> int:
        from backend.scripts import seed_trading_calendar as cli

        return cli.main(list(argv))

    def test_preview_writes_nothing_and_apply_writes(self, tmp_path: Path) -> None:
        tenant, user = _scope()
        holidays = _write_holidays(tmp_path, 2027)
        base = [
            "--year",
            "2027",
            "--holidays-file",
            str(holidays),
            "--from",
            "2027-02-01",
            "--to",
            "2027-02-10",
            "--tenant",
            tenant,
            "--user",
            user,
        ]
        try:
            assert self._run(*base) == 0
            assert _count_rows(tenant) == 0, "预演动了库"

            assert self._run(*base, "--apply") == 0
            assert _count_rows(tenant) == 10 * len(seed.market_keys("CN"))
        finally:
            _purge_sync(tenant)

    def test_a_broken_holiday_file_is_refused_even_with_apply(self, tmp_path: Path) -> None:
        tenant, user = _scope()
        path = tmp_path / "bad.txt"
        path.write_text("2027-01-01\n# 只有这两天，明显抄漏了\n", encoding="utf-8")
        try:
            rc = self._run(
                "--year",
                "2027",
                "--holidays-file",
                str(path),
                "--apply",
                "--tenant",
                tenant,
                "--user",
                user,
            )

            assert rc == 1
            assert _count_rows(tenant) == 0, "sanity 没过却写了库"
        finally:
            _purge_sync(tenant)

    def test_a_missing_holiday_file_is_a_usage_error(self, tmp_path: Path) -> None:
        rc = self._run("--year", "2027", "--holidays-file", str(tmp_path / "nope.txt"))
        assert rc == 2

    def test_status_points_out_a_half_seeded_market_group(self) -> None:
        """``--status`` 必须点名「哪一组只播了一部分」。

        这是整组缺口**唯一**的可见面：三个键各查各的，界面上没有任何地方会显示
        「SSE 还是兜底判定」。没有这条提示，运维看到 ``CN 2027 365 天`` 就会以为
        补完了。两个方向都钉：半套要点名，整套要静默。
        """
        from backend.scripts import seed_trading_calendar as cli

        half = cli._incomplete_groups([{"market": "CN"}])

        assert any("SSE" in n and "SZSE" in n for n in half), half
        full = [{"market": k} for k in seed.market_keys("CN")]
        assert cli._incomplete_groups(full) == []
        # 不在任何已知组里的键（如库里遗留的 'A'）不该被当成「半套」噪声
        assert cli._incomplete_groups([{"market": "A"}]) == []

    def test_status_names_legacy_keys_outside_any_group(self) -> None:
        """库里冒出来的未知 market 键（``'A'`` 这类遗留痕迹）要点名。

        历史实例：9862 行 ``market='A'`` / ``source='baostock'``（含周末全判交易日），
        2026-09-24 已从库里清掉（备份与回滚脚本在
        ``/media/zbox/data/quantmind/backups_calendar_20260924/``）——正因为它**真的
        存在过**才是 fail-open 面：判定层不做别名归一，决策轮查 ``CN`` 看不到它，
        但任何显式查 ``'A'`` 的调用方会拿到「周六也是交易日」。满屏「A 2026 365 天」
        还容易被误读成「次年日历已补好」，所以必须说清它不是。
        同样两个方向都钉：遗留键要点名，已知键要静默。
        """
        from backend.scripts import seed_trading_calendar as cli

        notes = cli._unknown_market_keys([{"market": "A", "days": 365}])

        assert any("'A'" in n and "365" in n for n in notes), notes
        known = [{"market": k, "days": 1} for k in seed.market_keys("CN")]
        assert cli._unknown_market_keys(known) == []

    def test_modes_are_mutually_exclusive(self, tmp_path: Path) -> None:
        """含糊的调用要**拒**，不能悄悄挑一个跑（``--status --set ...`` 到底做哪件事？）。"""
        assert self._run("--status", "--set", "2027-07-01=0", "--reason", "x") == 2
        assert (
            self._run(
                "--status",
                "--year",
                "2027",
                "--holidays-file",
                str(_write_holidays(tmp_path, 2027)),
            )
            == 2
        )
        assert self._run("--set", "2027-07-01=0", "--reason", "x", "--year", "2027") == 2

    def test_the_default_range_is_the_gap_not_the_whole_year(self, tmp_path: Path) -> None:
        """不给 ``--from`` 时默认从**真日历覆盖不到的那天**开始。

        判别式测试：拿一个已被真日历覆盖的年份（覆盖末日那一年）来问，若默认起
        点是 ``date(year,1,1)``，就会把真日历**已经答对**的日子也覆盖成手动口径。
        """
        from backend.scripts import seed_trading_calendar as cli

        from backend.shared.trading_calendar import xcal_coverage

        last, reason = xcal_coverage()[seed.market_calendar_name("CN")]
        assert last is not None, f"取不到真日历覆盖末日：{reason}"

        start, end = cli._resolve_range(
            market="CN", year=last.year, date_from=None, date_to=None
        )

        assert start > last, f"默认起点 {start} 落在真日历覆盖内（末日 {last}）"
        assert end == date(last.year, 12, 31)
        assert start > end, "覆盖末日所在年份已无缺口，默认范围应为空"

    def test_a_year_without_a_gap_is_a_no_op(self, tmp_path: Path) -> None:
        from backend.scripts import seed_trading_calendar as cli

        from backend.shared.trading_calendar import xcal_coverage

        last, _ = xcal_coverage()[seed.market_calendar_name("CN")]
        tenant, user = _scope()
        holidays = _write_holidays(tmp_path, last.year)
        try:
            rc = self._run(
                "--year",
                str(last.year),
                "--holidays-file",
                str(holidays),
                "--apply",
                "--tenant",
                tenant,
                "--user",
                user,
            )

            assert rc == 0, "没有缺口不是错误"
            assert _count_rows(tenant) == 0, "没有缺口却写了库"
        finally:
            _purge_sync(tenant)

    def test_verify_reads_back_through_the_real_verdict_path(self, tmp_path: Path) -> None:
        tenant, user = _scope()
        holidays = _write_holidays(tmp_path, 2027)
        base = [
            "--year",
            "2027",
            "--holidays-file",
            str(holidays),
            "--from",
            "2027-02-01",
            "--to",
            "2027-02-10",
            "--tenant",
            tenant,
            "--user",
            user,
        ]
        try:
            assert self._run(*base, "--apply") == 0
            assert self._run(*base, "--verify") == 0

            # 反向对照：清掉再验，必须报不一致（否则 --verify 只是永远返回 0）
            _purge_sync(tenant)
            assert self._run(*base, "--verify") == 1
        finally:
            _purge_sync(tenant)

    def test_set_writes_one_day_and_needs_a_reason(self, tmp_path: Path) -> None:
        tenant, user = _scope()
        day = _first_day_beyond_library_coverage()
        try:
            assert (
                self._run(
                    "--set",
                    f"{day.isoformat()}=1",
                    "--apply",
                    "--tenant",
                    tenant,
                    "--user",
                    user,
                )
                == 2
            ), "--set 没有 --reason 必须当用法错误"

            assert (
                self._run(
                    "--set",
                    f"{day.isoformat()}=1",
                    "--reason",
                    "测试：临时交易日",
                    "--apply",
                    "--tenant",
                    tenant,
                    "--user",
                    user,
                )
                == 0
            )
            assert _verdict("CN", day, tenant, user) == (True, SRC_DB_OVERRIDE)
            # 单日覆盖同样是「整组查询键」，不是只有 CN
            assert _verdict("SSE", day, tenant, user) == (True, SRC_DB_OVERRIDE)
        finally:
            _purge_sync(tenant)

    def test_verify_checks_every_query_key_not_just_one(self, tmp_path: Path) -> None:
        """判别式：把 ``SSE`` / ``SZSE`` 的行删掉，只留 ``CN``，``--verify`` 必须报不一致。

        若回读只看主键，这条会**假通过**（0 表示一切正常），而真实世界里被漏掉的
        正是 celery 自动推理（``SSE``）与代码推断（``SZSE``）这两条链路。
        """
        tenant, user = _scope()
        holidays = _write_holidays(tmp_path, 2027)
        base = [
            "--year",
            "2027",
            "--holidays-file",
            str(holidays),
            "--from",
            "2027-02-01",
            "--to",
            "2027-02-10",
            "--tenant",
            tenant,
            "--user",
            user,
        ]
        try:
            assert self._run(*base, "--apply") == 0
            assert self._run(*base, "--verify") == 0

            for key in ("SSE", "SZSE"):
                _sync(lambda key=key: _purge_market_key(tenant, key))

            assert self._run(*base, "--verify") != 0, "少了 SSE/SZSE 的行却报通过"
        finally:
            _purge_sync(tenant)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
