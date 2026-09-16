"""策略评分卡（T-P4-05b-2，设计 §2.3）：回测产物 → 收益/风险/稳定性三维修订 + 诚实缺省维。

数据源：`qlib_backtest_runs.result_file_path`（实测 28 份结果 JSON 在盘，含
equity_curve/drawdown_curve）；基准 = 同窗口指数（index_daily，默认沪深300）。
v1 覆盖率：收益 20 ✅ / 风险 20 ✅ / 稳定性 15 ✅（月度胜率+连续三月红红线）/
成本 15 🟡（向量化引擎无成交明细 → 缺省）/ 一致性 20 🟡（无同期模拟曲线 → 缺省）/
容量 10 🟡（缺省）；缺失维度权重归一（如实）。

用法：python backend/scripts/eval/strategy_card.py [--backtest-id ID | --file PATH] [--save] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.eval_scoring import (  # noqa: E402
    DimensionScore,
    combine_dimension_scores,
    score_from_thresholds,
)

WEIGHTS = {
    "return": 20.0,
    "risk": 20.0,
    "stability": 15.0,
    "cost": 15.0,
    "consistency": 20.0,
    "capacity": 10.0,
}
TRADING_DAYS = 252
BENCHMARK = "000300.SH"


# ── 纯计算（可单测）────────────────────────────────────────────────


def curve_to_returns(values: list[float]) -> np.ndarray:
    arr = np.asarray([float(v) for v in values if v is not None], dtype=float)
    if len(arr) < 2:
        return np.empty(0)
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = arr[1:] / arr[:-1] - 1.0
    return rets[np.isfinite(rets)]


def annualized_return(returns: np.ndarray) -> float | None:
    if len(returns) < 20:
        return None
    total = float(np.prod(1.0 + returns) - 1.0)
    years = len(returns) / TRADING_DAYS
    if years <= 0 or total <= -1.0:
        return None
    return float((1.0 + total) ** (1.0 / years) - 1.0)


def monthly_returns(dates: list[str], values: list[float]) -> list[tuple[str, float]]:
    """按自然月聚合收益（上一个可用净值 → 当月末净值）。"""
    out: list[tuple[str, float]] = []
    month_first: dict[str, float] = {}
    month_last: dict[str, float] = {}
    order: list[str] = []
    for d, v in zip(dates, values, strict=False):
        month = str(d)[:7]
        if month not in month_first:
            month_first[month] = float(v)
            order.append(month)
        month_last[month] = float(v)
    for i, month in enumerate(order):
        start = month_first[month] if i == 0 else month_last[order[i - 1]]
        if start > 0:
            out.append((month, month_last[month] / start - 1.0))
    return out


def score_return_dim(ann: float | None, bench_ann: float | None) -> DimensionScore:
    if ann is None:
        return DimensionScore(
            "return",
            "收益",
            WEIGHTS["return"],
            None,
            False,
            {"insufficient": True, "note": "曲线样本不足"},
        )
    excess = ann - bench_ann if bench_ann is not None else None
    excess_score = score_from_thresholds(
        excess, [(-0.15, 0.0), (-0.03, 30.0), (0.0, 50.0), (0.08, 80.0), (0.20, 100.0)]
    )
    ann_score = score_from_thresholds(
        ann, [(-0.10, 0.0), (0.0, 45.0), (0.10, 70.0), (0.25, 90.0), (0.50, 100.0)]
    )
    total = round(
        0.6 * (excess_score if excess_score is not None else 50.0)
        + 0.4 * (ann_score if ann_score is not None else 50.0),
        2,
    )
    return DimensionScore(
        "return",
        "收益",
        WEIGHTS["return"],
        total,
        False,
        {
            "annual_return": round(ann, 6),
            "benchmark_annual": round(bench_ann, 6) if bench_ann is not None else None,
            "excess_annual": round(excess, 6) if excess is not None else None,
            "excess_score": excess_score,
            "ann_score": ann_score,
        },
    )


def score_risk_dim(
    returns: np.ndarray, drawdowns: list[float] | None, ann: float | None
) -> DimensionScore:
    if len(returns) < 20:
        return DimensionScore(
            "risk",
            "风险",
            WEIGHTS["risk"],
            None,
            False,
            {"insufficient": True, "note": "曲线样本不足"},
        )
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(equity)
    mdd = float(np.min(equity / peak - 1.0))
    if drawdowns:
        mdd = min(mdd, float(min(drawdowns)))
    vol = float(np.std(returns, ddof=1)) * np.sqrt(TRADING_DAYS)
    calmar = (ann / abs(mdd)) if (ann is not None and mdd < 0) else None
    mdd_score = score_from_thresholds(
        abs(mdd), [(0.05, 100.0), (0.15, 85.0), (0.30, 65.0), (0.50, 35.0), (0.80, 0.0)]
    )
    calmar_score = score_from_thresholds(
        calmar, [(0.0, 0.0), (0.5, 40.0), (1.0, 65.0), (2.0, 90.0), (4.0, 100.0)]
    )
    total = round(
        0.6 * (mdd_score if mdd_score is not None else 50.0)
        + 0.4 * (calmar_score if calmar_score is not None else 50.0),
        2,
    )
    red = bool(mdd <= -0.50)
    return DimensionScore(
        "risk",
        "风险",
        WEIGHTS["risk"],
        total,
        red,
        {
            "max_drawdown": round(mdd, 6),
            "annual_vol": round(vol, 6),
            "calmar": round(calmar, 4) if calmar is not None else None,
            "red_line": "MDD ≤ -50%" if red else None,
        },
    )


def score_stability_dim(monthly: list[tuple[str, float]]) -> DimensionScore:
    if len(monthly) < 6:
        return DimensionScore(
            "stability",
            "稳定性",
            WEIGHTS["stability"],
            None,
            False,
            {"insufficient": True, "note": "月度样本 < 6"},
        )
    rets = [r for _, r in monthly]
    win_rate = float(np.mean([r > 0 for r in rets]))
    # 连续 3 月负（尾部）红线
    tail_neg = len(rets) >= 3 and all(r < 0 for r in rets[-3:])
    win_score = score_from_thresholds(
        win_rate, [(0.3, 0.0), (0.45, 45.0), (0.55, 65.0), (0.7, 85.0), (1.0, 100.0)]
    )
    return DimensionScore(
        "stability",
        "稳定性",
        WEIGHTS["stability"],
        win_score,
        tail_neg,
        {
            "months": len(rets),
            "monthly_win_rate": round(win_rate, 4),
            "tail_3m_negative": tail_neg,
            "red_line": "连续 3 月负" if tail_neg else None,
        },
    )


# ── IO / 编排 ───────────────────────────────────────────────────────


def _benchmark_returns(start: str, end: str) -> np.ndarray:
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    df = hub.fetch_series(
        "qdb_index_daily",
        BENCHMARK,
        int(start.replace("-", "").replace("/", "")[:8]),
        int(end.replace("-", "").replace("/", "")[:8]),
        columns=["close"],
    )
    if df is None or len(df) == 0:
        return np.empty(0)
    return curve_to_returns(df.sort_values("dt")["close"].astype(float).tolist())


async def load_backtest_curve(
    backtest_id: str | None, file_path: str | None
) -> dict[str, Any]:
    """结果 JSON → {equity_curve, drawdown_curve, start, end}（async：run_all 复用）。"""
    path = file_path
    if not path:
        from sqlalchemy import text as _text

        from backend.shared.database_manager_v2 import get_session

        def _resolve_existing(raw: str) -> str | None:
            for candidate in (raw, raw + ".json"):
                if Path(candidate).exists():
                    return candidate
            return None

        async def _query() -> str | None:
            async with get_session(read_only=True) as session:
                if backtest_id:
                    rows = (
                        await session.execute(
                            _text(
                                "SELECT result_file_path FROM qlib_backtest_runs "
                                "WHERE backtest_id = :b"
                            ),
                            {"b": backtest_id},
                        )
                    ).fetchall()
                else:
                    # 近期结果文件可能被清理：向下探测直至找到在盘的文件（最多 20 条）
                    rows = (
                        await session.execute(
                            _text(
                                "SELECT result_file_path FROM qlib_backtest_runs "
                                "WHERE status='completed' AND result_file_path IS NOT NULL "
                                "ORDER BY created_at DESC LIMIT 20"
                            )
                        )
                    ).fetchall()
            for row in rows:
                if row and row[0]:
                    hit = _resolve_existing(str(row[0]))
                    if hit:
                        return hit
            return None

        path = await _query()
        if not path:
            return {"error": "未找到回测结果（result_file_path 空）"}
    p = Path(str(path))
    if not p.exists() and Path(str(path) + ".json").exists():
        p = Path(str(path) + ".json")
    if not p.exists():
        return {"error": f"结果文件不存在: {path}"}
    data = json.loads(p.read_text(encoding="utf-8"))
    curve = data.get("equity_curve") or []
    if not curve:
        return {"error": "结果缺 equity_curve"}
    dates = [str(row.get("date")) for row in curve]
    values = [float(row.get("value") or 0.0) for row in curve]
    drawdowns = [
        float(row.get("value") or 0.0) for row in (data.get("drawdown_curve") or [])
    ]
    return {
        "equity_curve": values,
        "drawdown_curve": drawdowns or None,
        "start": dates[0] if dates else "",
        "end": dates[-1] if dates else "",
        "dates": dates,
        "path": str(p),
    }


async def candidate_backtest_ids(*, limit: int = 20) -> list[str]:
    """近期 completed 且登记了结果文件路径的回测 ID（新→旧；文件仍在由 scored 时兜底）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                _text(
                    "SELECT backtest_id FROM qlib_backtest_runs "
                    "WHERE status='completed' AND result_file_path IS NOT NULL "
                    "AND result_file_path <> '' "
                    "ORDER BY created_at DESC LIMIT :n"
                ),
                {"n": max(1, int(limit))},
            )
        ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


