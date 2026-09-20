"""账户长序列载荷（设计 §1.6）：模拟盘日度快照 → 前端详情图。

账户是三类里**最薄**的一条序列：实测模拟盘净值快照每个账户只有 5~10 天
（``10000001:CN`` 5 天、``999:ALL`` 10 天）。所以这个模块的重点不是画图，
而是**如实标注样本量**——5 个点连成的线不是趋势，页面上必须说出来。

**三条纪律**：

1. **缺测不补 0**：``today_pnl`` 为空就跳过并计数——补 0 等于宣称「那天不赚不亏」；
2. **市场各成序列**：按市场取行由装载层负责（``load_fund_snapshot_rows``，快照表
   带 market 列时 ``FUTURES``/``ALL`` 各有各的行）；本层只对「非 CN 且一行都没有」
   写明原因，绝不拿 CN 的行顶上；
3. **样本量进 notes**：不足 :data:`MIN_TREND_POINTS` 天时写明「只有 N 个交易日」
   并带起止日期，避免一条 5 点折线被读成趋势。
"""

from __future__ import annotations

import math
from typing import Any

# 单条曲线点数上限（与 factor_series / model_series / strategy_series 同口径）
MAX_SERIES_POINTS = 2000
# 少于这么多个交易日就不叫趋势：只当抽样看（20 个交易日 ≈ 一个月）
MIN_TREND_POINTS = 20

_NON_CN_NOTE = "{market} 市场没有净值快照行（不误用 CN 序列）；该账户在此窗口无快照"


def _as_float(raw: Any) -> float | None:
    """数值化；None / 非数 / NaN / inf → None（缺测，不是 0）。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _points(
    rows: list[dict[str, Any]], field: str, max_points: int
) -> tuple[list[dict[str, Any]], int, str]:
    """取一列成点列 → (点列, 丢弃数, 截断说明)。空值跳过（不补 0）。"""
    points: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            dropped += 1
            continue
        value = _as_float(row.get(field))
        if value is None:
            dropped += 1
            continue
        points.append({"date": str(row.get("date") or ""), "value": value})
    total = len(points)
    if total <= max_points:
        return points, dropped, ""
    return (
        points[-max_points:],
        dropped,
        f"序列共 {total} 点，只保留最近 {max_points} 点（截去最早 {total - max_points} 点）",
    )


def _sample_days(rows: list[dict[str, Any]]) -> int:
    """快照覆盖的**交易日**数（按日期去重；一行一天是常态，但不去重会虚报样本量）。"""
    return len({str(r.get("date") or "") for r in rows if isinstance(r, dict)})


def _window_return(points: list[dict[str, Any]]) -> float | None:
    if len(points) < 2:
        return None
    first = float(points[0]["value"])
    last = float(points[-1]["value"])
    if first <= 0:
        return None
    return last / first - 1.0


def account_series_payload(
    rows: list[dict[str, Any]] | None,
    *,
    market: str = "CN",
    scalars: dict[str, Any] | None = None,
    max_points: int = MAX_SERIES_POINTS,
) -> dict[str, Any]:
    """模拟盘快照行 → ``{series, scalars, notes}``。

    ``rows`` 为按日期升序的 ``[{date, total_asset, today_pnl}]``（``today_pnl``
    可缺）——本函数只摊平，不重算净值和盈亏。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    is_cn = str(market or "").upper() == "CN"
    sample_days = _sample_days(rows)

    # 非 CN 且一行都没有：如实说是「这个市场没有行」，不画空线也不借 CN 的行
    missing_market_rows = not rows and not is_cn
    equity, eq_dropped, eq_truncated = _points(rows, "total_asset", max_points)
    pnl, pnl_dropped, _ = _points(rows, "today_pnl", max_points)

    equity_note = ""
    pnl_note = ""
    if missing_market_rows:
        equity_note = pnl_note = _NON_CN_NOTE.format(market=market)
    else:
        parts = []
        if not equity:
            parts.append("净值序列为空（模拟盘快照表里没有该账户的行）")
        elif sample_days < MIN_TREND_POINTS:
            parts.append(
                f"模拟盘净值快照仅 {sample_days} 个交易日"
                f"（{equity[0]['date']} → {equity[-1]['date']}）"
                "——点数少，只能当抽样看，别读成趋势"
            )
        if eq_truncated:
            parts.append(eq_truncated)
        if eq_dropped:
            parts.append(f"{eq_dropped} 个快照的净值为空已剔除（不补前值）")
        equity_note = "；".join(parts)

        if not pnl:
            pnl_note = (
                "快照未记录当日盈亏（today_pnl 全空或该列不存在），不给一排 0 柱占位"
            )
        else:
            pnl_parts = []
            if all(float(p["value"]) == 0.0 for p in pnl):
                # 全 0 是「窗口内没有产生盈亏」的实测值，不是缺测：照画，但说一句，
                # 免得一排贴着 0 轴的柱子被当成图坏了
                pnl_parts.append(
                    f"窗口内 {len(pnl)} 个交易日的当日盈亏全为 0（该账户此窗口无盈亏）"
                )
            if pnl_dropped:
                pnl_parts.append(
                    f"{pnl_dropped} 个交易日的当日盈亏为空已跳过（不补 0）"
                )
            pnl_note = "；".join(pnl_parts)

    out_scalars: dict[str, Any] = {
        "n_days": sample_days,
        "n_points": len(equity),
        "sample_days": sample_days,
        "sample_sufficient": sample_days >= MIN_TREND_POINTS,
        "start": equity[0]["date"] if equity else "",
        "end": equity[-1]["date"] if equity else "",
        "latest_total_asset": equity[-1]["value"] if equity else None,
        "window_return": _window_return(equity),
        "pnl_all_zero": bool(pnl) and all(float(p["value"]) == 0.0 for p in pnl),
    }
    out_scalars.update(scalars or {})

    return {
        "series": {"equity": equity, "daily_pnl": pnl},
        "scalars": out_scalars,
        "notes": {"equity": equity_note, "daily_pnl": pnl_note},
    }
