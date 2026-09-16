"""模型评分卡（T-P4-05b-2，设计 §2.2）：模型产物 → 五维评分。

数据源：`models/production/<model_id>/metadata.json`——**两代 schema 兼容**：
新代 `metrics.{train,val,test}_{rank_ic,rank_icir}`；旧代 `performance_metrics.test.{mean_ic,icir}`
（alpha158 等存量模型，已实测归一）。
v1 覆盖率（诚实口径，缺维按剩余权重归一）：
- OOS 预测力（30）✅ 直接可算（test RankIC/ICIR，红线 IC<0.02 或 ICIR<0.3）；
- 分层能力（25）🟡 需预测+标签重算 → v1 insufficient；
- 稳健性（15）🟡 子样本分段 → v1 insufficient；
- 滚动健康（15）🟡 依赖 model_ic_monitor 历史接线 → v1 insufficient；
- 换手与成本（15）🟡 需持仓序列 → v1 insufficient。
ensemble 模型（无 metrics）→ 全部维度缺省、不评分（组件模型各自评分）。

用法：python backend/scripts/eval/model_card.py [--model-id model_qlib] [--save] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.eval_scoring import (  # noqa: E402
    DimensionScore,
    combine_dimension_scores,
    score_from_thresholds,
)

WEIGHTS = {
    "oos_predictive": 30.0,
    "stratification": 25.0,
    "robustness": 15.0,
    "rolling_health": 15.0,
    "turnover_cost": 15.0,
}
PRODUCTION_DIR = PROJECT_ROOT / "models" / "production"


def extract_oos_metrics(meta: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """OOS 指标归一（两代元数据）：新代 ``metrics.test_rank_ic/icir``，旧代
    ``performance_metrics.test.mean_ic/icir``。→ (metrics, source)。"""
    metrics = dict(meta.get("metrics") or {})
    if (
        metrics.get("test_rank_ic") is not None
        or metrics.get("test_rank_icir") is not None
    ):
        return metrics, "metrics"
    legacy = (meta.get("performance_metrics") or {}).get("test") or {}
    if legacy.get("mean_ic") is not None or legacy.get("icir") is not None:
        metrics.setdefault("test_rank_ic", legacy.get("mean_ic"))
        metrics.setdefault("test_rank_icir", legacy.get("icir"))
        return metrics, "performance_metrics.test"
    return metrics, "metrics" if metrics else "none"


def score_oos(metrics: dict[str, Any]) -> DimensionScore:
    """OOS 预测力：test RankIC / RankICIR（红线 IC<0.02 或 ICIR<0.3，设计 §2.2）。"""
    ic = metrics.get("test_rank_ic")
    icir = metrics.get("test_rank_icir")
    ic_score = score_from_thresholds(
        ic, [(0.0, 0.0), (0.02, 40.0), (0.04, 65.0), (0.08, 85.0), (0.15, 100.0)]
    )
    icir_score = score_from_thresholds(
        icir, [(0.0, 0.0), (0.3, 40.0), (0.5, 65.0), (1.0, 85.0), (2.0, 100.0)]
    )
    if ic_score is None and icir_score is None:
        return DimensionScore(
            "oos_predictive",
            "OOS 预测力",
            WEIGHTS["oos_predictive"],
            None,
            False,
            {
                "insufficient": True,
                "note": "metadata 无 OOS 指标（新代 metrics / 旧代 performance_metrics 均缺）",
            },
        )
    total = round(
        0.6 * (ic_score if ic_score is not None else 50.0)
        + 0.4 * (icir_score if icir_score is not None else 50.0),
        2,
    )
    red = bool(
        (ic is not None and float(ic) < 0.02)
        or (icir is not None and float(icir) < 0.3)
    )
    return DimensionScore(
        "oos_predictive",
        "OOS 预测力",
        WEIGHTS["oos_predictive"],
        total,
        red,
        {
            "test_rank_ic": ic,
            "test_rank_icir": icir,
            "ic_score": ic_score,
            "icir_score": icir_score,
            "red_line": "IC<0.02 或 ICIR<0.3" if red else None,
        },
    )


def _insufficient(key: str, label: str, note: str) -> DimensionScore:
    return DimensionScore(
        key, label, WEIGHTS[key], None, False, {"insufficient": True, "note": note}
    )


def list_production_models(*, limit: int = 50) -> list[str]:
    """生产模型目录清单（跳过 ``.bak`` 备份；无 metadata.json 的目录自然不纳入）。"""
    if not PRODUCTION_DIR.is_dir():
        return []
    out: list[str] = []
    for meta in sorted(PRODUCTION_DIR.glob("*/metadata.json")):
        name = meta.parent.name
        if ".bak" in name:
            continue
        out.append(name)
        if len(out) >= max(1, int(limit)):
            break
    return out


def score_model(model_id: str) -> dict[str, Any]:
    meta_path = PRODUCTION_DIR / model_id / "metadata.json"
    if not meta_path.is_file():
        return {
            "object_type": "model",
            "object_id": model_id,
            "error": f"metadata 不存在: {meta_path}",
        }
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    metrics, metrics_source = extract_oos_metrics(meta)
    dims = [
        score_oos(metrics),
        _insufficient("stratification", "分层能力", "需预测+标签重算（v1 缺省）"),
        _insufficient("robustness", "稳健性", "子样本分段回填未接线（v1 缺省）"),
        _insufficient(
            "rolling_health", "滚动健康", "model_ic_monitor 历史未接线（v1 缺省）"
        ),
        _insufficient("turnover_cost", "换手与成本", "需持仓序列（v1 缺省）"),
    ]
    combined = combine_dimension_scores(dims)
    return {
        "object_type": "model",
        "object_id": model_id,
        "run_id": meta.get("run_id"),
        "model_type": meta.get("model_type"),
        "train_window": [meta.get("train_start"), meta.get("train_end")],
        "test_window": [meta.get("test_start"), meta.get("test_end")],
        "inputs_version": {
            "model_id": model_id,
            "run_id": meta.get("run_id"),
            "weights": WEIGHTS,
            "source": "models/production metadata.metrics",
            "metrics_source": metrics_source,
        },
        **combined,
    }


def render_card(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"模型卡 {result['object_id']}：{result['error']}"
    lines = [
        f"模型评分卡 {result['object_id']}：{result.get('score')} 分 ｜评级 {result.get('grade')}"
        + ("（低置信 †）" if result.get("low_confidence") else ""),
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


async def _save(result: dict[str, Any]) -> bool:
    from datetime import date

    from backend.shared.eval_contract import save_eval_score

    return await save_eval_score(
        object_type="model",
        object_id=str(result["object_id"]),
        snapshot_date=date.today(),
        score=result.get("score"),
        grade=result.get("grade"),
        low_confidence=bool(result.get("low_confidence")),
        red_line_failed=result.get("red_line_failed") or [],
        dimensions=result.get("dimensions") or {},
        inputs_version=result.get("inputs_version") or {},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="模型评分卡（T-P4-05b）")
    parser.add_argument("--model-id", default="model_qlib")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = score_model(args.model_id)
    if args.save and not result.get("error"):

        async def _run():
            from backend.shared.database_manager_v2 import close_database

            try:
                return await _save(result)
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
