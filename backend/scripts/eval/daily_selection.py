"""每日选股评分卡（T-P4-05b）："今天的选股好不好"——§2.4 五维 → 0-100 分 + 评级。

维度（v1 权重，文档化可调）：事前质量 25 / 事后验证 35 / 一致性 15 / 校准 15 / 覆盖 10。
- 事前质量：入选 rank_pct 强度（阈值映射）+ 行业分散 + 板块状态（entry_gate）；
- 事后验证：T+1..T+H 真实超额（前复权日线 vs 沪深300）+ 命中率——**前向数据不足 → pending †**（不假填）；
- 一致性：当日模拟执行记录 vs 计划清单（无执行记录 → 维度缺省，权重归一）；
- 校准：月度层级单调（v1 记 insufficient，待月度回填）；
- 覆盖：入选数与市场状态匹配（空仓判断合规）。

用法：python backend/scripts/eval/daily_selection.py --date 2026-09-08 [--horizon 5] [--save] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.benchmark import BENCHMARK_SYMBOL  # noqa: E402
from backend.scripts.eval.daily_selection_series import (  # noqa: E402
    daily_selection_series_payload,
    history_upto,
)
from backend.shared.eval_scoring import (  # noqa: E402
    DimensionScore,
    combine_dimension_scores,
    score_from_quantile,
    score_from_thresholds,
)
from backend.shared.eval_series import save_series  # noqa: E402

V1_WEIGHTS = {
    "quality": 25.0,
    "realized": 35.0,
    "consistency": 15.0,
    "calibration": 15.0,
    "coverage": 10.0,
}
BENCHMARK = BENCHMARK_SYMBOL


# ── 纯打分函数（可单测）────────────────────────────────────────────


def score_quality(opportunities: list[dict], meta: dict) -> DimensionScore:
    """事前质量：rank_pct 强度 + 行业分散 + 板块状态。"""
    detail: dict[str, Any] = {"picked": len(opportunities)}
    if not opportunities:
        # 空仓也是决策：板块门关闭时"空仓"优秀，门开时"空仓"差（覆盖维度兜底）
        gate = meta.get("entry_gate") or {}
        empty_ok = not (gate.get("ma20_ok") and gate.get("entry_ok"))
        return DimensionScore(
            "quality",
            "事前质量",
            V1_WEIGHTS["quality"],
            70.0 if empty_ok else 30.0,
            False,
            {
                **detail,
                "empty_position_ok": empty_ok,
                "gate": gate,
                "note": "无入选（空仓）",
            },
        )
    strengths = [float(o.get("strength") or 0.0) for o in opportunities]
    top_strength = max(strengths)
    strength_score = score_from_thresholds(
        1 - top_strength,  # 输入已反转：距 1.0 的缺口越小越好（阈值表本身已升序映射）
        [(0.0, 100.0), (0.02, 85.0), (0.05, 60.0), (0.10, 30.0), (0.30, 0.0)],
    )
    industries = [
        (o.get("evidence") or {}).get("industry") or "" for o in opportunities
    ]
    unique_ratio = len({i for i in industries if i}) / max(1, len(opportunities))
    dispersion_score = round(min(100.0, 40.0 + 60.0 * unique_ratio), 2)
    gate = meta.get("entry_gate") or {}
    gate_score = 100.0 if (gate.get("ma20_ok") and gate.get("entry_ok")) else 50.0
    total = round(
        0.5 * (strength_score or 50.0) + 0.3 * dispersion_score + 0.2 * gate_score, 2
    )
    return DimensionScore(
        "quality",
        "事前质量",
        V1_WEIGHTS["quality"],
        total,
        False,
        {
            **detail,
            "top_rank_pct": round(top_strength, 4),
            "strength_score": strength_score,
            "industry_dispersion": round(unique_ratio, 3),
            "dispersion_score": dispersion_score,
            "gate_score": gate_score,
            "market_state": meta.get("market_state"),
        },
    )


def score_realized(
    picks_forward: list[dict], benchmark_return: float | None, *, horizon: int
) -> DimensionScore:
    """事后验证：T+1..T+H 超额 + 命中率；前向数据不足 → None（pending †）。"""
    valid = [p for p in picks_forward if p.get("realized_return") is not None]
    if not valid or benchmark_return is None:
        return DimensionScore(
            "realized",
            "事后验证",
            V1_WEIGHTS["realized"],
            None,
            False,
            {
                "horizon": horizon,
                "pending": True,
                "note": f"T+1..T+{horizon} 前向数据未齐，待回填（不假填）",
            },
        )
    rets = [float(p["realized_return"]) for p in valid]
    excess = [r - float(benchmark_return) for r in rets]
    mean_excess = float(np.mean(excess))
    hit_rate = float(np.mean([e > 0 for e in excess]))
    excess_score = score_from_thresholds(
        mean_excess,
        [(-0.10, 0.0), (-0.02, 30.0), (0.0, 50.0), (0.03, 80.0), (0.10, 100.0)],
    )
    hit_score = score_from_thresholds(
        hit_rate, [(0.0, 0.0), (0.4, 40.0), (0.5, 55.0), (0.7, 85.0), (1.0, 100.0)]
    )
    total = round(0.6 * (excess_score or 50.0) + 0.4 * (hit_score or 50.0), 2)
    return DimensionScore(
        "realized",
        "事后验证",
        V1_WEIGHTS["realized"],
        total,
        False,
        {
            "horizon": horizon,
            "n": len(valid),
            "benchmark_return": round(float(benchmark_return), 6),
            "mean_excess": round(mean_excess, 6),
            "hit_rate": round(hit_rate, 4),
            "excess_score": excess_score,
            "hit_score": hit_score,
        },
    )


def score_coverage(opportunities: list[dict], meta: dict) -> DimensionScore:
    """覆盖：入选数与市场状态匹配（门开有票/门关空仓为合规）。"""
    gate = meta.get("entry_gate") or {}
    gate_open = bool(gate.get("ma20_ok") and gate.get("entry_ok"))
    picked = len(opportunities)
    if gate_open and picked > 0:
        score, note = 100.0, "门开且有入选（合规）"
    elif gate_open and picked == 0:
        score, note = 40.0, "门开但零入选（可能阈值/过滤过严）"
    elif not gate_open and picked == 0:
        score, note = 100.0, "门关且空仓（合规）"
    else:
        score, note = 20.0, "门关但仍有入选（与门禁矛盾，人工核查）"
    return DimensionScore(
        "coverage",
        "覆盖",
        V1_WEIGHTS["coverage"],
        score,
        False,
        {"gate_open": gate_open, "picked": picked, "note": note},
    )


def score_calibration_v1(n_days: int) -> DimensionScore:
    """校准（月度层级单调）：v1 记 insufficient（待月度回填任务）。"""
    return DimensionScore(
        "calibration",
        "校准",
        V1_WEIGHTS["calibration"],
        None,
        False,
        {
            "insufficient": True,
            "note": "月度回填任务未上线（v1 记缺省）",
            "n_days": n_days,
        },
    )


# ── IO / 编排 ───────────────────────────────────────────────────────


def _forward_return(closes: list[float], horizon: int) -> float | None:
    if len(closes) < 2:
        return None
    entry = closes[0]
    if entry <= 0:
        return None
    exit_idx = min(len(closes) - 1, horizon)
    if exit_idx < 1:
        return None
    return float(closes[exit_idx] / entry - 1.0)


def _load_forward_closes(
    symbol: str, start_dt: int, end_dt: int, *, view: str = "qdb_daily_forward"
) -> list[float]:
    """前向收盘序列（个股=daily_forward 前复权；指数=index_daily 独立数据集）。"""
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    df = hub.fetch_series(view, symbol, start_dt, end_dt, columns=["close"])
    if df is None or len(df) == 0:
        return []
    return df.sort_values("dt")["close"].astype(float).tolist()


async def load_selection_history(
    *, tenant_id: str = "default", user_id: str = "", limit: int = 400
) -> list[dict[str, Any]]:
    """`eval_scores` 里本租户的每日选股历史（日期升序，含 dimensions）。

    只取**同一 user_id 键形**的行：`eval_scores` 的唯一键含 user_id，混键形会把
    别的用户的选股曲线画进这张图。``limit`` 取最近 N 天（升序返回）。
    """
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        "SELECT snapshot_date, dimensions FROM eval_scores "
                        "WHERE object_type = 'daily_selection' AND tenant_id = :t "
                        "AND COALESCE(user_id, '') = :u "
                        "ORDER BY snapshot_date DESC LIMIT :n"
                    ),
                    {"t": tenant_id, "u": user_id, "n": max(1, int(limit))},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "snapshot_date": r["snapshot_date"].isoformat()
            if r["snapshot_date"]
            else "",
            "dimensions": r["dimensions"],
        }
        for r in rows
    ][::-1]


async def save_selection_sidecar(
    trade_date: str,
    combined: dict[str, Any],
    *,
    tenant_id: str = "default",
    user_id: str = "",
    horizon: int = 5,
    pending: bool = False,
) -> dict[str, Any]:
    """当日选股 → 跨日期序列侧车（``data/eval_series/daily_selection/<日期>.json``）。

    历史来自 `eval_scores`（还没落库的当天由 ``combined`` 现算现并——顺序上排最后，
    同日重跑以当天为准）。历史读不到就只画当天并写明原因：**缺数据可以，不说不行**。
    """
    history: list[dict[str, Any]] = []
    note = ""
    try:
        history = await load_selection_history(tenant_id=tenant_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — 序列是附加证据，读不到要留痕不能断卡
        note = f"历史行读取失败（{type(exc).__name__}）：{exc}"
    today_row = {
        "snapshot_date": trade_date,
        "dimensions": combined.get("dimensions") or {},
    }
    rows = [*history_upto(history, trade_date), today_row]
    payload = daily_selection_series_payload(
        rows,
        extra_note=note,
        scalars={
            "score": combined.get("score"),
            "grade": combined.get("grade"),
            # 当天入选数不在这里另给一份：coverage.detail.picked 就是它
            # （score_coverage 写的就是 len(opportunities)），两个键会有对不上的风险
            "pending_backfill": bool(pending),
            "horizon": horizon,
        },
    )
    sidecar = save_series("daily_selection", trade_date, payload)
    if note:
        sidecar["history_note"] = note
    return sidecar


async def score_daily_selection(
    trade_date: str | None = None,
    *,
    horizon: int = 5,
    tenant_id: str = "default",
    user_id: str | None = None,
    save: bool = False,
) -> dict[str, Any]:
    """当日选股评分：扫描（分位口径全链路）→ 五维 → 合成 →（可选）落表。"""
    from backend.services.engine.scanners.runner import run_scan

    report = await run_scan(
        trade_date=trade_date, tenant_id=tenant_id, user_id=user_id, mode="quantile"
    )
    meta = (report.get("meta") or [{}])[0]
    # 日期归一 ISO 'YYYY-MM-DD'（loader 产 ISO、CLI 传 'YYYY-MM-DD'、裸 YYYYMMDD 均兼容）
    _raw_date = str(meta.get("trade_date") or trade_date or "").replace("-", "")
    resolved_date = (
        f"{_raw_date[:4]}-{_raw_date[4:6]}-{_raw_date[6:8]}"
        if len(_raw_date) >= 8
        else ""
    )
    opportunities = report.get("opportunities") or []

    dims: list[DimensionScore] = [score_quality(opportunities, meta)]

    # 事后验证：前向 T+1..T+H 真实收益（前复权）+ 基准
    picks_forward: list[dict] = []
    benchmark_return: float | None = None
    pending = False
    if resolved_date:
        from datetime import date as _date
        from datetime import timedelta as _td

        start = _date.fromisoformat(resolved_date)
        start_dt = int(start.strftime("%Y%m%d"))
        end_dt = int((start + _td(days=horizon * 3 + 20)).strftime("%Y%m%d"))
        for o in opportunities:
            closes = _load_forward_closes(o["symbol"], start_dt, end_dt)
            picks_forward.append(
                {
                    "symbol": o["symbol"],
                    "realized_return": _forward_return(closes, horizon),
                }
            )
        bench_closes = _load_forward_closes(
            BENCHMARK, start_dt, end_dt, view="qdb_index_daily"
        )
        benchmark_return = _forward_return(bench_closes, horizon)
        pending = all(p["realized_return"] is None for p in picks_forward)
    dims.append(score_realized(picks_forward, benchmark_return, horizon=horizon))

    # 一致性：当日模拟执行记录数（无执行 → 维度缺省）
    exec_count = 0
    try:
        from sqlalchemy import text as _text

        from backend.shared.database_manager_v2 import get_session

        async with get_session(read_only=True) as session:
            from datetime import date as _dcls

            row = (
                await session.execute(
                    _text(
                        "SELECT count(*) FROM sim_orders "
                        "WHERE tenant_id=:t AND created_at::date = :d"
                    ),
                    {
                        "t": tenant_id,
                        "d": _dcls.fromisoformat(resolved_date)
                        if resolved_date
                        else _dcls(1970, 1, 1),
                    },
                )
            ).scalar()
        exec_count = int(row or 0)
    except Exception:  # noqa: BLE001
        exec_count = 0
    if exec_count > 0:
        match_score = score_from_thresholds(
            min(1.0, exec_count / max(1, len(opportunities))),
            [(0.0, 0.0), (0.5, 50.0), (0.9, 90.0), (1.0, 100.0)],
        )
        dims.append(
            DimensionScore(
                "consistency",
                "一致性",
                V1_WEIGHTS["consistency"],
                match_score,
                False,
                {"exec_orders": exec_count, "planned": len(opportunities)},
            )
        )
    else:
        dims.append(
            DimensionScore(
                "consistency",
                "一致性",
                V1_WEIGHTS["consistency"],
                None,
                False,
                {"note": "当日无模拟执行记录（维度缺省，权重重归一）"},
            )
        )

    dims.append(score_calibration_v1(n_days=1))
    dims.append(score_coverage(opportunities, meta))

    combined = combine_dimension_scores(dims, low_confidence=bool(pending))
    sidecar: dict[str, Any] | None = None
    if resolved_date:
        sidecar = await save_selection_sidecar(
            resolved_date,
            combined,
            tenant_id=tenant_id,
            user_id=str(user_id or ""),
            horizon=horizon,
            pending=pending,
        )
    result = {
        "object_type": "daily_selection",
        "object_id": resolved_date,
        "trade_date": resolved_date,
        "mode": report.get("mode"),
        "picked": len(opportunities),
        "pending_backfill": pending,
        "inputs_version": {
            "scanner": "model_signal",
            "mode": report.get("mode"),
            "horizon": horizon,
            "weights": V1_WEIGHTS,
            # 长序列走 `data/eval_series/daily_selection/<日期>.json`（§1.6）：
            # 每条只装**该日及以前**的历史（详情页不画当时看不到的未来）
            **({"series_sidecar": sidecar} if sidecar else {}),
        },
        **combined,
    }
    if save and resolved_date:
        from backend.shared.eval_contract import save_eval_score

        await save_eval_score(
            object_type="daily_selection",
            object_id=resolved_date,
            snapshot_date=resolved_date,
            score=combined.get("score"),
            grade=combined.get("grade"),
            low_confidence=bool(combined.get("low_confidence")),
            red_line_failed=combined.get("red_line_failed") or [],
            dimensions=combined.get("dimensions") or {},
            inputs_version=result["inputs_version"],
            tenant_id=tenant_id,
            user_id=str(user_id or ""),
        )
    return result


def render_scorecard(result: dict[str, Any]) -> str:
    lines = [
        f"每日选股评分 {result.get('trade_date')}：{result.get('score')} 分 "
        f"｜评级 {result.get('grade')}｜入选 {result.get('picked')} 只"
        + ("（事后验证待回填 †）" if result.get("pending_backfill") else ""),
        "─" * 46,
    ]
    for key, dim in (result.get("dimensions") or {}).items():
        score = dim.get("score")
        lines.append(
            f"{dim.get('label')}({key}): {score if score is not None else '缺省'} "
            f"× {dim.get('weight')}" + ("  ⚠红线" if dim.get("red_line_failed") else "")
        )
    if result.get("missing_dims"):
        lines.append(f"缺省维度（权重归一）: {result['missing_dims']}")
    return "\n".join(lines)


async def _main_async(args) -> dict:
    return await score_daily_selection(args.date, horizon=args.horizon, save=args.save)


def main() -> int:
    parser = argparse.ArgumentParser(description="每日选股评分卡（T-P4-05b）")
    parser.add_argument(
        "--date", default=None, help="信号交易日 YYYY-MM-DD（缺省最新）"
    )
    parser.add_argument("--horizon", type=int, default=5, help="事后验证持有期 T+H")
    parser.add_argument("--save", action="store_true", help="写入 eval_scores 表")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(_main_async(args))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_scorecard(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
