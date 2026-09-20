"""策略成本 / 容量两维的实现（设计 §2.3）：成交口径 → 评分。

与 :mod:`model_realized` 并列：本模块只放**纯函数**（不读盘、不连库），
盘面取数由调用方（:mod:`strategy_card` 与 ``run_all``）取好传入。

三条口径纪律：
- ``trades[]`` 的 ``commission`` 只含券商佣金，不含印花税/过户费/滑点，
  所以实测成本占比是**下界**，另用 ``CostModel`` 双边成本率给口径化估计，取较劣者判红线；
- ``trades.totalAmount`` 是**元**，``daily_forward.amount`` 是**万元**——容量公式里显式换算，
  单位错一次就是 1e4 倍的静默偏差；
- 缺成交/缺成交额/缺持仓一律 ``insufficient`` + 写明缺的是哪一项，**不按 0 算分**。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from backend.shared.eval_scoring import DimensionScore, score_from_thresholds

# 设计 §2.3 红线：成本吃掉 > 50% 毛利
COST_RED_LINE_RATIO = 0.5
# `daily_forward.amount` 单位是**万元**，trades.totalAmount 是**元**（漏乘差 1e4 倍）
AMOUNT_WAN_TO_YUAN = 1e4
TRADING_DAYS = 252
# 持仓只数低于此值时容量外推偏乐观（公式未计冲击成本与集中度）
CONCENTRATED_POSITIONS = 5
# 策略卡六维权重（与 strategy_card.WEIGHTS 同源，这里只取用到的三项）
WEIGHTS = {
    "return": 20.0,
    "risk": 20.0,
    "stability": 15.0,
    "cost": 15.0,
    "consistency": 20.0,
    "capacity": 10.0,
}


# ── 成交口径（成本 / 容量两维的取数） ────────────────────────────────


def _as_float(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _holdings_by_day(rows: list[tuple[dict[str, Any], float]]) -> list[int]:
    """成交流水回放 → 每个交易日的持仓只数（trades 无 positions 快照，只能回放）。

    只回放**有成交的日**（无成交日持仓不变、不重复计数）。清仓后的空仓日会记 0，
    由 ``trade_stats`` 决定是否计入中位。
    """
    net: dict[str, float] = {}
    by_day: dict[str, list[tuple[str, float, str]]] = {}
    for row, _amount in rows:
        day = str(row.get("date") or "")
        symbol = str(row.get("symbol") or "")
        qty = _as_float(row.get("quantity"))
        action = str(row.get("action") or "").lower()
        if not day or not symbol or qty is None or action not in ("buy", "sell"):
            continue
        by_day.setdefault(day, []).append((symbol, qty, action))
    counts: list[int] = []
    for day in sorted(by_day):
        for symbol, qty, action in by_day[day]:
            net[symbol] = net.get(symbol, 0.0) + (qty if action == "buy" else -qty)
        counts.append(sum(1 for v in net.values() if v > 1e-9))
    return counts


def trade_stats(
    trades: Any, equity_curve: list[float], *, trading_days: int = TRADING_DAYS
) -> dict[str, Any]:
    """回测成交流水 → 换手 / 成本统计（纯函数，不读盘）。

    实测字段（``qlib_backtest_runs`` 的结果 JSON）：``date/symbol/action/price/
    quantity/totalAmount/commission``，金额单位**元**。

    两条必须知道的口径：
    - **佣金不等于成本**：trades 只记券商佣金，不含印花税/过户费/滑点，所以由它算出的
      成本占比是**下界**，另给按 ``CostModel`` 双边成本率折算的口径化估计；
    - **单边口径**：``one_way_amount = Σ|totalAmount| / 2``——建仓/清仓期只有单边成交，
      这个口径会低估换手（trades 无持仓快照，无法更精确），detail 里如实标注。
    """
    raw_rows = [r for r in (trades or []) if isinstance(r, dict)]
    if not raw_rows:
        return {
            "insufficient": True,
            "n_trades": 0,
            "note": "回测结果无成交明细（trades 缺省或为空）——换手与成本无从计算",
        }
    rows: list[tuple[dict[str, Any], float]] = []
    unreadable = 0
    for row in raw_rows:
        amount = _as_float(row.get("totalAmount"))
        if amount is None:
            unreadable += 1
            continue
        rows.append((row, amount))
    if not rows:
        return {
            "insufficient": True,
            "n_trades": 0,
            "n_unreadable": unreadable,
            "note": f"成交流水 {len(raw_rows)} 行全部缺 totalAmount（不可读）——不估换手",
        }

    gross = sum(abs(a) for _, a in rows)
    one_way = gross / 2.0
    commissions = [_as_float(r.get("commission")) for r, _ in rows]
    commission_total = sum(c for c in commissions if c is not None)
    equity = [float(v) for v in equity_curve if _as_float(v) is not None]
    n_days = len(equity)
    avg_equity = float(np.mean(equity)) if equity else 0.0
    turnover_ok = avg_equity > 0 and n_days > 0
    daily_turnover = (one_way / avg_equity / n_days) if turnover_ok else None
    holdings = _holdings_by_day(rows)
    # 容量公式要的是「投资时的典型持仓只数」：空仓日（已清仓）不计入中位，
    # 但要如实报出剔了几天，免得读成「窗口内一直是空仓」。
    invested = [c for c in holdings if c > 0]
    flat_days = len(holdings) - len(invested)
    out: dict[str, Any] = {
        "insufficient": False,
        "source": "result JSON trades[]",
        "n_trades": len(rows),
        "n_unreadable": unreadable,
        "n_days_with_trades": len({str(r.get("date") or "") for r, _ in rows}),
        "n_symbols": len({str(r.get("symbol") or "") for r, _ in rows}),
        "symbols": sorted({str(r.get("symbol") or "") for r, _ in rows}),
        "gross_amount": round(gross, 2),
        "one_way_amount": round(one_way, 2),
        "avg_equity": round(avg_equity, 2),
        "n_days": n_days,
        "daily_turnover": round(daily_turnover, 8) if daily_turnover else None,
        "annual_turnover": (
            round(daily_turnover * trading_days, 6) if daily_turnover else None
        ),
        "commission_total": round(commission_total, 4),
        "commission_rate_realized": (
            round(commission_total / one_way, 8) if one_way > 0 else None
        ),
        "median_holdings": float(np.median(invested)) if invested else None,
        "max_holdings": max(holdings) if holdings else None,
        "flat_days": flat_days,
        "holdings_convention": "中位只数只算有持仓的成交日（空仓日已剔除并另报）",
        "turnover_convention": "单边口径 Σ|totalAmount|/2（建仓/清仓期会低估换手）",
    }
    if unreadable:
        out["note"] = f"{unreadable} 行缺 totalAmount（未计入换手）"
    if not turnover_ok:
        out["note"] = (
            f"净值曲线不可用（{n_days} 天，均净值 {avg_equity}）：换手率无从折算"
        )
    return out


def _insufficient_cost(note: str) -> DimensionScore:
    return DimensionScore(
        "cost",
        "成本",
        WEIGHTS["cost"],
        None,
        False,
        {"insufficient": True, "note": note},
    )


def cost_dim(
    stats: dict[str, Any],
    *,
    gross_pnl: float | None,
    ann_return: float | None,
    round_trip_cost: float | None,
) -> DimensionScore:
    """成本维：换手率 + 成本占比（设计 §2.3），红线「成本吃掉 > 50% 毛利」。

    两个占比口径并列，取**较劣者**判红线：
    ``cost_over_gross``（佣金实测／区间毛利，是下界）与
    ``model_cost_over_return``（年化换手 × 双边成本率／年化收益，是含税费的口径估计）。
    """
    if stats.get("insufficient"):
        return _insufficient_cost(str(stats.get("note") or "成交流水不可用"))
    annual_turnover = stats.get("annual_turnover")
    commission = _as_float(stats.get("commission_total")) or 0.0
    cost_over_gross = commission / gross_pnl if gross_pnl and gross_pnl > 0 else None
    model_cost = (
        annual_turnover * round_trip_cost
        if annual_turnover is not None and round_trip_cost is not None
        else None
    )
    model_cost_over_return = (
        model_cost / ann_return
        if model_cost is not None and ann_return and ann_return > 0
        else None
    )
    turnover_score = score_from_thresholds(
        annual_turnover,
        [(0.0, 100.0), (2.0, 90.0), (6.0, 70.0), (12.0, 40.0), (24.0, 0.0)],
    )
    ratios = [r for r in (cost_over_gross, model_cost_over_return) if r is not None]
    worst = max(ratios) if ratios else None
    detail: dict[str, Any] = {
        "insufficient": False,
        "n_trades": stats.get("n_trades"),
        "n_symbols": stats.get("n_symbols"),
        "one_way_amount": stats.get("one_way_amount"),
        "commission_total": commission,
        "commission_rate_realized": stats.get("commission_rate_realized"),
        "daily_turnover": stats.get("daily_turnover"),
        "annual_turnover": annual_turnover,
        "gross_pnl": round(gross_pnl, 2) if gross_pnl is not None else None,
        "cost_over_gross": round(cost_over_gross, 6)
        if cost_over_gross is not None
        else None,
        "model_cost_over_return": (
            round(model_cost_over_return, 6)
            if model_cost_over_return is not None
            else None
        ),
        "round_trip_cost": round_trip_cost,
        "worst_ratio": round(worst, 6) if worst is not None else None,
        "cost_scope_note": (
            "trades 的 commission 只含券商佣金，不含印花税/过户费/滑点，"
            "故 cost_over_gross 是成本**下界**；model_cost_over_return 用 CostModel "
            "双边成本率折算，含税费口径，两者取较劣者判红线"
        ),
        "turnover_convention": stats.get("turnover_convention"),
    }
    if worst is None:
        detail["note"] = (
            "区间毛利与年化收益均 ≤0：「成本吃掉毛利」无定义，仅按年化换手计分"
        )
        score = turnover_score
    else:
        ratio_score = score_from_thresholds(
            worst, [(0.0, 100.0), (0.1, 85.0), (0.3, 60.0), (0.5, 35.0), (1.0, 0.0)]
        )
        score = round(
            0.7 * (ratio_score if ratio_score is not None else 50.0)
            + 0.3 * (turnover_score if turnover_score is not None else 50.0),
            2,
        )
    red = bool(worst is not None and worst > COST_RED_LINE_RATIO)
    if red:
        detail["red_line"] = (
            f"成本吃掉毛利 {worst:.0%} > {COST_RED_LINE_RATIO:.0%}："
            "换手越高越像给券商打工，净收益对费率假设极敏感"
        )
    if stats.get("note"):
        detail["trade_note"] = stats["note"]
    return DimensionScore("cost", "成本", WEIGHTS["cost"], score, red, detail)


def capacity_dim(
    stats: dict[str, Any],
    *,
    median_amount_wan: float | None,
    n_positions: float | None,
    amount_note: str | None = None,
) -> DimensionScore:
    """容量维：``capacity_estimate(日换手, 持仓中位日成交额, 持仓数)``（假设模型）。

    ``daily_forward.amount`` 单位是**万元**，这里显式换算成元再进公式；
    假设说明（参与率等）原样带出——容量是估算不是实测，不给裸数字。
    """
    if stats.get("insufficient"):
        return _insufficient_capacity(str(stats.get("note") or "成交流水不可用"))
    if median_amount_wan is None or median_amount_wan <= 0:
        return _insufficient_capacity(
            amount_note or "持仓日成交额未取到（daily_forward.amount）——容量无从估算"
        )
    if not n_positions:
        return _insufficient_capacity(
            "成交回放未得出持仓只数（trades 缺 quantity/action）——容量无从估算"
        )
    from backend.services.engine.factor_report.metrics_eval import capacity_estimate

    median_amount_yuan = float(median_amount_wan) * AMOUNT_WAN_TO_YUAN
    est = capacity_estimate(
        stats.get("daily_turnover"), median_amount_yuan, int(round(n_positions))
    )
    detail: dict[str, Any] = {
        "insufficient": False,
        **est,
        "median_amount_wan": float(median_amount_wan),
        "median_amount_yuan": median_amount_yuan,
        "daily_turnover": stats.get("daily_turnover"),
        "annual_turnover": stats.get("annual_turnover"),
    }
    if est.get("est_aum") is None:
        return _insufficient_capacity(
            f"容量估算输入不足（日换手 {stats.get('daily_turnover')}）", detail
        )
    caveat = ""
    if int(round(n_positions)) < CONCENTRATED_POSITIONS:
        caveat = (
            f" 持仓仅 {int(round(n_positions))} 只（<{CONCENTRATED_POSITIONS}）："
            "集中持仓的外推容量偏乐观——公式只按参与率×成交额算，没算「自己就是这只票的对手盘」"
        )
    detail["note"] = (
        f"{est.get('note', '')} 成交额口径：daily_forward.amount 万元 → 元。{caveat}"
    ).strip()
    score = score_from_thresholds(
        est["est_aum"],
        [(0.0, 0.0), (2e7, 40.0), (1e8, 70.0), (5e8, 90.0), (2e9, 100.0)],
    )
    return DimensionScore("capacity", "容量", WEIGHTS["capacity"], score, False, detail)


def _insufficient_capacity(
    note: str, detail: dict[str, Any] | None = None
) -> DimensionScore:
    return DimensionScore(
        "capacity",
        "容量",
        WEIGHTS["capacity"],
        None,
        False,
        {**(detail or {}), "insufficient": True, "note": note},
    )


def consistency_dim() -> DimensionScore:
    """一致性维：回测 ↔ 同期模拟/实盘曲线的对照，评估侧尚未接这条链路。"""
    return DimensionScore(
        "consistency",
        "一致性",
        WEIGHTS["consistency"],
        None,
        False,
        {
            "insufficient": True,
            "note": (
                "回测与同期模拟盘/实盘的净值曲线对照未接入评估侧"
                "（模拟台账在 sim_trades，与本卡数据源不同源）——无对照即缺省，"
                "不拿回测曲线自证"
            ),
        },
    )
