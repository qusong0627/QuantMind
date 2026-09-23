"""风控引擎核心测试（T-RC-01）：每规则正/反/边界 + 引擎聚合 + 状态机 + fail-closed。

边界口径：阈值比较"等于放行、超过才拦"；缺失关键字段（资金/行情新鲜度）=拒（fail-closed）；
建议类缺失（行业占比）=WARN。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.shared.risk import RiskContext, RiskGateCore, all_rules, next_state
from backend.shared.risk.contracts import ACTION_HALT, ACTION_REJECT, ACTION_WARN
from backend.shared.risk.registry import RuleSpec

CST = timezone(timedelta(hours=8))
# 2026-09-16 是周三（交易日）
WED_10AM = datetime(2026, 9, 16, 10, 0, tzinfo=CST).timestamp()
SAT_10AM = datetime(2026, 9, 19, 10, 0, tzinfo=CST).timestamp()

FULL_CONFIG = {r.rule_id: dict(r.default_params) for r in all_rules()}


def _ctx(**over) -> RiskContext:
    base = {
        "market": "CN",
        "symbol": "600036.SH",
        "side": "BUY",
        "order_type": "LIMIT",
        "price": 40.0,
        "quantity": 100,
        "now_ts": WED_10AM,
        "available_cash": 1_000_000.0,
        "sellable_volume": 1000,
        "total_assets": 1_000_000.0,
        # 空仓基准：与 position_pct=0.0 同义（l1.leverage_cap 的分子）。
        # 缺快照的用例要显式传 None —— 那是该规则 fail-closed 的另一分支。
        "total_position_value": 0.0,
        # 账户快照时点：基准用例=刚更新（0.0）。缺时点/陈旧的用例要**显式**传
        # （None 或大数）——那是 l1.leverage_cap 的"陈旧告警"分支，不是默认值。
        "account_age_s": 0.0,
        "account_source": "sim",
        "position_pct": 0.0,
        "industry_pct": 0.05,
        "daily_pnl_pct": 0.0,
        "last_price": 40.0,
        "quote_age_s": 1.0,
        "orders_last_minute": 0,
        "orders_today": 0,
        "cancels_today": 0,
        # 当日已开仓标的：基准用例=今天还没开过（空集合）。**不能省**——FULL_CONFIG
        # 启用了每条注册规则，而基准用例都是 position_pct=0.0 的新开仓买单，
        # `l1.new_buys_per_day` 见到 opened_today=None（不可得）会 fail-closed 拒掉，
        # 于是整套用例的 passed 断言会被一条与用例意图无关的规则翻掉。
        # 要断言 fail-closed 分支的用例请**显式**传 None。
        "opened_today": (),
    }
    base.update(over)
    return RiskContext(**base)


def _verdict(**over):
    return RiskGateCore().evaluate(_ctx(**over), FULL_CONFIG, version=7)


# ── L0 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l0_kill_switch_halts():
    v = _verdict(kill_switch=True)
    assert not v.passed and v.halt
    assert v.decisions[0].action == ACTION_HALT and v.decisions[0].rule_id == "l0.kill_switch"


@pytest.mark.unit
@pytest.mark.parametrize(
    "ts,expect_pass",
    [
        (WED_10AM, True),
        (datetime(2026, 9, 16, 9, 20, tzinfo=CST).timestamp(), True),    # 集合竞价内（09:15 起）
        (datetime(2026, 9, 16, 11, 29, tzinfo=CST).timestamp(), True),
        (datetime(2026, 9, 16, 11, 30, tzinfo=CST).timestamp(), False),  # 边界：右开
        (datetime(2026, 9, 16, 12, 0, tzinfo=CST).timestamp(), False),   # 午休
        (datetime(2026, 9, 16, 15, 0, tzinfo=CST).timestamp(), False),   # 收盘右开
        (SAT_10AM, False),
    ],
)
def test_l0_session_boundaries(ts, expect_pass):
    v = _verdict(now_ts=ts)
    assert v.passed is expect_pass


@pytest.mark.unit
def test_l0_unknown_market_fail_closed():
    v = _verdict(market="XX")
    assert not v.passed
    assert any(d.rule_id == "l0.session" for d in v.rejects)


@pytest.mark.unit
def test_l0_clock_drift_boundary():
    assert _verdict(clock_skew_ms=500.0).passed          # 等于阈值放行
    assert not _verdict(clock_skew_ms=501.0).passed
    assert _verdict(clock_skew_ms=None).passed           # 未测量不拦


# ── L1 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l1_available_cash_and_missing_snapshot():
    assert _verdict(available_cash=4000.0).passed                       # 等于金额放行
    assert not _verdict(available_cash=3999.0).passed
    assert not _verdict(available_cash=None).passed                     # fail-closed
    assert _verdict(side="SELL", available_cash=None).passed            # SELL 不适用资金规则


@pytest.mark.unit
def test_l1_t1_sellable():
    assert _verdict(side="SELL", quantity=1000, sellable_volume=1000).passed
    assert not _verdict(side="SELL", quantity=1001, sellable_volume=1000).passed
    assert not _verdict(side="SELL", quantity=100, sellable_volume=None).passed


@pytest.mark.unit
def test_l1_position_cap_boundary():
    # 持仓 10% + 本单 5% = 15% 恰好触线放行；超一线即拒（注意 last_price 需对齐，免触 L3 偏离闸门）
    assert _verdict(total_assets=100_000.0, position_pct=0.10, price=50.0, quantity=100, last_price=50.0).passed
    assert not _verdict(total_assets=100_000.0, position_pct=0.10, price=50.5, quantity=100, last_price=50.0).passed
    assert not _verdict(total_assets=None).passed
    assert not _verdict(total_assets=0.0).passed


@pytest.mark.unit
def test_l1_leverage_cap_is_in_default_rules():
    # 不在默认配置里 = 引擎按"未启用"直接跳过 = 闸门形同不存在。
    # 这条断言是防"空转通过"：规则写得再对，配置缺席就等于没有。
    #
    # 断言对象必须是被**真正写进** `qm:risk:config.rules` 的那份 DEFAULT_RULES。
    # 用上面的 FULL_CONFIG 断言是恒真的：它由 all_rules() 构造，天然含每条注册规则，
    # 只能证明"规则注册了"，证明不了"默认配置启用了它"——两者之间正是闸门失效的地方。
    from backend.services.trade.services.risk_gate_service import DEFAULT_RULES

    assert "l1.leverage_cap" in DEFAULT_RULES
    assert DEFAULT_RULES["l1.leverage_cap"]["max_leverage"] == 1.0


@pytest.mark.unit
def test_l1_leverage_cap_boundary():
    # 权益 10 万、持仓 9.5 万 + 本单 0.5 万 = 恰好 100% → 放行（等阈值放行口径）
    assert _verdict(
        total_assets=100_000.0,
        total_position_value=95_000.0,
        price=50.0,
        quantity=100,
        last_price=50.0,
    ).passed
    # 多 1 元市值即越线 → 拒
    assert not _verdict(
        total_assets=100_000.0,
        total_position_value=95_001.0,
        price=50.0,
        quantity=100,
        last_price=50.0,
    ).passed


@pytest.mark.unit
def test_l1_leverage_cap_counts_this_order():
    # 分子含本单：同样的持仓下，买 100 股合法、买 200 股顶破阈值。
    # 若只判"当前已超"（遗留守护的口径），这两笔都会被放行，逐笔小单即可无限加杠杆。
    base = {
        "total_assets": 100_000.0,
        "total_position_value": 95_000.0,
        "last_price": 50.0,
        "price": 50.0,
    }
    assert _verdict(quantity=100, **base).passed
    assert not _verdict(quantity=200, **base).passed


@pytest.mark.unit
def test_l1_leverage_cap_blocks_only_buys():
    # 已超杠杆时卖单必须放行：拦卖会把账户锁死在高敞口里，与规则目的相反
    over = {"total_assets": 100_000.0, "total_position_value": 300_000.0}
    assert not _verdict(**over).passed
    assert _verdict(
        side="SELL", quantity=100, sellable_volume=1000, last_price=40.0, **over
    ).passed


@pytest.mark.unit
def test_l1_leverage_cap_fail_closed_on_missing_snapshot():
    # 持仓市值缺失 → 拒买。**不能**退化成"拿不到就当 0"——那等于在快照故障时
    # 把全场唯一的总敞口闸整个关掉，恰好在最需要它的时候失效。
    v = _verdict(total_position_value=None)
    assert [d.rule_id for d in v.rejects] == ["l1.leverage_cap"]
    # 权益同样不可得/非正时也拒（总资产为 0 会让杠杆比率失去意义）
    assert not _verdict(total_position_value=0.0, total_assets=None).passed
    assert not _verdict(total_position_value=0.0, total_assets=0.0).passed


@pytest.mark.unit
def test_l1_leverage_cap_param_override():
    # 默认 1.0 是收紧方向；融资场景须显式放宽参数
    hit = {
        "total_assets": 100_000.0,
        "total_position_value": 140_000.0,
        "price": 50.0,
        "quantity": 100,
        "last_price": 50.0,
    }
    assert not _verdict(**hit).passed
    cfg = {**FULL_CONFIG, "l1.leverage_cap": {"max_leverage": 1.5}}
    assert RiskGateCore().evaluate(_ctx(**hit), cfg).passed


@pytest.mark.unit
def test_l1_leverage_cap_rejection_is_explainable():
    # 每条拦截必须可解释（规则 ID + 计算依据 + 快照值）
    v = _verdict(
        total_assets=100_000.0,
        total_position_value=95_000.0,
        price=50.0,
        quantity=200,
        last_price=50.0,
    )
    d = next(x for x in v.rejects if x.rule_id == "l1.leverage_cap")
    assert d.level == "L1"
    assert d.evidence["projected_leverage"] == pytest.approx(1.05)
    assert d.evidence["pre_leverage"] == pytest.approx(0.95)
    assert d.evidence["max_leverage"] == 1.0
    assert d.evidence["position_value"] == 95_000.0
    assert d.evidence["order_amount"] == 10_000.0


@pytest.mark.unit
def test_l1_leverage_cap_rejection_carries_account_point():
    # 拦截证据要能回答"这个数字是哪座账户、什么时候的"——否则事后无法判断
    # 那次拦截是基于新鲜快照还是几小时前的旧账（两座真账户并存，来源必须可辨）。
    v = _verdict(
        total_assets=100_000.0,
        total_position_value=95_000.0,
        price=50.0,
        quantity=200,
        last_price=50.0,
        account_age_s=42.0,
        account_source="tdx_bridge",
    )
    d = next((x for x in v.rejects if x.rule_id == "l1.leverage_cap"), None)
    assert d is not None, "该单必须被 l1.leverage_cap 拒"
    assert d.evidence["account_age_s"] == pytest.approx(42.0)
    assert d.evidence["account_source"] == "tdx_bridge"


@pytest.mark.unit
def test_l1_leverage_cap_warns_on_stale_or_unknown_account_age():
    """陈旧/时点不可得的账户快照 → 放行 + WARN（数据可得性），**不拦单**。

    账户快照不是行情：持仓不交易就不变，晚一小时不等于不能用。拦在小额买单上
    会造成"闸门莫名拦单"，但那一点都不该是无声的——年龄进判定留痕。
    """
    base = {
        "total_assets": 100_000.0,
        "total_position_value": 10_000.0,
        "price": 50.0,
        "quantity": 100,
        "last_price": 50.0,
    }
    fresh = _verdict(**base)
    assert fresh.passed and not fresh.warns  # 新鲜：连告警都没有

    stale = _verdict(**base, account_age_s=7200.0, account_source="qmt_exec")
    assert stale.passed  # 放行
    d = next((x for x in stale.warns if x.rule_id == "l1.leverage_cap"), None)
    assert d is not None, "陈旧账户快照必须留一条 WARN"
    assert d.action == ACTION_WARN
    assert d.evidence["account_age_s"] == pytest.approx(7200.0)
    assert d.evidence["account_source"] == "qmt_exec"
    assert d.evidence["warn_after_s"] == pytest.approx(3600.0)

    # 时点不可得（None）同样告警——**不能**当成"刚更新"
    unknown = _verdict(**base, account_age_s=None)
    assert unknown.passed
    d2 = next((x for x in unknown.warns if x.rule_id == "l1.leverage_cap"), None)
    assert d2 is not None, "时点不可得必须留 WARN（否则等于谎报新鲜）"
    assert d2.evidence["account_age_s"] is None

    # 阈值可配（档位层/融资场景按需收紧）；显式 None = 关闭该告警
    cfg = {
        **FULL_CONFIG,
        "l1.leverage_cap": {"max_leverage": 1.0, "stale_warn_s": 10.0},
    }
    assert RiskGateCore().evaluate(_ctx(**base, account_age_s=60.0), cfg).warns
    cfg_off = {
        **FULL_CONFIG,
        "l1.leverage_cap": {"max_leverage": 1.0, "stale_warn_s": None},
    }
    v_off = RiskGateCore().evaluate(_ctx(**base, account_age_s=None), cfg_off)
    assert v_off.passed and not v_off.warns


@pytest.mark.unit
def test_l1_per_order_pct_is_in_default_rules():
    # 同 leverage_cap 的理由：注册 ≠ 启用。断言对象必须是真正写进配置的那份
    # DEFAULT_RULES（FULL_CONFIG 由 all_rules() 构造，断言它恒真、测不出缺席）。
    from backend.services.trade.services.risk_gate_service import DEFAULT_RULES

    assert "l1.per_order_pct" in DEFAULT_RULES
    assert DEFAULT_RULES["l1.per_order_pct"]["max_pct"] == 0.15


@pytest.mark.unit
def test_l1_per_order_pct_boundary():
    # 单笔 = 总资产的 15% 恰好触线放行（等阈值放行口径）；多一元即拒。
    # 权益 10 万、价格 150 → 100 股 = 1.5 万 = 15%
    at_limit = {
        "total_assets": 100_000.0,
        "price": 150.0,
        "last_price": 150.0,
        "quantity": 100,
    }
    assert _verdict(**at_limit).passed
    v = _verdict(**{**at_limit, "price": 150.1})
    # 默认值下两条规则同值 → 越线时**同时**触发（position_cap 的累计闸与单笔闸
    # 都见 15.01%）。这正是"默认 0.15 = 不额外收紧"的形态；分工见下一条用例。
    assert [d.rule_id for d in v.rejects] == ["l1.per_order_pct", "l1.position_cap"]
    # 快照缺失/非正 → 拒，**且理由必须是规则自己的那条**。只断"被拒"不够：引擎把
    # 规则抛出的异常也变成 REJECT（reason=规则执行异常），于是把 None 做成
    # "浮点除法炸掉"这种坏法照样能过断言。
    for bad in (None, 0.0):
        v_bad = _verdict(total_assets=bad)
        d_bad = next(x for x in v_bad.rejects if x.rule_id == "l1.per_order_pct")
        assert "总资产未知" in d_bad.reason


@pytest.mark.unit
def test_l1_per_order_pct_is_not_position_cap():
    """两条规则分工：position_cap 管**累计**占比（对已持仓标的只剩残量），
    per_order_pct 管**单笔**体量。把 position_cap 放宽到 50% 后，一笔 16% 的
    买入仍须被单笔闸拦下；反过来把 position_cap 收到 5%，6% 的单子也会被它先拦。

    没有这条时，"小步快跑"可以绕开累计闸——每笔都不到残量就永远撞不上线。
    """
    cfg_loose_pos = {**FULL_CONFIG, "l1.position_cap": {"max_pct": 0.50}}
    v = RiskGateCore().evaluate(
        _ctx(total_assets=100_000.0, price=32.0, last_price=32.0, quantity=500),
        cfg_loose_pos,
    )  # 16000 / 100000 = 16%
    assert [d.rule_id for d in v.rejects] == ["l1.per_order_pct"]

    cfg_tight_pos = {**FULL_CONFIG, "l1.position_cap": {"max_pct": 0.05}}
    v2 = RiskGateCore().evaluate(
        _ctx(total_assets=100_000.0, price=12.0, last_price=12.0, quantity=500),
        cfg_tight_pos,
    )  # 6000 / 100000 = 6%：单笔闸放行，累计闸拦下
    assert [d.rule_id for d in v2.rejects] == ["l1.position_cap"]


@pytest.mark.unit
def test_l1_per_order_pct_blocks_only_buys():
    # 卖出是降仓动作；大额卖出（如强平）被单笔买入闸拦下会把账户锁死在高敞口里
    over = {"total_assets": 100_000.0, "price": 150.0, "last_price": 150.0}
    assert not _verdict(quantity=200, **over).passed  # 买 30% → 拒
    assert _verdict(
        side="SELL", quantity=500, sellable_volume=1000, **over
    ).passed  # 75%


@pytest.mark.unit
def test_l1_per_order_pct_fail_closed_when_amount_unknown():
    # 金额不可得（无价且无额）→ 拒：与 available_cash 同一条纪律，拿不到就拦
    v = _verdict(price=None)
    d = next(x for x in v.rejects if x.rule_id == "l1.per_order_pct")
    assert "订单金额不可得" in d.reason


@pytest.mark.unit
def test_l1_per_order_pct_param_override():
    # 档位压到 0.10 时 12% 的单子必须被拦；默认 0.15 下同一笔放行
    hit = {
        "total_assets": 100_000.0,
        "price": 12.0,
        "last_price": 12.0,
        "quantity": 1000,
    }
    assert _verdict(**hit).passed  # 12000 = 12% ≤ 15%
    cfg = {**FULL_CONFIG, "l1.per_order_pct": {"max_pct": 0.10}}
    v = RiskGateCore().evaluate(_ctx(**hit), cfg)
    assert [d.rule_id for d in v.rejects] == ["l1.per_order_pct"]


@pytest.mark.unit
def test_l1_per_order_pct_rejection_is_explainable():
    v = _verdict(
        total_assets=100_000.0,
        price=150.0,
        last_price=150.0,
        quantity=200,
        account_age_s=42.0,
        account_source="tdx_bridge",
    )
    d = next((x for x in v.rejects if x.rule_id == "l1.per_order_pct"), None)
    assert d is not None, "该单必须被 l1.per_order_pct 拒"
    assert d.level == "L1"
    assert d.evidence["order_pct"] == pytest.approx(0.30)
    assert d.evidence["max_pct"] == pytest.approx(0.15)
    assert d.evidence["order_amount"] == pytest.approx(30_000.0)
    assert d.evidence["total_assets"] == pytest.approx(100_000.0)
    # 判定坐标：这个分母是哪座账户、什么时候的
    assert d.evidence["account_source"] == "tdx_bridge"
    assert d.evidence["account_age_s"] == pytest.approx(42.0)


# ── L1：当日新开仓上限（口径对齐隔壁 buy_gate.check_buy）────────────────


@pytest.mark.unit
def test_l1_new_buys_per_day_is_in_default_rules():
    from backend.services.trade.services.risk_gate_service import DEFAULT_RULES

    assert "l1.new_buys_per_day" in DEFAULT_RULES
    assert DEFAULT_RULES["l1.new_buys_per_day"]["max_new_buys"] == 3


@pytest.mark.unit
def test_l1_new_buys_per_day_boundary():
    # 已开 2 只 → 本单是第 3 只，恰好到线放行；已开 3 只 → 拒。
    # 注意这里的等号语义与别处相反：比较的是**已开数**不是本单序号，隔壁同款。
    # 已开集合刻意不含 600036（ctx.symbol 默认 600036.SH）——否则命中"同一标的
    # 重复买入"的豁免分支，测的就不是边界了。
    two = ("SH600519", "SH601318")
    assert _verdict(opened_today=two).passed
    v = _verdict(opened_today=(*two, "SH600000"))
    assert [d.rule_id for d in v.rejects] == ["l1.new_buys_per_day"]


@pytest.mark.unit
def test_l1_new_buys_per_day_same_symbol_does_not_consume_quota():
    """同一标的当日第二次买入不占新开仓额度——按**标的集合**去重而非笔数。

    代码口径按层分裂：ctx.symbol 是行情/后缀口径（600036.SH），DB 读回来的是
    前缀口径（SH600036）。没做归一时这里会退化成"同一标的算两次新开仓"。
    """
    three = ("SH600036", "SH600519", "SH601318")
    # ctx.symbol 默认 600036.SH（后缀）——命中已开仓集合 → 不占额度
    assert _verdict(opened_today=three).passed
    # 换成真·新标的即拒（600000.SH 不在已开集合里）
    v = _verdict(symbol="600000.SH", opened_today=three)
    assert [d.rule_id for d in v.rejects] == ["l1.new_buys_per_day"]


@pytest.mark.unit
def test_l1_new_buys_per_day_exempts_addon_and_sell():
    # 加仓不占额度：position_cap 管累计占比，本规则只管"往几个**新**标的开仓"。
    # 同样避开 600036，保证放行是两条豁免分支各自的功劳而非"同一标的"命中。
    full = ("SH600519", "SH601318", "SH600000")
    assert _verdict(position_pct=0.10, opened_today=full).passed
    # 卖出永远不看这条（清仓日不该被"今天已开 3 只"拦住）
    assert _verdict(
        side="SELL", quantity=100, sellable_volume=1000, opened_today=full
    ).passed


@pytest.mark.unit
def test_l1_new_buys_per_day_fail_closed_when_count_unavailable():
    """计数查询失败（None）→ 拒新开仓。按 0 处理等于当天无上限，恰好在最需要
    它的时候失效；加仓/卖出不受影响，账户不会被锁死。"""
    v = _verdict(opened_today=None)
    assert [d.rule_id for d in v.rejects] == ["l1.new_buys_per_day"]
    assert v.rejects[0].level == "L1"
    # 理由必须点名"不可得"：只断言"被拒"是不够的——引擎把规则抛出的异常也变成
    # REJECT，于是"None 路径"被改坏成迭代 None 抛错时，那条断言照样过。
    assert "不可得" in v.rejects[0].reason
    assert _verdict(
        side="SELL", quantity=100, sellable_volume=1000, opened_today=None
    ).passed


@pytest.mark.unit
def test_l1_new_buys_per_day_unknown_position_counts_as_new():
    # position_pct 不可得 → 按新开仓处理（收紧方向：宁可多算一次新开仓）
    v = _verdict(position_pct=None, opened_today=("SH600519", "SH601318", "SH600000"))
    assert [d.rule_id for d in v.rejects] == ["l1.new_buys_per_day"]


@pytest.mark.unit
def test_l1_new_buys_per_day_param_override():
    # 档位防守档 max_new_buys=1：已开 1 只即拦；改回 3 才放行
    hit = {"opened_today": ("SH600519",)}
    cfg = {**FULL_CONFIG, "l1.new_buys_per_day": {"max_new_buys": 1}}
    v = RiskGateCore().evaluate(_ctx(**hit), cfg)
    assert [d.rule_id for d in v.rejects] == ["l1.new_buys_per_day"]
    assert _verdict(**hit).passed  # 默认 3 下同一状态放行


@pytest.mark.unit
def test_l1_new_buys_per_day_rejection_lists_symbols():
    v = _verdict(
        opened_today=("SH600519", "SH601318", "SH600000", "000001.SZ"),
    )
    d = next((x for x in v.rejects if x.rule_id == "l1.new_buys_per_day"), None)
    assert d is not None
    assert d.evidence["opened_today"] == 4
    assert d.evidence["max_new_buys"] == 3
    # 证据里的代码统一成**前缀口径**且排序——两个口径混着列出来没法肉眼核对
    assert d.evidence["opened_symbols"] == [
        "SH600000",
        "SH600519",
        "SH601318",
        "SZ000001",
    ]


@pytest.mark.unit
def test_l1_industry_cap_warn_when_unknown():
    v = _verdict(industry_pct=None)
    assert v.passed and any(d.action == ACTION_WARN and d.rule_id == "l1.industry_cap" for d in v.warns)
    assert not _verdict(industry_pct=0.30, total_assets=100_000.0, price=40.0, quantity=100).passed  # 30%+4%


@pytest.mark.unit
def test_l1_daily_loss_limit():
    assert _verdict(daily_pnl_pct=-2.9).passed
    assert not _verdict(daily_pnl_pct=-3.0).passed    # 等于限额即停
    assert _verdict(daily_pnl_pct=None).passed


# ── L3 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l3_max_order_value_boundary():
    cfg = {**FULL_CONFIG, "l3.max_order_value": {"max_value": 4000.0}}
    assert RiskGateCore().evaluate(_ctx(), cfg).passed                    # 等于放行
    assert not RiskGateCore().evaluate(_ctx(quantity=101), cfg).passed


@pytest.mark.unit
def test_l3_price_deviation_and_forced_exit():
    assert _verdict(price=40.8).passed                                    # 2% 触线放行
    assert not _verdict(price=40.9).passed
    assert _verdict(price=44.0, forced_exit=True).passed                  # 强平 10% 在 sanity 内
    assert not _verdict(price=48.1, forced_exit=True).passed              # 20.25% 超 sanity


@pytest.mark.unit
def test_l3_stale_quote():
    assert _verdict(quote_age_s=5.0).passed
    assert not _verdict(quote_age_s=5.001).passed
    assert not _verdict(quote_age_s=None).passed


@pytest.mark.unit
def test_l3_frequency_and_cancel_ratio():
    assert not _verdict(orders_last_minute=20).passed
    assert _verdict(orders_last_minute=19).passed
    v = _verdict(orders_today=11, cancels_today=5)                        # 45% 撤单率 → WARN 不拒
    assert v.passed and any(d.rule_id == "l3.cancel_ratio" for d in v.warns)
    assert _verdict(orders_today=9, cancels_today=9).passed               # 样本不足跳过


@pytest.mark.unit
def test_l3_self_trade_and_duplicate():
    assert not _verdict(recent_symbol_sides=(("600036.SH", "SELL"),)).passed
    assert _verdict(recent_symbol_sides=(("600036.SH", "BUY"), ("000001.SZ", "SELL"))).passed
    assert not _verdict(fingerprint="fp1", recent_fingerprints=("fp1",)).passed
    assert _verdict(fingerprint="fp2", recent_fingerprints=("fp1",)).passed


@pytest.mark.unit
def test_l3_lot_size_boards():
    assert _verdict(quantity=200).passed
    assert not _verdict(quantity=150).passed
    assert _verdict(side="SELL", quantity=37, sellable_volume=100).passed  # 卖出允许零股
    assert _verdict(symbol="688981.SH", quantity=200).passed
    assert not _verdict(symbol="688981.SH", quantity=300).passed          # 科创板 200 股整手
    assert not _verdict(quantity=0).passed


# ── L6 ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_l6_book_and_contract():
    assert not _verdict(book_crossed=True).passed
    assert not _verdict(book_empty=True).passed
    assert not _verdict(contract_ok=False).passed


# ── 引擎语义 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_engine_opt_in_and_always_on():
    """最小配置：仅 always_on（急停/时段）执行；其余规则未配置即跳过。"""
    v = RiskGateCore().evaluate(_ctx(), {}, version=3)
    assert v.passed and v.config_version == 3
    assert set(v.checked_rules) == {"l0.kill_switch", "l0.session"}


@pytest.mark.unit
def test_engine_rule_exception_fail_closed():
    def _boom(ctx, params):
        raise RuntimeError("rule crashed")

    spec = RuleSpec(rule_id="l9.boom", level="L3", description="test", fn=_boom)
    v = RiskGateCore(specs=(spec,)).evaluate(_ctx(), {"l9.boom": {}})
    assert not v.passed
    assert v.rejects[0].rule_id == "l9.boom" and "fail-closed" in v.rejects[0].reason


@pytest.mark.unit
def test_engine_determinism_and_perf_smoke():
    core = RiskGateCore()
    ctx = _ctx()
    v1 = core.evaluate(ctx, FULL_CONFIG, version=7)
    v2 = core.evaluate(ctx, FULL_CONFIG, version=7)
    assert v1 == v2  # 同输入同裁决（dataclass 值等价）

    t0 = time.perf_counter()
    n = 3000
    for _ in range(n):
        core.evaluate(ctx, FULL_CONFIG, version=7)
    avg_ms = (time.perf_counter() - t0) / n * 1000
    assert avg_ms < 1.0, f"平均 {avg_ms:.3f}ms 超 1ms 预算"


# ── 状态机 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_state_machine_upgrade_immediate_downgrade_manual():
    assert next_state("NORMAL", caution=True) == ("CAUTION", "信号升级（NORMAL→CAUTION）")
    assert next_state("NORMAL", halt=True)[0] == "HALT"                 # 直达最严重
    # 降级：无人工确认不降；人工确认单步降
    s, why = next_state("HALT")
    assert s == "HALT" and "人工确认" in why
    assert next_state("HALT", manual_confirm=True)[0] == "RESTRICT"
    assert next_state("CAUTION", manual_confirm=True)[0] == "NORMAL"
    # 信号仍在（CAUTION 级）且人工确认 → 降至信号推断级（不是盲目跳级）
    assert next_state("RESTRICT", caution=True, manual_confirm=True)[0] == "CAUTION"
    # 信号未解除（halt 仍在）→ 不降
    assert next_state("HALT", halt=True, manual_confirm=True)[0] == "HALT"


@pytest.mark.unit
def test_state_machine_position_and_buy_rules():
    from backend.shared.risk import allows_buy, position_cap_pct

    assert allows_buy("NORMAL") and allows_buy("CAUTION")
    assert not allows_buy("RESTRICT") and not allows_buy("HALT")
    assert position_cap_pct("NORMAL") == pytest.approx(0.95)
    assert position_cap_pct("CAUTION") == pytest.approx(0.60)
    assert position_cap_pct("HALT") == pytest.approx(0.0)
    assert position_cap_pct("CAUTION", {"CAUTION": 0.5}) == pytest.approx(0.5)
