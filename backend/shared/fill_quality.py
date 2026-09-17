"""F2 保真度度量（T-P6-18）：成交价偏差 / 成交率 / 部分成交比例 / 滑点实现（纯函数）。

口径（文档化，报表随附）：
- **成交价偏差**：``cost_bps = direction × (fill_price/ref_price − 1) × 1e4``，
  direction=+1 买 / −1 卖（正 = 成本，负 = 收益）；``ref_price`` = 当日真实参考价
  （默认收盘，QuantDB 前复权日线；调用方给定，缺失该笔不计入分布并如实计数）；
- **成交率** = 有成交委托 / 委托总数；**部分成交比例** = 有成交且成交量 < 委托量 的笔数 / 有成交笔数；
- **滑点实现** = 偏差分布中位数与配置滑点（bps）之差（正 = 实现比配置更贵）；
- **保真度分层**：按 ``execution_model``（daily 核 = synthetic_price；F2 = snapshot_core）
  分组计数，交易台标注"本批订单用什么核撮合的"。
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

_DIRECTION = {"buy": 1.0, "sell": -1.0}


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(q * len(ordered) + 0.999)))
    return ordered[rank - 1]


def fidelity_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    reference_prices: Mapping[str, float] | None = None,
    configured_slippage_bps: float | None = None,
) -> dict[str, Any]:
    """当日委托（含未成交）→ 保真度指标块（不可用维度如实 None/计数，不假填）。"""
    refs = {str(k): float(v) for k, v in (reference_prices or {}).items() if _f(v)}
    total = len(rows or [])
    filled_rows: list[Mapping[str, Any]] = []
    for row in rows or []:
        qty = _f(row.get("filled_quantity")) or 0.0
        price = _f(row.get("fill_price"))
        if qty > 0 and price and price > 0:
            filled_rows.append(row)

    deviations: list[float] = []
    missing_ref = 0
    for row in filled_rows:
        symbol = str(row.get("symbol") or "")
        ref = refs.get(symbol)
        direction = _DIRECTION.get(str(row.get("side") or "").lower())
        price = _f(row.get("fill_price"))
        if ref is None or ref <= 0 or direction is None or price is None:
            missing_ref += 1
            continue
        deviations.append(direction * (price / ref - 1.0) * 1e4)

    partial = 0
    for row in filled_rows:
        filled = _f(row.get("filled_quantity")) or 0.0
        want = _f(row.get("quantity")) or _f(row.get("order_quantity")) or 0.0
        if want > 0 and filled < want - 1e-9:
            partial += 1

    by_model: dict[str, int] = {}
    for row in rows or []:
        model = str(row.get("execution_model") or "unknown")
        by_model[model] = by_model.get(model, 0) + 1

    pending = sum(
        1 for r in rows or []
        if str(r.get("status") or "").lower() in {"pending", "partially_filled", "submitted"}
        and (_f(r.get("filled_quantity")) or 0.0) <= 0
    )

    median_dev = statistics.median(deviations) if deviations else None
    slippage_realized = None
    if median_dev is not None and configured_slippage_bps is not None:
        slippage_realized = median_dev - float(configured_slippage_bps)

    return {
        "orders": total,
        "filled_orders": len(filled_rows),
        "fill_rate": round(len(filled_rows) / total, 4) if total else None,
        "pending_orders": pending,
        "partial_ratio": round(partial / len(filled_rows), 4) if filled_rows else None,
        "price_deviation_bps": {
            "n": len(deviations),
            "missing_ref": missing_ref,
            "mean": round(statistics.fmean(deviations), 2) if deviations else None,
            "median": round(median_dev, 2) if median_dev is not None else None,
            "p95": round(_percentile(deviations, 0.95), 2) if deviations else None,
        },
        "slippage_realized_bps": round(slippage_realized, 2) if slippage_realized is not None else None,
        "configured_slippage_bps": configured_slippage_bps,
        "by_execution_model": by_model,
        "caliber": (
            "cost_bps=方向×(成交/参考−1)×1e4（正=成本）；参考价=当日真实收盘（前复权）；"
            "成交率=有成交/委托；部分成交=成交量<委托量；滑点实现=偏差中位数−配置值"
        ),
    }
