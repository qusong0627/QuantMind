"""因子评分卡（T-P4-05b-2，设计 §2.1）：因子库产物 → 五维评分。

数据源（实测在盘）：`<quantdb>/6_ml_datasets/<dataset>/report/`
- `factor_series.parquet`：逐日 × 逐因子（ic/coverage/turnover + ic_1..ic_20 衰减 + q1..q10）；
- `factor_report.json`：factors（快照指标）+ correlation（相关性矩阵）。

五维（权重 30/20/15/20/15）：预测力 |RankIC|/|ICIR|（红线 |ICIR|<0.2；负 IC 强因子标
direction=inverted，反转即可用而非判 0）· 稳定性 子样本 IC 方差 + **半衰期**（ic_H 衰减至
ic_1 一半，红线 <2 日）· 独立性 与存量因子最大相关（红线 >0.9，源 correlation.matrix）·
质量闸门 PFS/DH（未落库 → 如实缺省）· 覆盖均值（红线 <60%）。

用法：python backend/scripts/eval/factor_card.py --factor rsi_6 [--dataset alpha_library]
      python backend/scripts/eval/factor_card.py --top 20 --save     # 按 |IC| 排名批量
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.eval.factor_pfs import (  # noqa: E402
    compute_pfs_for,
    quality_gate_dim,
)
from backend.scripts.eval.factor_series import factor_series_payload  # noqa: E402
from backend.shared.eval_scoring import (  # noqa: E402
    DimensionScore,
    combine_dimension_scores,
    score_from_thresholds,
)
from backend.shared.eval_series import save_series  # noqa: E402

WEIGHTS = {
    "predictive": 30.0,
    "stability": 20.0,
    "independence": 15.0,
    "quality_gate": 20.0,
    "coverage": 15.0,
}
HORIZONS = (1, 2, 5, 10, 20)


# ── 纯函数 ──────────────────────────────────────────────────────────


def half_life_days(ic_by_horizon: dict[int, float]) -> float | None:
    """IC 衰减半衰期：|ic_H| ≤ |ic_1|/2 的最早 H（线性插值）；未衰减到一半 → None（>20 日）。

    缺失/NaN 视界跳过（**不得当 0 处理**——会把「未测」误判成「已衰减」）。
    """
    base = abs(float(ic_by_horizon.get(1) or 0.0))
    if base <= 0:
        return None
    prev_h, prev_v = 1, base
    for h in HORIZONS[1:]:
        raw = ic_by_horizon.get(h)
        if raw is None:
            continue
        v = abs(float(raw))
        if not math.isfinite(v):
            continue
        if v <= base / 2.0:
            if prev_v <= v:
                return float(h)
            # 插值：prev_v → v 之间穿过 base/2
            frac = (prev_v - base / 2.0) / max(1e-12, prev_v - v)
            return prev_h + frac * (h - prev_h)
        prev_h, prev_v = h, v
    return None


def score_factor(
    factor: str,
    series: dict[str, np.ndarray],
    report_factor: dict[str, Any] | None,
    max_corr: float | None,
    pfs_record: dict[str, Any] | None = None,
    *,
    pfs_note: str | None = None,
) -> dict[str, Any]:
    """单因子五维评分（series 为逐列 numpy 数组）。

    ``pfs_record`` 由 ``factor_pfs.compute_pfs_for`` 取好传入（本函数不读盘）；
    为 None 时质量闸门维如实缺省，``pfs_note`` 写明为什么没有。
    """
    ic = series.get("ic")
    dims: list[DimensionScore] = []

    # 预测力（RankIC/ICIR）：按强度取绝对值——负 IC 的强因子反转即可用，
    # 方向单独标注（direction=inverted），红线只看 |ICIR|（信号弱）。
    if ic is not None and len(ic) >= 20:
        mean_ic = float(np.nanmean(ic))
        std_ic = float(np.nanstd(ic, ddof=1))
        icir = mean_ic / std_ic if std_ic > 0 else 0.0
        abs_icir = abs(icir)
        ic_score = score_from_thresholds(
            abs(mean_ic),
            [(0.0, 0.0), (0.02, 40.0), (0.04, 65.0), (0.08, 85.0), (0.15, 100.0)],
        )
        icir_score = score_from_thresholds(
            abs_icir, [(0.0, 0.0), (0.2, 40.0), (0.5, 65.0), (1.0, 85.0), (2.0, 100.0)]
        )
        dims.append(
            DimensionScore(
                "predictive",
                "预测力",
                WEIGHTS["predictive"],
                round(0.6 * (ic_score or 0) + 0.4 * (icir_score or 0), 2),
                bool(abs_icir < 0.2),
                {
                    "mean_ic": round(mean_ic, 6),
                    "icir": round(icir, 4),
                    "direction": "inverted" if mean_ic < 0 else "normal",
                    "ic_score": ic_score,
                    "icir_score": icir_score,
                    "red_line": "|ICIR|<0.2（信号弱）" if abs_icir < 0.2 else None,
                },
            )
        )
    else:
        dims.append(
            DimensionScore(
                "predictive",
                "预测力",
                WEIGHTS["predictive"],
                None,
                False,
                {"insufficient": True, "note": "IC 序列 < 20"},
            )
        )

    # 稳定性（子样本 IC 方差 + 半衰期）
    stability_detail: dict[str, Any] = {}
    stab_components: list[float] = []
    red_stab = False
    if ic is not None and len(ic) >= 60:
        thirds = np.array_split(ic, 3)
        chunk_means = [float(np.nanmean(t)) for t in thirds if len(t)]
        sub_var = float(np.var(chunk_means, ddof=1)) if len(chunk_means) > 1 else 0.0
        var_score = score_from_thresholds(
            sub_var, [(0.0, 100.0), (0.0004, 80.0), (0.0016, 50.0), (0.01, 0.0)]
        )
        stability_detail.update(
            {
                "subsample_var": round(sub_var, 8),
                "chunk_means": [round(m, 6) for m in chunk_means],
            }
        )
        if var_score is not None:
            stab_components.append(var_score)
    ic_by_h = {}
    for h in HORIZONS:
        arr = series.get(f"ic_{h}")
        if arr is not None and len(arr):
            ic_by_h[h] = float(np.nanmean(arr))
    hl = half_life_days(ic_by_h) if ic_by_h else None
    stability_detail["half_life_days"] = round(hl, 2) if hl is not None else None
    stability_detail["ic_by_horizon"] = {k: round(v, 6) for k, v in ic_by_h.items()}
    if hl is not None:
        hl_score = score_from_thresholds(
            hl, [(1.0, 0.0), (2.0, 30.0), (5.0, 60.0), (10.0, 85.0), (20.0, 100.0)]
        )
        if hl_score is not None:
            stab_components.append(hl_score)
        if hl < 2.0:
            red_stab = True
            stability_detail["red_line"] = "半衰期<2 日"
    if stab_components:
        dims.append(
            DimensionScore(
                "stability",
                "稳定性",
                WEIGHTS["stability"],
                round(float(np.mean(stab_components)), 2),
                red_stab,
                stability_detail,
            )
        )
    else:
        dims.append(
            DimensionScore(
                "stability",
                "稳定性",
                WEIGHTS["stability"],
                None,
                False,
                {
                    **stability_detail,
                    "insufficient": True,
                    "note": "子样本/衰减数据不足",
                },
            )
        )

    # 独立性（与存量因子最大相关）
    if max_corr is not None:
        corr_score = score_from_thresholds(
            abs(max_corr),
            [(0.0, 100.0), (0.3, 90.0), (0.6, 70.0), (0.9, 25.0), (1.0, 0.0)],
        )
        dims.append(
            DimensionScore(
                "independence",
                "独立性",
                WEIGHTS["independence"],
                corr_score,
                bool(abs(max_corr) > 0.9),
                {
                    "max_corr": round(float(max_corr), 4),
                    "red_line": "相关性>0.9 去重" if abs(max_corr) > 0.9 else None,
                },
            )
        )
    else:
        dims.append(
            DimensionScore(
                "independence",
                "独立性",
                WEIGHTS["independence"],
                None,
                False,
                {"insufficient": True, "note": "相关性矩阵缺该因子"},
            )
        )

    # 质量闸门（PFS/DH）：优先用现算的 PFS（factor_pfs 侧车），退回 report 里的 DH
    record: dict[str, Any] | None = dict(pfs_record) if pfs_record else None
    dh = (report_factor or {}).get("diversity_gain") or (report_factor or {}).get("dh")
    if dh is not None:
        record = {**(record or {}), "dh": dh}
    dims.append(quality_gate_dim(record, note=pfs_note))

    # 覆盖
    cov = series.get("coverage")
    if cov is not None and len(cov):
        mean_cov = float(np.nanmean(cov))
        cov_score = score_from_thresholds(
            mean_cov, [(0.3, 0.0), (0.6, 40.0), (0.8, 70.0), (0.95, 90.0), (1.0, 100.0)]
        )
        dims.append(
            DimensionScore(
                "coverage",
                "覆盖",
                WEIGHTS["coverage"],
                cov_score,
                bool(mean_cov < 0.6),
                {
                    "mean_coverage": round(mean_cov, 4),
                    "red_line": "覆盖<60%" if mean_cov < 0.6 else None,
                },
            )
        )
    else:
        dims.append(
            DimensionScore(
                "coverage",
                "覆盖",
                WEIGHTS["coverage"],
                None,
                False,
                {"insufficient": True, "note": "覆盖序列缺失"},
            )
        )

    combined = combine_dimension_scores(dims)
    return {"object_type": "factor", "object_id": factor, **combined}


# ── IO ──────────────────────────────────────────────────────────────


def _load_dataset(dataset: str) -> tuple[Any, dict[str, Any]] | None:
    """→ (pandas DataFrame 因子序列, report json)，不可用返回 None。"""
    import pandas as pd

    from backend.services.engine.factor_report.datasets import dataset_dir

    base = dataset_dir(dataset) / "report"
    series_path = base / "factor_series.parquet"
    report_path = base / "factor_report.json"
    if not series_path.exists():
        return None
    df = pd.read_parquet(series_path)
    report = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.exists()
        else {}
    )
    return df, report


def _parse_correlation(raw: Any) -> tuple[list[str], list[list[Any]]]:
    """factor_report.json 的 correlation = {factors: [...], matrix: [[...]]} → 对齐校验后返回。"""
    if not isinstance(raw, dict):
        return [], []
    names = raw.get("factors") or []
    matrix = raw.get("matrix") or []
    if not names or len(matrix) != len(names):
        return [], []
    return [str(n) for n in names], matrix


def score_factors(
    dataset: str = "alpha_library",
    factor: str | None = None,
    top: int | None = None,
    *,
    with_pfs: bool = True,
) -> list[dict[str, Any]]:
    loaded = _load_dataset(dataset)
    if loaded is None:
        return [
            {
                "object_type": "factor",
                "object_id": factor or dataset,
                "error": f"因子报告不存在: {dataset}",
            }
        ]
    df, report = loaded
    corr_names, corr_matrix = _parse_correlation(report.get("correlation"))
    report_factors = {
        str(f.get("factor") or f.get("name")): f for f in (report.get("factors") or [])
    }

    if factor:
        names = [factor]
    else:
        # top N：按 |mean ic| 排名
        means = (
            df.groupby("factor", observed=True)["ic"]
            .mean()
            .abs()
            .sort_values(ascending=False)
        )
        names = [str(n) for n in means.index[: int(top or 20)]]

    pfs_map, pfs_evidence = _pfs_for(dataset, names, enabled=with_pfs)

    def _score_one(name: str) -> dict[str, Any]:
        sub = df[df["factor"] == name]
        if sub.empty:
            return {"object_type": "factor", "object_id": name, "error": "序列缺该因子"}
        series = {
            col: sub[col].to_numpy(dtype=float)
            for col in sub.columns
            if col != "factor"
        }
        max_corr = None
        try:
            idx = corr_names.index(name)
        except ValueError:
            idx = None
        if idx is not None:
            row = corr_matrix[idx]
            pairs = [
                abs(float(row[j]))
                for j in range(len(corr_names))
                if j != idx and row[j] is not None
            ]
            if pairs:
                max_corr = max(pairs)
        payload = factor_series_payload(sub)
        sidecar = save_series("factor", name, payload)
        return {
            **score_factor(
                name,
                series,
                report_factors.get(name),
                max_corr,
                pfs_map.get(name),
                pfs_note=pfs_evidence.get("note"),
            ),
            "inputs_version": {
                "weights": WEIGHTS,
                "dataset": dataset,
                "pfs": {**pfs_evidence, "enabled": bool(with_pfs)},
                # 长序列走 `data/eval_series/factor/<code>.json`（§1.6）：列表接口
                # 只带标量，序列按需取——写失败在这里如实留痕，不静默丢图
                "series_sidecar": {
                    "written": sidecar["written"],
                    "bytes": sidecar["bytes"],
                    "note": sidecar["note"],
                },
            },
        }

    return [_score_one(str(n)) for n in names]


def _pfs_for(
    dataset: str, factors: list[str], *, enabled: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """PFS 取数（异常隔离在评估侧，不因面板读不到而中断整批评分）。"""
    if not enabled:
        return {}, {"note": "PFS 已按调用方要求跳过（--no-pfs）"}
    try:
        return compute_pfs_for(dataset, [f for f in factors if f])
    except Exception as exc:  # noqa: BLE001 — 取数失败只影响质量闸门维
        return {}, {"note": f"PFS 取数失败：{type(exc).__name__}: {exc}"}


async def _save_many(
    results: list[dict[str, Any]], dataset: str = "alpha_library"
) -> None:
    from backend.shared.database_manager_v2 import close_database
    from backend.shared.eval_contract import save_eval_score

    try:
        for r in results:
            if r.get("error") or r.get("score") is None:
                continue
            await save_eval_score(
                object_type="factor",
                object_id=str(r["object_id"]),
                snapshot_date=date.today(),
                score=r.get("score"),
                grade=r.get("grade"),
                low_confidence=bool(r.get("low_confidence")),
                red_line_failed=r.get("red_line_failed") or [],
                dimensions=r.get("dimensions") or {},
                inputs_version=r.get("inputs_version")
                or {"weights": WEIGHTS, "dataset": dataset},
            )
    finally:
        await close_database()


def render_card(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"因子卡 {result.get('object_id')}：{result['error']}"
    lines = [
        f"因子评分卡 {result['object_id']}：{result.get('score')} 分 ｜评级 {result.get('grade')}"
        + ("（低置信 †）" if result.get("low_confidence") else ""),
    ]
    for key, dim in (result.get("dimensions") or {}).items():
        score = dim.get("score")
        lines.append(
            f"  {dim.get('label')}({key}): {score if score is not None else '缺省'}"
            + ("  ⚠红线" if dim.get("red_line_failed") else "")
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="因子评分卡（T-P4-05b）")
    parser.add_argument("--dataset", default="alpha_library")
    parser.add_argument("--factor", default=None)
    parser.add_argument(
        "--top", type=int, default=None, help="按 |IC| 排名批量评前 N 个"
    )
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--no-pfs",
        action="store_true",
        help="跳过错动保真（PFS）取数，质量闸门维将如实缺省",
    )
    args = parser.parse_args()
    results = score_factors(
        args.dataset, args.factor, args.top, with_pfs=not args.no_pfs
    )
    if args.save:
        asyncio.run(_save_many(results, args.dataset))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        for r in results:
            print(render_card(r))
    return 0 if results and not results[0].get("error") else 2


if __name__ == "__main__":
    raise SystemExit(main())
