"""策略评分卡（T-P4-05b-2，设计 §2.3）：回测产物 → 收益/风险/稳定性三维修订 + 诚实缺省维。

数据源：`qlib_backtest_runs.result_file_path`（实测结果 JSON 含 equity_curve /
drawdown_curve / trades[]）；基准 = 同窗口指数（index_daily，默认沪深300）；
容量维的持仓日成交额取 `daily_forward.amount`（万元）。
覆盖：收益 20 ✅ / 风险 20 ✅ / 稳定性 15 ✅（月度胜率+连续三月负红线）/
成本 15 ✅（成交换手 + 成本占比，红线「成本吃掉 >50% 毛利」）/
容量 10 ✅（假设模型，假设说明随卡带出）/ 一致性 20 🟡（回测↔模拟盘曲线对照未接评估侧）；
缺失维度权重归一（如实）。

用法：python backend/scripts/eval/strategy_card.py [--backtest-id ID | --file PATH] [--save] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.benchmark import BENCHMARK_SYMBOL  # noqa: E402
from backend.scripts.eval.strategy_realized import (  # noqa: E402
    capacity_dim,
    cost_dim,
    consistency_dim,
    trade_stats,
)
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
BENCHMARK = BENCHMARK_SYMBOL

# 成本模型不可用时的兜底双边成本率（metrics_core 的 BRAIN 默认 20bp）
FALLBACK_ROUND_TRIP_COST = 0.002
MAX_CAPACITY_SYMBOLS = 1200  # 实测单个回测最多 933 只成交标的
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.]+$")


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


def strategy_dims(
    loaded: dict[str, Any],
    *,
    bench_ann: float | None,
    amount_stats: dict[str, Any] | None,
    round_trip_cost: float | None,
) -> tuple[list[DimensionScore], dict[str, Any]]:
    """已加载的回测产物 → 六维（不读库；基准年化与成交额由调用方取好传入）。"""
    equity = [float(v) for v in loaded["equity_curve"]]
    returns = curve_to_returns(equity)
    ann = annualized_return(returns)
    monthly = monthly_returns(loaded["dates"], equity)
    stats = trade_stats(loaded.get("trades") or [], equity)
    gross_pnl = (equity[-1] - equity[0]) if len(equity) >= 2 else None
    amount = amount_stats or {}
    dims = [
        score_return_dim(ann, bench_ann),
        score_risk_dim(returns, loaded.get("drawdown_curve"), ann),
        score_stability_dim(monthly),
        cost_dim(
            stats, gross_pnl=gross_pnl, ann_return=ann, round_trip_cost=round_trip_cost
        ),
        consistency_dim(),
        capacity_dim(
            stats,
            median_amount_wan=amount.get("median_amount_wan"),
            n_positions=stats.get("median_holdings"),
            amount_note=amount.get("note"),
        ),
    ]
    return dims, {"trades": stats, "amount": amount, "annual_return": ann}


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


def normalize_symbols(symbols: list[str]) -> list[str]:
    """成交代码 → QuantDB 后缀式（``sh600007``／``SH600007``／``600007`` → ``600007.SH``）。

    口径必须分层（CLAUDE.md）：回测 trades 里的小写 Qlib 形 ``sh600007`` 直接拿去查
    ``daily_forward`` 会**一行都匹配不上**，而 ``median()`` 对空集返回 NULL——
    静默变成 NaN，容量维看起来「算了」其实没数据。
    """
    from backend.shared.stock_utils import StockCodeUtil

    out: list[str] = []
    for raw in symbols:
        code = str(raw or "").strip()
        if not code or not _SYMBOL_RE.match(code):
            continue
        suffix = StockCodeUtil.to_suffix(code)
        if suffix:
            out.append(suffix)
    return sorted(set(out))


def _as_float(raw: Any) -> float | None:
    """数值或 None（NaN/Inf 一律当「没有值」——`median()` 对空集给的就是 NaN）。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _sample_evenly(items: list[str], cap: int) -> list[str]:
    """超上限时**等距抽样**（不取前 N 只）：按代码序截断会系统性偏向某个交易所。"""
    idx = np.linspace(0, len(items) - 1, num=cap).round().astype(int)
    return [items[i] for i in sorted(set(idx.tolist()))]


