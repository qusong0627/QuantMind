"""内置风控规则（T-RC-01 首批）：L0 系统级 / L1 账户级 / L3 订单级 / L6 数据级。

口径总纲（与 `docs/风险控制体系_设计方案.md` 对齐）：
- **fail-closed**：资金类/价格新鲜度等关键字段缺失（None）→ REJECT，绝不"缺数据放行"；
- 建议类字段缺失（行业占比等）→ WARN（数据可得性问题可见化，不阻断交易）；
- 规则仅在**配置显式列出**时生效（`always_on` 除外：L0 急停/时段），参数缺省见 default_params；
- 时间一律 epoch 秒 → 中国市场按固定 UTC+8（中国无夏令时）解释。

边界口径（可测）：
- 阈值比较一律"**等于阈值放行、超过才拦**"（`amount > max` 拦，`== max` 过）；
- 数量单位=股；金额单位=元。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from collections.abc import Mapping

from backend.shared.risk.contracts import (
    ACTION_HALT,
    ACTION_REJECT,
    ACTION_WARN,
    Decision,
    RiskContext,
)
from backend.shared.risk.registry import rule

CST = timezone(timedelta(hours=8))

# 浮点边界容差：十进制字面量的二进制表示误差（如 0.10+0.05>0.15）不构成越限——
# 阈值比较一律 `value > limit + _EPS`（"等于阈值放行"口径的可测实现）。
_EPS = 1e-9

# A 股默认申报时段（含集合竞价；午休/盘后拒绝）——按交易所口径可配置
CN_SESSION_DEFAULT = [["09:15", "11:30"], ["13:00", "15:00"]]
HK_SESSION_DEFAULT = [["09:30", "12:00"], ["13:00", "16:00"]]

#: 账户快照"陈旧"告警阈值（秒）。取 60 分钟，与 ``real_positions._REAL_SOURCE_STALE_MINUTES``
#: 同源——那边判"这条真账户快照还算数吗"（持仓/账户页），这边只是**告警**（不拦单）：
#: 账户快照不是行情，晚半小时不等于不能用（持仓不交易就不变），故"陈旧"不是拒绝理由，
#: 但必须可见（金额类 L1 判定的输入到底有多旧）。
_ACCOUNT_STALE_WARN_S = 3600.0


def _reject(rule_id: str, level: str, reason: str, **evidence: Any) -> Decision:
    return Decision(
        rule_id=rule_id,
        level=level,
        action=ACTION_REJECT,
        reason=reason,
        evidence=evidence,
    )


def _warn(rule_id: str, level: str, reason: str, **evidence: Any) -> Decision:
    return Decision(
        rule_id=rule_id,
        level=level,
        action=ACTION_WARN,
        reason=reason,
        evidence=evidence,
    )


def _hm_ok(now_hm: str, windows: list[list[str]]) -> bool:
    return any(start <= now_hm < end for start, end in windows)


# ── L0 系统级 ─────────────────────────────────────────────────────────


@rule(
    "l0.kill_switch",
    "L0",
    "急停开关：置位时全停（拒新单 + 触发全撤迁移）",
    always_on=True,
)
def l0_kill_switch(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if ctx.kill_switch:
        return Decision(
            rule_id="l0.kill_switch",
            level="L0",
            action=ACTION_HALT,
            reason="急停开关置位（kill switch）",
            evidence={"kill_switch": True},
        )
    return None


@rule("l0.session", "L0", "交易日/时段校验（周末与场外拒单）", always_on=True)
def l0_session(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    windows_by_market = params.get(
        "windows", {"CN": CN_SESSION_DEFAULT, "HK": HK_SESSION_DEFAULT}
    )
    market = str(ctx.market or "CN").upper()
    windows = windows_by_market.get(market)
    if not windows:
        return _reject(
            "l0.session",
            "L0",
            f"市场时段未配置（{market}），fail-closed",
            market=market,
        )
    ts = ctx.now_ts or time.time()
    local = datetime.fromtimestamp(ts, tz=CST)
    if str(local.date()) in {str(d) for d in (params.get("holidays") or [])}:
        return _reject("l0.session", "L0", "非交易日（节假日）", date=str(local.date()))
    if local.weekday() >= 5:
        return _reject("l0.session", "L0", "非交易日（周末）", date=str(local.date()))
    now_hm = local.strftime("%H:%M")
    if not _hm_ok(now_hm, windows):
        if ctx.queued_intent:
            # 盘后入队（挂单）：申报时段约束由派发环节（下一交易时段）保证——
            # 此处降级为告警并留痕，避免"盘后挂单"被时段规则误拒（2026-09-18 影子实测）
            return _warn(
                "l0.session", "L0", "盘后入队：申报时段校验延后到派发环节", hm=now_hm
            )
        return _reject("l0.session", "L0", "非申报时段", hm=now_hm, windows=windows)
    return None


@rule(
    "l0.clock_drift",
    "L0",
    "时钟漂移校验（与交易所时间偏差超限拒单）",
    max_skew_ms=500.0,
)
def l0_clock_drift(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if ctx.clock_skew_ms is None:
        return None  # 未测量（适配器可提供）；测量后超限必拦
    max_skew = float(params.get("max_skew_ms", 500.0))
    if abs(float(ctx.clock_skew_ms)) > max_skew:
        return _reject(
            "l0.clock_drift",
            "L0",
            "时钟漂移超限",
            skew_ms=ctx.clock_skew_ms,
            max_skew_ms=max_skew,
        )
    return None


# ── L1 账户级 ─────────────────────────────────────────────────────────


@rule("l1.available_cash", "L1", "买入可用资金校验（快照缺失=拒，fail-closed）")
def l1_available_cash(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "BUY":
        return None
    if ctx.available_cash is None:
        return _reject(
            "l1.available_cash", "L1", "账户快照缺失（可用资金未知），fail-closed"
        )
    amount = ctx.order_amount()
    if amount is None:
        return _reject(
            "l1.available_cash", "L1", "订单金额不可得（价格/数量缺失），fail-closed"
        )
    if amount > float(ctx.available_cash) + _EPS:
        return _reject(
            "l1.available_cash",
            "L1",
            "买入金额超可用资金",
            amount=amount,
            available_cash=ctx.available_cash,
        )
    return None


@rule("l1.t1_sellable", "L1", "T+1 可卖量校验（卖出 ≤ 可用持仓）")
def l1_t1_sellable(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "SELL":
        return None
    if ctx.sellable_volume is None:
        return _reject("l1.t1_sellable", "L1", "可卖量未知（快照缺失），fail-closed")
    if int(ctx.quantity) > int(ctx.sellable_volume):
        return _reject(
            "l1.t1_sellable",
            "L1",
            "卖出量超可卖持仓",
            quantity=ctx.quantity,
            sellable=ctx.sellable_volume,
        )
    return None


@rule("l1.position_cap", "L1", "单票市值上限（占比=持仓+本单 ≤ 上限）", max_pct=0.15)
def l1_position_cap(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "BUY":
        return None
    if ctx.total_assets is None or float(ctx.total_assets) <= 0:
        return _reject("l1.position_cap", "L1", "总资产未知（快照缺失），fail-closed")
    amount = ctx.order_amount()
    if amount is None:
        return _reject("l1.position_cap", "L1", "订单金额不可得，fail-closed")
    max_pct = float(params.get("max_pct", 0.15))
    held = float(ctx.position_pct or 0.0)
    after = held + amount / float(ctx.total_assets)
    if after > max_pct + _EPS:
        return _reject(
            "l1.position_cap",
            "L1",
            "单票占比超上限",
            after_pct=round(after, 4),
            max_pct=max_pct,
        )
    return None


@rule(
    "l1.per_order_pct",
    "L1",
    "单笔买入金额上限（本单/总资产 ≤ max_pct；**只拦买入**）",
    max_pct=0.15,
)
def l1_per_order_pct(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    """单**笔**买入的体量上限（本单金额 ÷ 总资产）——与 ``l1.position_cap`` 的分工。

    为什么不是 ``l1.position_cap`` 的重复
    ------------------------------------
    ``l1.position_cap`` 管的是"**这笔成交后**该标的的累计占比"（分子 = 持仓 + 本单）。
    对空仓标的它等价于单笔上限；对**已持仓**标的只剩残量（持仓 14% 时最多再买 1%），
    于是"**防守档下单笔下得太重**"这件事无人约束——持仓 3% 的票在防守日一笔打进
    12%，position_cap（15%）照样放行，而单笔风险预算只想给 10%。

    档位联动与分母口径（与隔壁的差异要点名）
    ----------------------------------------
    键与角色照搬隔壁 ``quant-Trader/scripts/buy_gate.py`` 的 ``per_stock_pct``
    （风险档位三键之一：平静 0.20 / 谨慎 0.15 / 防守 0.10），但**分母不同**：
    隔壁的分母是"agent **剩余**额度"（¥10 万/agent 的**分账虚拟额度**减去
    ``agent_used``，见 ``live_llm_trade.py``：11/712/1339 行）——那是**预算分配**
    语义，属于决策层职责；本闸门在执行面只做**绝对体量**上限，分母取账户总资产
    （单账户场景下"额度≡总资产"），"剩余"那一半由决策层自行扣减、并由
    ``l1.available_cash`` 兜底"不超过可用现金"。
    后果：仓位很重时（现金 < 总资产）本规则比隔壁**松**——但 orders 侧真实现金
    约束在 ``l1.available_cash`` 上，两道闸取交集后不产生"下得出去但隔壁拦"的单。
    本规则默认 **0.15**
    ——与 ``l1.position_cap`` 同值，即"未接档位时不额外收紧"：同值下新开仓完全被
    position_cap 覆盖，上线不会突然拦掉本来能过的单；档位层把它压到 0.10 才真正
    开始约束单笔体量。**放宽**（平静档的 0.20）须显式改配置——配置是静态上限，
    档位只能在此基础上更严（`shared/risk/tiers.py`）。

    只拦买入
    --------
    卖出是降杠杆动作，拦它会把账户锁在高敞口里（同 ``l1.leverage_cap``）。

    fail-closed
    -----------
    总资产 / 订单金额不可得 → 拒（与 ``l1.position_cap`` 同口径）。不能"拿不到就
    当 0%"——那等于在快照故障时把这条闸整个关掉。
    """
    if str(ctx.side).upper() != "BUY":
        return None
    point = {
        "account_source": ctx.account_source or None,
        "account_age_s": (
            round(ctx.account_age_s, 1) if ctx.account_age_s is not None else None
        ),
    }
    if ctx.total_assets is None or float(ctx.total_assets) <= 0:
        return _reject(
            "l1.per_order_pct", "L1", "总资产未知（快照缺失），fail-closed", **point
        )
    amount = ctx.order_amount()
    if amount is None:
        return _reject("l1.per_order_pct", "L1", "订单金额不可得，fail-closed", **point)

    max_pct = float(params.get("max_pct", 0.15))
    assets = float(ctx.total_assets)
    pct = amount / assets
    if pct > max_pct + _EPS:
        return _reject(
            "l1.per_order_pct",
            "L1",
            "单笔买入金额超总资产上限",
            order_pct=round(pct, 4),
            max_pct=max_pct,
            order_amount=round(amount, 2),
            total_assets=round(assets, 2),
            **point,
        )
    return None


@rule(
    "l1.new_buys_per_day",
    "L1",
    "当日新开仓标的上限（**只拦买入新仓**；加仓不算）",
    max_new_buys=3,
)
def l1_new_buys_per_day(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    """当日**新开仓**的标的数上限（口径与隔壁 ``buy_gate.check_buy`` 逐条对齐）。

    语义
    ----
    - 加仓（该标的已有持仓）**不占**额度：`max_new_buys` 管的是"今天往几个**新**
      标的里开仓"，不是"今天能下几笔单"——后者是 ``l3.order_frequency`` 的事；
    - 按**标的集合**去重而不是按笔数：同一标的当日第二次买入不再占用额度
      （``opened_today`` 是当日已成交买入的标的集合，隔壁 ``daily_buy_codes`` 同义）；
    - 判定只在**本单会成为新开仓**时进行（``position_pct`` 为 0/不可得）。

    为什么需要（隔壁实况）
    --------------------
    风险预算给的是"全天新开仓 ≤ N 只"。没有这条时，一个把所有票都算成"小仓位"的
    策略可以在一天内摊开二十个新标的——每个都过 position_cap，合起来却是一次
    没有预算的分散下注。隔壁 2026-09-17 的影子账把它列为独立规则
    （``cap.daily_new_buys``），因为它的代价与"单票太重"是两回事。

    ``opened_today`` 不可得 → 拒（fail-closed）
    -------------------------------------------
    这是**买入侧**上限：计数拿不到时按 0（"今天还没开过仓"）等于当天无上限，
    恰好在最需要它的时候失效。故 ``None``（计数查询失败）→ 拒**新开仓**；
    加仓与卖出不受影响，账户不会被锁死。与 ``l1.available_cash`` 的
    "快照缺失=拒"同一条纪律。

    ``position_pct`` 不可得（None）→ 按**新开仓**处理：那是收紧方向
    （宁可多算一次新开仓，不可把新开仓当加仓放过去）。
    """
    if str(ctx.side).upper() != "BUY":
        return None
    if float(ctx.position_pct or 0.0) > 0:
        return None  # 加仓：不占"新开仓"额度（position_cap 管累计占比）
    opened = ctx.opened_today
    max_new_buys = int(params.get("max_new_buys", 3))
    if opened is None:
        return _reject(
            "l1.new_buys_per_day",
            "L1",
            "当日新开仓计数不可得（查询失败），fail-closed 拒新开仓",
            max_new_buys=max_new_buys,
        )
    # 代码口径：ctx.symbol 是行情/后缀口径（600036.SH），DB 里是前缀口径（SH600036）。
    # 两侧都过 `to_prefix` 归一后再比——跨层等值匹配不做归一的后果是静默查空：
    # 这里会退化成"同一标的当日第二次买入也算一笔新开仓"（收紧方向，但仍不对）。
    from backend.shared.stock_utils import StockCodeUtil

    symbol = StockCodeUtil.to_prefix(str(ctx.symbol or ""))
    opened_prefix = [StockCodeUtil.to_prefix(str(s)) for s in opened]
    if symbol and symbol in opened_prefix:
        return None  # 该标的今天已开过仓：不再占用额度（同一标的重复买入）
    if len(opened) >= max_new_buys:
        return _reject(
            "l1.new_buys_per_day",
            "L1",
            "当日新开仓已达上限",
            opened_today=len(opened),
            max_new_buys=max_new_buys,
            opened_symbols=sorted(s for s in opened_prefix if s)[:10],
        )
    return None


@rule(
    "l1.leverage_cap",
    "L1",
    "总杠杆上限（(持仓市值+本单)/权益 ≤ max_leverage；**只拦买入**）",
    max_leverage=1.0,
    stale_warn_s=_ACCOUNT_STALE_WARN_S,
)
def l1_leverage_cap(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    """全账户总敞口上限 —— 18 条既有规则里的**唯一一条总额闸**。

    为什么必须有
    ------------
    既有规则全是**逐个维度**的上限：单票 15%、行业 30%、单笔 10 万。
    一组"每只都不超 15%"的持仓可以轻松把总敞口顶到 100% 以上（十只就是 150%），
    没有任何一条规则看得见"总共投了多少"。等价于把鸡蛋分成十份放进同一个篮子。

    只拦买入
    --------
    卖出是**降杠杆**动作。在超杠杆状态下拦卖单会把账户锁死在高敞口里，
    与规则自身目的相反。故本规则只拒 ``BUY``，卖出恒放行。

    **已知局限（融券前是潜在问题）**：订单只有 ``side``，没有"开/平"方向位，
    `买券还券`（``buy_to_close``）在语义上同样是 ``BUY``，会被本条当加杠杆拦下——
    那会拦住**降杠杆**动作。今天无碍：A 股无融券、模拟账户负持仓即删，全库不存在
    空头仓位；融券落地时必须先给订单带开平标志，再在此按 (side, offset) 分开判。

    分子含本单
    ----------
    与 ``l1.position_cap`` 同口径（"占比=持仓+本单"）：判的是**这笔单成交后**
    的敞口，否则「刚好卡在阈值下」的连续小单可以逐笔加杠杆、每笔都合法。

    ``total_position_value`` 为 None 时 fail-closed 拒买
    ----------------------------------------------------
    与 ``l1.available_cash``/``l1.position_cap`` 同一约定：L1 资金类字段缺失
    = 账户快照不可得 → 拒。**不能**退化成"拿不到持仓市值就当 0"——那等价于
    在快照故障时把总杠杆闸整个关掉，恰好在最需要它的时候失效。

    默认 1.0 而非隔壁的 1.5：隔壁硬编码 1.5，但实测其 ``risk_budget_agent``
    当天把档位压到 1.0 才真正生效。本文件不含第二杠杆来源，但**动态档位层
    （``shared/risk/tiers.py``）已接线**：档位只在此基础上继续收紧、不能放大
    （融资场景要放宽须显式改配置参数）。规则层仍只读参数、不感知档位——
    合并发生在 ``risk_gate_service.load_config``。

    账户快照陈旧只告警、不拦（``stale_warn_s``）
    -------------------------------------------
    判决质量取决于输入的账户快照有多旧。快照是**账户**数据而非行情：持仓不交易
    就不变，晚半小时不等于不能用——故陈旧不构成拒绝理由（拦在小额买单上会造成
    "闸门莫名拦单"），但必须可见：``account_age_s`` 超出 ``stale_warn_s``
    （缺省 60 分钟，与 ``real_positions`` 的"真账户快照过期"同源）或**时点不可得**
    时，放行并留一条带年龄与来源的 WARN。显式传 ``stale_warn_s=None`` 关闭该告警。
    （与 QMT Agent 那 120 秒的在线判定不是一回事：那条判"桥上还有没有代理在跑"，
    这条判"拿来算杠杆的数字有多旧"。）
    """
    if str(ctx.side).upper() != "BUY":
        return None  # 卖出降杠杆，绝不拦
    point = {
        "account_source": ctx.account_source or None,
        "account_age_s": (
            round(ctx.account_age_s, 1) if ctx.account_age_s is not None else None
        ),
    }
    if ctx.total_assets is None or float(ctx.total_assets) <= 0:
        return _reject(
            "l1.leverage_cap", "L1", "总资产未知（快照缺失），fail-closed", **point
        )
    if ctx.total_position_value is None:
        return _reject(
            "l1.leverage_cap",
            "L1",
            "持仓市值合计未知（快照缺失），fail-closed",
            **point,
        )
    amount = ctx.order_amount()
    if amount is None:
        return _reject("l1.leverage_cap", "L1", "订单金额不可得，fail-closed", **point)

    max_lev = float(params.get("max_leverage", 1.0))
    assets = float(ctx.total_assets)
    held = float(ctx.total_position_value)
    projected = (held + amount) / assets
    if projected > max_lev + _EPS:
        return _reject(
            "l1.leverage_cap",
            "L1",
            "总杠杆超上限，拒买",
            projected_leverage=round(projected, 4),
            pre_leverage=round(held / assets, 4),
            max_leverage=max_lev,
            position_value=round(held, 2),
            order_amount=round(amount, 2),
            total_assets=round(assets, 2),
            **point,
        )
    warn_s = params.get("stale_warn_s", _ACCOUNT_STALE_WARN_S)
    if warn_s is not None and (
        ctx.account_age_s is None or float(ctx.account_age_s) > float(warn_s)
    ):
        return _warn(
            "l1.leverage_cap",
            "L1",
            "账户快照陈旧或时点不可得（数据可得性，放行并记录）",
            projected_leverage=round(projected, 4),
            warn_after_s=float(warn_s),
            **point,
        )
    return None


@rule(
    "l1.industry_cap", "L1", "行业集中度上限（占比未知记 WARN，超限拒）", max_pct=0.30
)
def l1_industry_cap(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "BUY":
        return None
    max_pct = float(params.get("max_pct", 0.30))
    if ctx.industry_pct is None:
        return _warn("l1.industry_cap", "L1", "行业占比未知（数据可得性），放行并记录")
    if ctx.total_assets is None:
        return _warn(
            "l1.industry_cap", "L1", "总资产未知，行业占比无法折算，放行并记录"
        )
    amount = ctx.order_amount() or 0.0
    after = float(ctx.industry_pct) + amount / float(ctx.total_assets)
    if after > max_pct + _EPS:
        return _reject(
            "l1.industry_cap",
            "L1",
            "行业集中度超上限",
            after_pct=round(after, 4),
            max_pct=max_pct,
        )
    return None


@rule(
    "l1.daily_loss_limit",
    "L1",
    "日内亏损限额（≤ -max_loss_pct% 停止开仓）",
    max_loss_pct=3.0,
)
def l1_daily_loss_limit(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "BUY":
        return None
    if ctx.daily_pnl_pct is None:
        return None
    limit = -abs(float(params.get("max_loss_pct", 3.0)))
    if float(ctx.daily_pnl_pct) <= limit:
        return _reject(
            "l1.daily_loss_limit",
            "L1",
            "日内亏损达限额，停止开仓",
            daily_pnl_pct=ctx.daily_pnl_pct,
            limit_pct=limit,
        )
    return None


# ── L3 订单级 ─────────────────────────────────────────────────────────


@rule("l3.max_order_value", "L3", "单笔金额上限", max_value=100_000.0)
def l3_max_order_value(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    amount = ctx.order_amount()
    if amount is None:
        return _reject("l3.max_order_value", "L3", "订单金额不可得，fail-closed")
    max_value = float(params.get("max_value", 100_000.0))
    if amount > max_value + _EPS:
        return _reject(
            "l3.max_order_value",
            "L3",
            "单笔金额超限",
            amount=amount,
            max_value=max_value,
        )
    return None


@rule(
    "l3.price_deviation",
    "L3",
    "价格偏离闸门（限价 vs 最新价；强平单仅保 sanity 上界）",
    max_dev=0.02,
    sanity_max_dev=0.20,
)
def l3_price_deviation(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if (
        str(ctx.order_type).upper() != "LIMIT"
        or ctx.price is None
        or ctx.last_price in (None, 0)
    ):
        return None
    dev = abs(float(ctx.price) / float(ctx.last_price) - 1.0)
    if ctx.forced_exit:
        cap = float(params.get("sanity_max_dev", 0.20))
        if dev > cap + _EPS:
            return _reject(
                "l3.price_deviation",
                "L3",
                "强平单价格超 sanity 上界",
                dev=round(dev, 4),
                cap=cap,
                forced_exit=True,
            )
        return None
    max_dev = float(params.get("max_dev", 0.02))
    if dev > max_dev + _EPS:
        return _reject(
            "l3.price_deviation",
            "L3",
            "委托价偏离最新价超限",
            dev=round(dev, 4),
            max_dev=max_dev,
            price=ctx.price,
            last=ctx.last_price,
        )
    return None


@rule(
    "l3.stale_quote", "L3", "陈旧价拒单（行情时间戳早于阈值；不可得=拒）", max_age_s=5.0
)
def l3_stale_quote(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    max_age = float(params.get("max_age_s", 5.0))
    age = ctx.quote_age_s
    if age is not None and float(age) <= max_age:
        return None
    # 陈旧或不可得。盘后入队：快照过期属常态（有可用市场价参与金额类校验即可），
    # 真实行情在派发环节重取——降级为告警并留痕（2026-09-18 影子+现场实测；
    # 注意用 last_price[市场价] 而非 price[委托限价]，市价单 price 恒为 None）
    if ctx.queued_intent and ctx.last_price is not None:
        return _warn(
            "l3.stale_quote",
            "L3",
            "盘后入队：行情时效校验延后到派发环节",
            age_s=age,
            price_source=ctx.price_source,
        )
    if age is None:
        return _reject("l3.stale_quote", "L3", "行情时间戳不可得，fail-closed")
    return _reject(
        "l3.stale_quote", "L3", "行情陈旧", age_s=ctx.quote_age_s, max_age_s=max_age
    )


@rule("l3.order_frequency", "L3", "下单频率上限（每分钟）", max_per_minute=20)
def l3_order_frequency(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    max_per_minute = int(params.get("max_per_minute", 20))
    if int(ctx.orders_last_minute) >= max_per_minute:
        return _reject(
            "l3.order_frequency",
            "L3",
            "下单频率超限",
            orders_last_minute=ctx.orders_last_minute,
            max_per_minute=max_per_minute,
        )
    return None


@rule(
    "l3.cancel_ratio",
    "L3",
    "撤单率监控（超限记 WARN 供限频，不直接拒单）",
    max_ratio=0.40,
    min_orders=10,
)
def l3_cancel_ratio(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    min_orders = int(params.get("min_orders", 10))
    if int(ctx.orders_today) < min_orders:
        return None
    ratio = int(ctx.cancels_today) / max(1, int(ctx.orders_today))
    max_ratio = float(params.get("max_ratio", 0.40))
    if ratio > max_ratio:
        return _warn(
            "l3.cancel_ratio",
            "L3",
            "撤单率超监管参考线（建议限频）",
            ratio=round(ratio, 4),
            max_ratio=max_ratio,
        )
    return None


@rule("l3.self_trade", "L3", "自成交防范（窗口内同标的反向单存在即拒）")
def l3_self_trade(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    opposite = "SELL" if str(ctx.side).upper() == "BUY" else "BUY"
    for sym, side in ctx.recent_symbol_sides:
        if str(sym) == str(ctx.symbol) and str(side).upper() == opposite:
            return _reject(
                "l3.self_trade",
                "L3",
                "同标的窗口内存在反向委托（自成交风险）",
                symbol=ctx.symbol,
                opposite=opposite,
            )
    return None


@rule(
    "l3.lot_size",
    "L3",
    "整手校验（买入整手：主板 100/科创 200；卖出允许零股清仓）",
    default_lot=100,
    star_lot=200,
)
def l3_lot_size(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    qty = int(ctx.quantity)
    if qty <= 0:
        return _reject("l3.lot_size", "L3", "委托数量非正", quantity=qty)
    if str(ctx.side).upper() != "BUY":
        return None
    code = str(ctx.symbol)
    num = "".join(ch for ch in code if ch.isdigit())[:6]
    lot = (
        int(params.get("star_lot", 200))
        if num.startswith("688")
        else int(params.get("default_lot", 100))
    )
    if qty % lot != 0:
        return _reject(
            "l3.lot_size",
            "L3",
            "买入数量非整手",
            quantity=qty,
            lot=lot,
            symbol=ctx.symbol,
        )
    return None


@rule("l3.duplicate_fingerprint", "L3", "重复单防范（窗口内同参数指纹存在即拒）")
def l3_duplicate_fingerprint(
    ctx: RiskContext, params: Mapping[str, Any]
) -> Decision | None:
    fp = str(ctx.fingerprint or "").strip()
    if fp and fp in ctx.recent_fingerprints:
        return _reject(
            "l3.duplicate_fingerprint",
            "L3",
            "窗口内存在同参数委托（重复单）",
            fingerprint=fp,
        )
    return None


# ── L6 数据/模型级 ────────────────────────────────────────────────────


@rule("l6.book_invalid", "L6", "盘口异常（倒挂/空盘口）拒单")
def l6_book_invalid(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if ctx.book_crossed or ctx.book_empty:
        return _reject(
            "l6.book_invalid",
            "L6",
            "盘口异常",
            crossed=ctx.book_crossed,
            empty=ctx.book_empty,
        )
    return None


@rule("l6.contract_mismatch", "L6", "模型/特征契约不符拒单（由上游置位）")
def l6_contract_mismatch(
    ctx: RiskContext, params: Mapping[str, Any]
) -> Decision | None:
    if not ctx.contract_ok:
        return _reject("l6.contract_mismatch", "L6", "模型/特征契约不符")
    return None
