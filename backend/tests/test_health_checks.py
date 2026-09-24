"""T-P0-07 体检脚本单测：纯判定函数 + 假上下文驱动的代表性检查。

假上下文（FakeCtx）只实现 query / redis_get / redis_scan 三个注入点，
与 HealthContext 鸭子类型一致——检查函数因此可在不连 DB/Redis 下单测。
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from backend.scripts.diagnose.health import (
    CALENDAR_FAIL_DAYS,
    CALENDAR_WARN_DAYS,
    CHECKS,
    classify_account_key_forms,
    classify_agent_ledger_parity,
    classify_calendar_coverage,
    classify_cid_duplicates,
    classify_ledger_writes,
    classify_local_market_data,
    classify_signal_distribution,
    classify_snapshot_consistency,
    check_c01_signal_distribution,
    check_c02_signal_readiness,
    check_c03_account_key_consistency,
    check_c04_snapshot_consistency,
    check_c05_ledger_writes,
    check_c12_local_market_data,
    check_c13_trading_calendar_coverage,
    check_c14_agent_ledger_parity,
    exit_code,
    ledger_parity,
    summarize,
)


class FakeCtx:
    """注入式上下文假实现：按 SQL 子串给行，按 key 给值。"""

    def __init__(self, rows_by_sql=None, keys=None, values=None, today=None, market="CN"):
        self._rows = rows_by_sql or {}
        self._keys = keys or []
        self._values = values or {}
        self.today = today or date.today()
        # 与 HealthContext.market 同默认值：C01/C02/C08 按市场取数，
        # 鸭子类型必须把这个字段也补齐，否则检查函数读 ctx.market 直接炸。
        self.market = market

    def query(self, sql, **params):
        for needle, rows in self._rows.items():
            if needle in sql:
                return rows
        return []

    def redis_get(self, key, db):
        return self._values.get(key)

    def redis_scan(self, pattern, db):
        return list(self._keys)


# --- 纯判定 ---------------------------------------------------------------


def test_signal_distribution_empty_is_fail():
    assert classify_signal_distribution({}).level == "fail"


def test_signal_distribution_all_hold_is_fail():
    r = classify_signal_distribution({"HOLD": 5190})
    assert r.level == "fail"
    assert "全 HOLD" in r.detail
    assert "SIGNAL-GATE" in r.suggestion


def test_signal_distribution_collapse_is_warn():
    r = classify_signal_distribution({"BUY": 5000, "SELL": 100, "HOLD": 90})
    assert r.level == "warn"


def test_signal_distribution_normal_is_ok():
    r = classify_signal_distribution({"BUY": 1040, "SELL": 843, "HOLD": 3307})
    assert r.level == "ok"


def test_account_key_forms_single_ok_multi_fail():
    assert classify_account_key_forms({"1"}, "1").level == "ok"
    r = classify_account_key_forms({"1", "00000001"}, "1")
    assert r.level == "fail"
    assert "00000001" in r.detail


def test_snapshot_consistency_within_tolerance_ok():
    assert classify_snapshot_consistency(100.0, 100.5).level == "ok"


def test_snapshot_consistency_mismatch_fail():
    assert classify_snapshot_consistency(1_008_379.58, 2_008_379.58).level == "fail"


def test_ledger_writes_classification():
    assert classify_ledger_writes(0, 0).level == "ok"
    r = classify_ledger_writes(0, 3)
    assert r.level == "warn" and "T-P1-04" in r.suggestion
    assert classify_ledger_writes(5, 3).level == "ok"


def test_cid_duplicates_scan():
    """T-P2-06：幂等键重复扫描（无重复 ok；有重复 fail 且点名）。"""
    ok = classify_cid_duplicates([])
    assert ok.level == "ok" and ok.metrics == {"cid_dup_groups": 0}
    dup = classify_cid_duplicates(
        [
            {"tenant_id": "default", "user_id": 1, "client_order_id": "sim-r-600036.SH-buy"},
            {"tenant_id": "default", "user_id": 1, "client_order_id": "sim-r2-000001.SZ-sell"},
        ]
    )
    assert dup.level == "fail"
    assert "2 组" in dup.detail
    assert "sim_orders" in dup.detail  # 默认走模拟台账，消息与历史一致
    assert "sim-r-600036.SH-buy" in dup.detail


def test_cid_duplicates_scan_covers_real_orders_table():
    """P2.7-⑧：实盘台账（orders）同口径扫描 —— 重复既是幂等失守，也是迁移停手的原因。

    只测判定函数的 ``table`` 参数会让「C05 真的扫了 orders」漏掉，故连查询一起钉：
    ``check_c05`` 里必须出现 orders 的重复扫描 SQL。
    """
    dup = classify_cid_duplicates(
        [
            {
                "tenant_id": "default",
                "user_id": 10000001,
                "client_order_id": "lld-r-600036.SH-sell",
            }
        ],
        table="orders",
    )
    assert dup.level == "fail"
    assert dup.detail.startswith("orders 幂等键重复")

    import inspect

    from backend.scripts.diagnose import health

    src = inspect.getsource(health.check_c05_ledger_writes)
    assert "FROM orders " in src and "HAVING count(*) > 1" in src
    assert 'classify_cid_duplicates(real_dup_rows or [], table="orders")' in src


def test_summary_and_exit_code():
    results = [
        classify_signal_distribution({"BUY": 1, "SELL": 1}),
        classify_ledger_writes(0, 1),
        classify_signal_distribution({}),
    ]
    counts = summarize(results)
    assert counts == {"ok": 1, "warn": 1, "fail": 1}
    assert exit_code(results) == 1
    assert exit_code([classify_ledger_writes(1, 0)]) == 0


# --- 假上下文驱动的检查 -----------------------------------------------------


@pytest.mark.asyncio
async def test_c03_detects_dual_key_forms():
    ctx = FakeCtx(
        keys=[
            "simulation:account:default:1",
            "simulation:account:default:00000001",
            "simulation:settings:default:1",  # 非账户键应被忽略
        ]
    )
    r = await check_c03_account_key_consistency(ctx)
    assert r.level == "fail"


@pytest.mark.asyncio
async def test_c03_single_form_ok():
    ctx = FakeCtx(keys=["simulation:account:default:1", "simulation:account:default:1:FUTURES"])
    r = await check_c03_account_key_consistency(ctx)
    assert r.level == "ok"


@pytest.mark.asyncio
async def test_c04_multimarket_sum_matches_snapshot():
    """集成口径：CN + FUTURES 两账户求和应与用户级快照一致（修 +100 万后）。"""
    import json

    ctx = FakeCtx(
        keys=["simulation:account:default:1", "simulation:account:default:1:FUTURES"],
        values={
            "simulation:account:default:1": json.dumps({"total_asset": 1_008_379.58}),
            "simulation:account:default:1:FUTURES": json.dumps({"total_asset": 1_000_000.0}),
        },
        rows_by_sql={
            "simulation_fund_snapshots": [
                {"tenant_id": "default", "user_id": "1", "total_asset": 2_008_379.58}
            ]
        },
    )
    r = await check_c04_snapshot_consistency(ctx)
    assert r.level == "ok"


@pytest.mark.asyncio
async def test_c04_mismatch_detected():
    import json

    ctx = FakeCtx(
        keys=["simulation:account:default:1"],
        values={"simulation:account:default:1": json.dumps({"total_asset": 100.0})},
        rows_by_sql={
            "simulation_fund_snapshots": [
                {"tenant_id": "default", "user_id": "1", "total_asset": 999999.0}
            ]
        },
    )
    r = await check_c04_snapshot_consistency(ctx)
    assert r.level == "fail"


@pytest.mark.asyncio
async def test_c05_warns_when_ledger_empty_but_positions_exist():
    import json

    ctx = FakeCtx(
        keys=["simulation:account:default:1"],
        values={
            "simulation:account:default:1": json.dumps(
                {"positions": {"600036.SH": {"volume": 100}}}
            )
        },
        rows_by_sql={"sim_trades": [{"trades": 0}]},
    )
    r = await check_c05_ledger_writes(ctx)
    assert r.level == "warn"


# --- C12 本地行情数据 ------------------------------------------------------


def test_c12_all_markets_available_is_ok():
    r = classify_local_market_data({"CN": "2026-09-16", "HK": "2026-09-16"}, {})
    assert r.level == "ok"
    assert "2026-09-16" in r.detail


def test_c12_partial_missing_is_warn():
    r = classify_local_market_data(
        {"CN": "2026-09-16"}, {"HK": "HK 日线数据集目录不存在: /data/quanthk/..."}
    )
    assert r.level == "warn"
    assert "HK 日线数据集目录不存在" in r.detail
    assert "CN" in r.detail


def test_c12_nothing_available_is_fail():
    r = classify_local_market_data(
        {}, {"CN": "CN 日线数据集目录不存在: /data/quantdb/1_kline_data/daily_unadjusted"}
    )
    assert r.level == "fail"
    assert "模拟盘撮合" in r.suggestion


# --- C13 真日历覆盖年限 -----------------------------------------------------


def test_calendar_coverage_far_out_is_ok():
    """离覆盖截止还有半年 → ok。"""
    r = classify_calendar_coverage({"XSHG": (date(2027, 6, 30), "")}, date(2026, 1, 1))
    assert r.level == "ok"
    assert r.metrics["XSHG_remaining_days"] > CALENDAR_WARN_DAYS


def test_calendar_coverage_inside_warn_window():
    """落在告警窗内（不足 CALENDAR_WARN_DAYS）→ warn，且剩多少天要说清楚。"""
    today = date(2026, 10, 15)
    last = date(2026, 12, 31)  # 剩 77 天：> FAIL 阈值、< WARN 阈值
    r = classify_calendar_coverage({"XSHG": (last, "")}, today)
    assert r.level == "warn"
    assert r.metrics["XSHG_remaining_days"] == (last - today).days < CALENDAR_WARN_DAYS
    assert "2026-12-31" in r.detail


def test_calendar_coverage_inside_fail_window():
    """不足 CALENDAR_FAIL_DAYS → fail，建议里两条修法都要在（升级库 / 落 DB override）。"""
    r = classify_calendar_coverage(
        {"XSHG": (date(2026, 10, 10), "")}, date(2026, 10, 1)
    )
    assert r.level == "fail"
    assert r.metrics["XSHG_remaining_days"] < CALENDAR_FAIL_DAYS
    assert "exchange_calendars" in r.suggestion
    assert "qm_market_calendar_day" in r.suggestion
    # 建议必须**指名可执行的入口**：只写「把次年交易日写进表里」等于让运维自己
    # 去手写 SQL（生产者缺席的老问题换了个地方出现）
    assert "seed_trading_calendar.py" in r.suggestion


def test_calendar_coverage_already_expired_is_fail():
    """已经越过截止日 → fail（剩余为负也要算 fail，不能因为减法溢出成 ok）。

    同时是「没有 override」的反向对照：上面那条 DB override 用例若把它删掉，
    就可能只是闸门永远在报 ok。
    """
    r = classify_calendar_coverage({"XSHG": (date(2026, 12, 31), "")}, date(2027, 3, 1))
    assert r.level == "fail"
    assert r.metrics["XSHG_remaining_days"] < 0


def test_calendar_coverage_counts_db_override_horizon():
    """真日历过期、但 DB override 已补到次年 → **转绿**（否则「照建议修好了照样红」）。

    2027-01-01 之后 XSHG 必然过期；运维按 C13 的建议把次年交易日落进
    ``qm_market_calendar_day``（判定层里 override 优先级更高）。一个修不好的红灯
    很快就会被人忽略——那才是真正的危险。
    """
    r = classify_calendar_coverage(
        {"XSHG": (date(2026, 12, 31), "")}, date(2027, 3, 1), {"XSHG": date(2027, 12, 31)}
    )

    assert r.level == "ok", r.detail
    assert r.metrics["XSHG_effective_last_session"] == "2027-12-31"
    assert r.metrics["XSHG_override_last_session"] == "2027-12-31"
    assert "DB override" in r.detail
    assert "2026-12-31" in r.detail  # 真日历只到哪天也要说清楚（不藏信息）


def test_calendar_coverage_override_earlier_than_calendar_does_not_help():
    """override 比真日历还早时不顶用：有效覆盖取**较晚者**，不是「有 override 就 ok」。"""
    r = classify_calendar_coverage(
        {"XSHG": (date(2026, 12, 31), "")}, date(2026, 10, 15), {"XSHG": date(2026, 1, 15)}
    )

    assert r.level == "warn", r.detail  # 78 天 → 落在告警窗
    assert r.metrics["XSHG_effective_last_session"] == "2026-12-31"
    assert "DB override" not in r.detail


def test_calendar_coverage_unreadable_calendar_with_override_counts():
    """真日历整个取不到（如 importerror），但库里 override 覆盖到未来 → 不算告急。"""
    r = classify_calendar_coverage(
        {"XSHG": (None, "ImportError: no exchange_calendars")},
        date(2027, 3, 1),
        {"XSHG": date(2027, 12, 31)},
    )

    assert r.level == "ok", r.detail
    assert "ImportError" in r.detail  # 原因仍然如实带出


def test_calendar_coverage_worst_market_wins():
    """多历取最紧的那个：HK 还很远也不能把 CN 的告急盖成 ok。"""
    r = classify_calendar_coverage(
        {"XSHG": (date(2026, 10, 15), ""), "XHKG": (date(2027, 9, 24), "")},
        date(2026, 10, 1),
    )
    assert r.level == "fail"
    assert "XSHG" in r.detail and "XHKG" in r.detail


def test_calendar_coverage_unreadable_only_warns():
    """取不到日历只算 warn，但**原因必须带出来**（不许静默说 ok）。"""
    r = classify_calendar_coverage(
        {"XSHG": (None, "ImportError: no exchange_calendars")}, date(2026, 9, 24)
    )
    assert r.level == "warn"
    assert "ImportError" in r.detail
    assert r.metrics["XSHG_last_session"] is None


def test_c13_registered_in_checks():
    """C13 必须在 CHECKS 里，否则体检根本不会跑它（接线与判定要一起钉）。"""
    ids = [cid for cid, _name, _fn in CHECKS]
    assert "C13" in ids


@pytest.mark.asyncio
async def test_c13_live_calendar_still_covers_today():
    """**到期即红**的看门狗：容器内真日历今天还没过期。

    这不是「测库版本」，是钉住本批引入的运维期限：2026-12-31 XSHG 到期时
    这条会红，提醒先升级 exchange_calendars 或落 DB override——正是 C13 想让人
    在停摆**之前**看见的那件事。
    """
    from backend.shared.trading_calendar import TRADED_MARKET_XCALS, xcal_coverage

    coverage = xcal_coverage()
    assert set(coverage) == {name for _m, name in TRADED_MARKET_XCALS}
    r = await check_c13_trading_calendar_coverage(FakeCtx())
    assert r.level != "fail", r.detail
    assert r.metrics["XSHG_last_session"] is not None, r.detail


@pytest.mark.asyncio
async def test_c13_check_wires_db_override_into_the_verdict():
    """判定被接线到库上：有 override 行 → 转绿；没有 → 仍报红。

    两方向都钉。只看「有 override 时绿」的话，一个永远返回空 override 的接线
    （查询写错表名/列名）会与「没有 override」不可区分——正是静默失效的老形态。
    """
    from backend.shared.market_calendar_seed import market_keys

    assert "CN" in market_keys("CN")  # 键组口径来自播种器，不在体检里各写一份
    rows = [{"market": "CN", "last_day": date(2027, 12, 31)}]
    ctx = FakeCtx(rows_by_sql={"qm_market_calendar_day": rows}, today=date(2027, 3, 1))

    r = await check_c13_trading_calendar_coverage(ctx)
    assert r.level != "fail", r.detail
    assert r.metrics["XSHG_effective_last_session"] == "2027-12-31"

    empty = FakeCtx(today=date(2027, 3, 1))
    r_empty = await check_c13_trading_calendar_coverage(empty)
    assert r_empty.level == "fail", r_empty.detail


def test_c13_override_rows_are_normalised_across_driver_types():
    """``last_day`` 可能是 date / datetime / str：``datetime`` 与 ``date`` 比较会抛
    ``TypeError``（Python 3 的坑），所以必须先归一再比较；坏行跳过不抛。"""
    from backend.scripts.diagnose.health import _latest_override_by_market

    got = _latest_override_by_market(
        [
            {"market": "CN", "last_day": date(2027, 1, 5)},
            {"market": "cn", "last_day": "2027-06-30T00:00:00"},  # 小写 + 带时间戳的字符串
            {"market": "CN", "last_day": datetime(2027, 2, 1, 0, 0)},  # 更早的 datetime
            {"market": "SSE", "last_day": None},
            {"market": "", "last_day": date(2030, 1, 1)},
            {"market": "SZSE", "last_day": "不是日期"},
        ]
    )

    assert got == {"CN": date(2027, 6, 30)}


@pytest.mark.asyncio
async def test_c12_check_reports_missing_market_with_reason(monkeypatch):
    from datetime import date

    import backend.services.engine.data_platform.quantbc_hub as quantbc_hub
    import backend.services.simulation.services.local_market_data as local_market_data
    from backend.services.simulation.services.market_rules import Market

    class _FakeMarketData:
        def __init__(self, latest, reason=None):
            self._latest = latest
            self._reason = reason

        def latest_trade_date(self):
            return self._latest

        def data_unavailable_reason(self):
            return self._reason

    def _fake_get(market=None):
        if market is Market.CN:
            return _FakeMarketData(date(2026, 9, 16))
        return _FakeMarketData(None, f"{market.value} 日线数据集目录不存在: /data/x")

    monkeypatch.setattr(quantbc_hub, "_crypto_enabled", lambda: False)
    monkeypatch.setattr(local_market_data, "get_local_market_data", _fake_get)

    r = await check_c12_local_market_data(FakeCtx())

    assert r.level == "warn"
    assert "CN" in r.detail and "HK" in r.detail


# --- C14 分账账本一致性 ----------------------------------------------------

SYNTH = "qmt-synth-"
SEED = "legacy-seed:"


def _parity(trades, ledger, *, index=True):
    return classify_agent_ledger_parity(
        ledger_parity(
            trades,
            ledger,
            index_enabled=index,
            synth_prefix=SYNTH,
            seed_prefix=SEED,
        )
    )


def _t(key, agent="alpha", order="o1", user="1001"):
    return {
        "tenant_id": "default",
        "user_id": user,
        "order_id": order,
        "exchange_trade_id": key,
        "agent": agent,
    }


def _l(key, agent="alpha", order="o1", user="1001", applied=100.0):
    return {
        "tenant_id": "default",
        "user_id": user,
        "order_id": order,
        "fill_key": key,
        "agent": agent,
        "applied_volume": applied,
        "note": "",
    }


def test_c14_every_fill_posted_is_ok():
    r = _parity([_t("k1"), _t("k2")], [_l("k1"), _l("k2")])
    assert r.level == "ok"
    assert r.metrics["posted"] == 2
    assert r.metrics["trades"] == 2


def test_c14_partial_fills_pair_per_order():
    """同一张委托的两次部分成交：逐条配对，不按订单数一刀切。"""
    r = _parity([_t("k1"), _t("k2")], [_l("k1"), _l("k2"), _l("k3")])
    assert r.level == "warn"  # k3 无对应成交（孤儿）
    assert r.metrics["posted"] == 2


def test_c14_synth_upgrade_pairs_by_order():
    """合成成交升级改键：成交行是真实号、账本流水留在合成键上——**登记过的有界偏差**。

    认不认这个配对，决定这条检查是「每笔升级都报漏记」还是「只在真出问题时响」。
    """
    r = _parity([_t("T1")], [_l(f"{SYNTH}o1")])
    assert r.level == "ok"
    assert r.metrics["posted"] == 1


def test_c14_leftover_synth_row_is_double_post():
    """账本里合成键与真实键各一条、成交只有一条 ⇒ 同一笔成交记了两次（fail）。"""
    r = _parity([_t("T1")], [_l(f"{SYNTH}o1"), _l("T1")])
    assert r.level == "fail"
    assert r.metrics["dup_posts"] == 1
    assert r.metrics["posted"] == 1


def test_c14_missing_post_is_fail():
    r = _parity([_t("k1")], [])
    assert r.level == "fail"
    assert r.metrics["missing"] == 1
    assert "k1" in r.detail


def test_c14_agent_mismatch_is_fail():
    r = _parity([_t("k1", agent="beta")], [_l("k1", agent="alpha")])
    assert r.level == "fail"
    assert r.metrics["agent_mismatch"] == 1
    assert "beta" in r.detail and "alpha" in r.detail


def test_c14_synth_partner_still_checks_agent():
    """合成了、也升级了，但归属不符——不能因为配对成功就放过归属。"""
    r = _parity([_t("T1", agent="beta")], [_l(f"{SYNTH}o1", agent="alpha")])
    assert r.level == "fail"
    assert r.metrics["agent_mismatch"] == 1


def test_c14_seed_rows_are_counted_not_orphaned():
    """期初结转流水（P3 迁入时就有的仓）单独计数，不当「无对应成交」报。

    它们本来就没有本仓成交可配；混进 orphans 的话，切换后 30 天里这条检查常亮 warn，
    真异常来了反而看不出来。
    """
    seed = _l(f"{SEED}600036.SH", order="", applied=100.0)
    r = _parity([], [seed])
    assert r.level == "ok"
    assert r.metrics["seeds"] == 1
    assert r.metrics["orphans"] == 0
    assert "期初结转" in r.detail
    assert "未参与" in r.detail  # 结转不是「分账跑起来了」：窗口里仍无 LLM 腿成交


def test_c14_seed_rows_coexist_with_real_pairs():
    """结转行与真实成交行同窗口：真实配对照常判，结转只报条数。"""
    seed = _l(f"{SEED}600036.SH", order="", applied=100.0)
    r = _parity([_t("k1")], [_l("k1"), seed])
    assert r.level == "ok"
    assert r.metrics["posted"] == 1
    assert r.metrics["seeds"] == 1
    assert r.metrics["orphans"] == 0
    assert "期初结转 1 条" in r.detail


def test_c14_seed_rows_do_not_claim_trades():
    """结转行的 fill_key 不许被拿去顶一笔真实成交的配对（否则漏记会被瞒过去）。"""
    seed = _l(f"{SEED}600036.SH", order="o1", applied=100.0)
    r = _parity([_t("k1")], [seed])
    assert r.level == "fail"
    assert r.metrics["missing"] == 1
    assert r.metrics["seeds"] == 1


def test_c14_seed_rows_skip_the_unapplied_bucket():
    """结转行不进 unapplied（``applied_volume = 0`` 那是「记了未生效」的口径，
    结转要么整条在（``= volume``）要么压根不写，不存在「记了没生效」的中间态）。"""
    seed = _l(f"{SEED}600036.SH", order="", applied=0.0)
    r = _parity([], [seed])
    assert r.metrics["unapplied"] == 0
    assert r.metrics["seeds"] == 1


def test_c14_orphan_and_unapplied_warn_without_failing():
    r = _parity([_t("k1")], [_l("k1", applied=0.0), _l("zz9")])
    assert r.level == "warn"
    assert r.metrics["unapplied"] == 1
    assert r.metrics["orphans"] == 1


def test_c14_missing_unique_index_warns():
    r = _parity([_t("k1")], [_l("k1")], index=False)
    assert r.level == "warn"
    assert r.metrics["trade_unique_index"] is False
    assert "唯一键" in r.detail


def test_c14_zero_participation_says_so():
    """零参与必须**明说未参与**：这条检查只有分账跑起来之后才可能变红。"""
    r = _parity([], [])
    assert r.level == "ok"
    assert "未参与" in r.detail
    r2 = _parity([], [], index=False)
    assert r2.level == "warn"
    assert "未参与" in r2.detail


def test_c14_registered_in_checks():
    """C14 必须在 CHECKS 里，否则体检根本不跑它（接线与判定一起钉）。"""
    ids = [cid for cid, _name, _fn in CHECKS]
    assert "C14" in ids


@pytest.mark.asyncio
async def test_c14_check_flags_duplicate_fills_before_parity():
    """存量重复成交：唯一键建不起来、账本可能已双记——先报这个（fail）。"""
    ctx = FakeCtx(
        {
            "FROM trades WHERE exchange_trade_id IS NOT NULL": [
                {"tenant_id": "default", "user_id": "1001", "exchange_trade_id": "T9", "c": 2}
            ]
        }
    )
    r = await check_c14_agent_ledger_parity(ctx)
    assert r.level == "fail"
    assert r.metrics["dup_groups"] == 1
    assert "T9" in r.detail


@pytest.mark.asyncio
async def test_c14_check_reports_all_buckets_from_one_run():
    ctx = FakeCtx(
        {
            "FROM trades t JOIN orders o": [_t("k1"), _t("k2", agent="beta")],
            "FROM qm_agent_ledger_fill": [_l("k1"), _l("k2", agent="alpha")],
            "pg_indexes": [],
        }
    )
    r = await check_c14_agent_ledger_parity(ctx)
    assert r.level == "fail"
    assert r.metrics["agent_mismatch"] == 1
    assert r.metrics["trade_unique_index"] is False
    assert "唯一键" in r.detail  # 一次跑出全部偏差，不用连跑几次才看全


@pytest.mark.asyncio
async def test_c14_check_missing_ledger_table_without_llm_fills_is_ok():
    """模拟盘-only 部署：账本表没建、也没有 LLM 腿成交 → 如实说「未参与」，不报红。"""

    class _NoLedgerTable(FakeCtx):
        def query(self, sql, **params):
            if "qm_agent_ledger_fill" in sql:
                raise RuntimeError('relation "qm_agent_ledger_fill" does not exist')
            return super().query(sql, **params)

    r = await check_c14_agent_ledger_parity(_NoLedgerTable({"pg_indexes": []}))
    assert r.level == "ok"
    assert "未参与" in r.detail


@pytest.mark.asyncio
async def test_c14_check_missing_ledger_table_with_llm_fills_is_warn():
    """有 LLM 腿成交却查不到账本表：不能当「未参与」糊过去，要人看。"""

    class _NoLedgerTable(FakeCtx):
        def query(self, sql, **params):
            if "qm_agent_ledger_fill" in sql:
                raise RuntimeError('relation "qm_agent_ledger_fill" does not exist')
            return super().query(sql, **params)

    ctx = _NoLedgerTable({"FROM trades t JOIN orders o": [_t("k1")], "pg_indexes": []})
    r = await check_c14_agent_ledger_parity(ctx)
    assert r.level == "warn"
    assert r.metrics["trades"] == 1


@pytest.mark.asyncio
async def test_c14_check_wires_the_seed_prefix_from_the_writer_side():
    """结转行**经生产调用点**（不是测试自传前缀）必须落进 ``seeds``。

    上面几条 ``_parity(...)`` 用例自带前缀，证的是判定函数；这一条证的是
    **接线**——体检认的前缀一旦与写入侧（``seed_fill_key``）脱钩，结转行就会
    被当成「无对应成交」的孤儿，切换后头 30 天这条检查常亮 warn。
    """
    seed = _l("legacy-seed:600036.SH", order="", applied=100.0)
    assert seed["fill_key"].startswith(SEED), "常量与语料同源，防止各写一份"
    ctx = FakeCtx(
        {
            "FROM trades t JOIN orders o": [],
            "FROM qm_agent_ledger_fill": [seed],
            "pg_indexes": [{"present": 1}],
        }
    )
    r = await check_c14_agent_ledger_parity(ctx)
    assert r.level == "ok", r.detail
    assert r.metrics["seeds"] == 1
    assert r.metrics["orphans"] == 0


# --- C01 信号分布：按 source 分桶 -------------------------------------------


_C01_SQL_KEY = "GROUP BY source, signal_side"


@pytest.mark.asyncio
async def test_c01_batch_missing_with_realtime_rows_is_not_all_hold():
    """2026-09-24 实测假警报形态：当日只有实时行（signal_side 恒 NULL）。

    旧实现把 551 行实时行拼进分布，`str(None)` 落 "None" 桶 → 报「全 HOLD
    （551 行无 BUY/SELL）」——指向闸门坍缩，真相是批量推理没产出。
    """
    ctx = FakeCtx({_C01_SQL_KEY: [{"source": "realtime", "signal_side": None, "n": 551}]})
    r = await check_c01_signal_distribution(ctx)

    assert r.level == "fail"
    assert "批量信号缺失" in r.detail
    assert "全 HOLD" not in r.detail
    assert r.metrics["realtime_rows"] == 551


@pytest.mark.asyncio
async def test_c01_distribution_counts_batch_rows_only():
    """同一交易日批量行与实时行并存时，分布只认批量——实时行不进分母。"""
    ctx = FakeCtx(
        {
            _C01_SQL_KEY: [
                {"source": "batch", "signal_side": "BUY", "n": 1299},
                {"source": "batch", "signal_side": "HOLD", "n": 3924},
                {"source": "batch", "signal_side": "SELL", "n": 1271},
                {"source": "realtime", "signal_side": None, "n": 551},
            ]
        }
    )
    r = await check_c01_signal_distribution(ctx)

    assert r.level == "ok", r.detail
    assert r.metrics["total"] == 1299 + 3924 + 1271
    assert r.metrics["realtime_rows"] == 551


@pytest.mark.asyncio
async def test_c01_batch_rows_all_null_side_is_its_own_failure():
    """批量行在、signal_side 整列 NULL：是落库字段问题，不是「无数据」。"""
    ctx = FakeCtx({_C01_SQL_KEY: [{"source": "batch", "signal_side": None, "n": 42}]})
    r = await check_c01_signal_distribution(ctx)

    assert r.level == "fail"
    assert "signal_side 全为空" in r.detail
    assert r.metrics["unknown_side"] == 42


@pytest.mark.asyncio
async def test_c01_empty_market_says_so_without_realtime_noise():
    ctx = FakeCtx({})
    r = await check_c01_signal_distribution(ctx)

    assert r.level == "fail"
    assert "无任何信号数据" in r.detail
    assert "0 行实时行" not in r.detail


# --- C02 信号就绪：runs 计数按 source 收敛 -----------------------------------


_C02_RUNS_SQL_KEY = "count(DISTINCT run_id)"
_HEALTH_PY = Path(__file__).resolve().parents[1] / "scripts" / "diagnose" / "health.py"


@pytest.mark.asyncio
async def test_c02_batch_runs_at_threshold_is_ok_with_ready_marker():
    """2026-09-24 实测形态：2 批量 run + 就绪标记 → ok，不得报「同日多 run」。

    实时行每日每模型一个 rt- run_id（与批量重跑/竞态无关），旧计数不筛
    source 时 runs=3 顶过 >2 阈值，把常态判成周赛/回填异常。
    """
    from backend.shared.inference_lock import ready_key

    ctx = FakeCtx(
        {
            "max(trade_date)": [{"d": date(2026, 9, 24)}],
            _C02_RUNS_SQL_KEY: [{"n": 2}],
        },
        values={ready_key("CN", "2026-09-24"): '{"run_id": "run_x", "symbols": 3274}'},
    )
    r = await check_c02_signal_readiness(ctx)

    assert r.level == "ok", r.detail
    assert "同日多 run" not in r.detail
    assert r.metrics["runs"] == 2


def test_c02_runs_sql_filters_to_batch_source():
    """行为用例喂的是假行、不经过 SQL —— 过滤词必须另有源断言钉住，
    否则把子句删掉全部行为用例仍绿（假通过形态）。"""
    src = _HEALTH_PY.read_text(encoding="utf-8")
    assert "COALESCE(source, 'batch') = 'batch'" in src


@pytest.mark.asyncio
async def test_c02_completed_marker_is_fallback_when_ready_absent():
    """就绪键缺失（2026-09-24 前的历史全缺失）时，完成标记兜底 → ok。"""
    ctx = FakeCtx(
        {
            "max(trade_date)": [{"d": date(2026, 9, 24)}],
            _C02_RUNS_SQL_KEY: [{"n": 2}],
        },
        values={"qm:inference:completed:2026-09-24": "run_20260923_995286b3"},
    )
    r = await check_c02_signal_readiness(ctx)

    assert r.level == "ok", r.detail
    assert "回退读完成标记" in r.detail


@pytest.mark.asyncio
async def test_c02_no_markers_at_all_is_warn():
    ctx = FakeCtx(
        {
            "max(trade_date)": [{"d": date(2026, 9, 24)}],
            _C02_RUNS_SQL_KEY: [{"n": 1}],
        }
    )
    r = await check_c02_signal_readiness(ctx)

    assert r.level == "warn"
    assert "无就绪/完成标记" in r.detail


@pytest.mark.asyncio
async def test_c02_three_batch_runs_is_multi_run_warn():
    """阈值仍要能报：3 个批量 run 同日才是真异常（重跑/竞态）。"""
    from backend.shared.inference_lock import ready_key

    ctx = FakeCtx(
        {
            "max(trade_date)": [{"d": date(2026, 9, 24)}],
            _C02_RUNS_SQL_KEY: [{"n": 3}],
        },
        values={ready_key("CN", "2026-09-24"): '{"run_id": "run_x"}'},
    )
    r = await check_c02_signal_readiness(ctx)

    assert r.level == "warn"
    assert "同日多 run" in r.detail