async def save_result(result: dict[str, Any], *, snapshot_date: Any = None) -> bool:
    """策略卡结果 → eval_scores（run_all/CLI 共用同一条落表路径）。"""
    from backend.shared.eval_contract import save_eval_score

    return await save_eval_score(
        object_type="strategy",
        object_id=str(result["object_id"]),
        snapshot_date=snapshot_date or date.today(),
        score=result.get("score"),
        grade=result.get("grade"),
        low_confidence=bool(result.get("low_confidence")),
        red_line_failed=result.get("red_line_failed") or [],
        dimensions=result.get("dimensions") or {},
        inputs_version=result.get("inputs_version") or {},
    )


async def score_strategy(
    backtest_id: str | None = None, file_path: str | None = None
) -> dict[str, Any]:
    loaded = await load_backtest_curve(backtest_id, file_path)
    if loaded.get("error"):
        # 结果文件已被清理的历史行 → skipped（正常状态）；调用方据此与真异常区分
        return {
            "object_type": "strategy",
            "object_id": backtest_id or file_path or "",
            "error": loaded["error"],
            "skipped": True,
        }
    returns = curve_to_returns(loaded["equity_curve"])
    ann = annualized_return(returns)
    bench = (
        _benchmark_returns(loaded["start"], loaded["end"])
        if loaded.get("start") and loaded.get("end")
        else np.empty(0)
    )
    bench_ann = annualized_return(bench) if len(bench) else None
    monthly = monthly_returns(loaded["dates"], loaded["equity_curve"])

    dims = [
        score_return_dim(ann, bench_ann),
        score_risk_dim(returns, loaded.get("drawdown_curve"), ann),
        score_stability_dim(monthly),
        DimensionScore(
            "cost",
            "成本",
            WEIGHTS["cost"],
            None,
            False,
            {"insufficient": True, "note": "向量化引擎无成交明细（v1 缺省）"},
        ),
        DimensionScore(
            "consistency",
            "一致性",
            WEIGHTS["consistency"],
            None,
            False,
            {"insufficient": True, "note": "无同期模拟曲线对照（v1 缺省）"},
        ),
        DimensionScore(
            "capacity",
            "容量",
            WEIGHTS["capacity"],
            None,
            False,
            {"insufficient": True, "note": "容量粗估未接线（v1 缺省）"},
        ),
    ]
    combined = combine_dimension_scores(dims)
    return {
        "object_type": "strategy",
        "object_id": backtest_id or loaded.get("path") or str(file_path),
        "window": [loaded["start"], loaded["end"]],
        "n_days": len(returns),
        "inputs_version": {
            "backtest_id": backtest_id,
            "file": str(file_path),
            "weights": WEIGHTS,
            "benchmark": BENCHMARK,
        },
        **combined,
    }


def render_card(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"策略卡 {result.get('object_id')}：{result['error']}"
    lines = [
        f"策略评分卡 {result['object_id']}：{result.get('score')} 分 ｜评级 {result.get('grade')}"
        f"（{result.get('window', ['', ''])[0]} ~ {result.get('window', ['', ''])[1]}）",
        "─" * 46,
    ]
    for key, dim in (result.get("dimensions") or {}).items():
        score = dim.get("score")
        lines.append(
            f"{dim.get('label')}({key}): {score if score is not None else '缺省'} × {dim.get('weight')}"
            + ("  ⚠红线" if dim.get("red_line_failed") else "")
        )
    if result.get("missing_dims"):
        lines.append(f"缺省维度（权重归一）: {result['missing_dims']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="策略评分卡（T-P4-05b）")
    parser.add_argument("--backtest-id", default=None)
    parser.add_argument("--file", default=None, help="直接指定结果 JSON 路径")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(score_strategy(args.backtest_id, args.file))
    if args.save and not result.get("error"):

        async def _run():
            from backend.shared.database_manager_v2 import close_database

            try:
                return await save_result(result)
            finally:
                await close_database()

        asyncio.run(_run())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_card(result))
    return 0 if not result.get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())