def _median_amount_wan(symbols: list[str], start: str, end: str) -> dict[str, Any]:
    """窗口内成交标的的 ``daily_forward.amount`` 中位（**万元**）→ 容量维输入。

    取不到就返回 ``{"note": …}``（调用方据此把容量维标缺省）——不抛、也不填 0；
    匹配到 0 行与 ``amount`` 全空是**两种不同的失败**，note 要分清。
    """
    clean = normalize_symbols(symbols)
    if not clean:
        return {"note": "无成交标的（symbol 缺失或不可归一），日成交额无从取数"}
    note = None
    if len(clean) > MAX_CAPACITY_SYMBOLS:
        note = f"成交标的 {len(clean)} 只超上限，等距抽 {MAX_CAPACITY_SYMBOLS} 只取中位"
        clean = _sample_evenly(clean, MAX_CAPACITY_SYMBOLS)
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        start_dt = int(str(start).replace("-", "").replace("/", "")[:8] or 0)
        end_dt = int(str(end).replace("-", "").replace("/", "")[:8] or 0)
        if not start_dt or not end_dt:
            return {"note": f"回测窗口不可解析（{start} ~ {end}）"}
        in_list = ", ".join(f"'{s}'" for s in clean)
        df = QuantDBDataHub.get_instance().query(
            "SELECT median(amount) AS m, count(*) AS n, count(amount) AS n_amount "
            "FROM qdb_daily_forward "
            f"WHERE symbol IN ({in_list}) AND dt BETWEEN {start_dt} AND {end_dt}"
        )
        row = df.iloc[0] if df is not None and len(df) else None
        matched = int(row["n"]) if row is not None else 0
        valued = int(row["n_amount"]) if row is not None else 0
        if matched == 0:
            return {
                "note": (
                    f"daily_forward 在 {start_dt}~{end_dt} 无匹配行"
                    f"（{len(clean)} 只归一后代码如 {clean[:3]}）——成交额取不到"
                )
            }
        median_wan = _as_float(row["m"]) if row is not None else None
        if median_wan is None:
            return {
                "note": (
                    f"匹配到 {matched} 行但 amount 全为空（{valued} 行有值）"
                    "——成交额取不到"
                )
            }
        return {
            "median_amount_wan": median_wan,
            "n_amount_rows": valued,
            "n_matched_rows": matched,
            "n_symbols_queried": len(clean),
            "amount_unit": "万元",
            **({"note": note} if note else {}),
        }
    except Exception as exc:  # noqa: BLE001 — 取数失败只影响容量维，如实带出原因
        return {"note": f"daily_forward.amount 取数失败：{type(exc).__name__}: {exc}"}


def _round_trip_cost() -> tuple[float, str]:
    """双边成本率：优先 CostModel 口径，取不到退回 BRAIN 默认 20bp 并写明降级。"""
    try:
        from backend.services.engine.inference.trading_cost import CostModel

        model = CostModel()
        return model.round_trip_cost(), "CostModel 默认（佣金+印花税+过户费+滑点）"
    except Exception as exc:  # noqa: BLE001 — 缺依赖时按文档默认值兜底
        return (
            FALLBACK_ROUND_TRIP_COST,
            f"CostModel 不可用（{type(exc).__name__}），退回 BRAIN 默认 20bp 双边",
        )


async def load_backtest_curve(
    backtest_id: str | None, file_path: str | None
) -> dict[str, Any]:
    """结果 JSON → {equity_curve, drawdown_curve, trades, start, end, dates}。"""
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
        "trades": data.get("trades") or [],
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
    bench = (
        _benchmark_returns(loaded["start"], loaded["end"])
        if loaded.get("start") and loaded.get("end")
        else np.empty(0)
    )
    bench_ann = annualized_return(bench) if len(bench) else None
    round_trip_cost, cost_source = _round_trip_cost()
    symbols = [str(t.get("symbol") or "") for t in (loaded.get("trades") or [])]
    amount_stats = _median_amount_wan(
        symbols, loaded.get("start", ""), loaded.get("end", "")
    )
    dims, evidence = strategy_dims(
        loaded,
        bench_ann=bench_ann,
        amount_stats=amount_stats,
        round_trip_cost=round_trip_cost,
    )
    combined = combine_dimension_scores(dims)
    return {
        "object_type": "strategy",
        "object_id": backtest_id or loaded.get("path") or str(file_path),
        "window": [loaded["start"], loaded["end"]],
        "n_days": len(curve_to_returns(loaded["equity_curve"])),
        "inputs_version": {
            "backtest_id": backtest_id,
            "file": str(file_path),
            "weights": WEIGHTS,
            "benchmark": BENCHMARK,
            "round_trip_cost": round_trip_cost,
            "cost_source": cost_source,
            "evidence": {
                **evidence,
                "annual_return": (
                    round(evidence["annual_return"], 6)
                    if evidence.get("annual_return") is not None
                    else None
                ),
            },
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
