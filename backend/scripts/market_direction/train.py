"""训练 / 评估大盘方向模型（T+1 / T+3 / T+5）。

协议（预注册，先看训练+验证再读测试）：
- 切分固定：train 2018-01~2024-05 / valid 2024-06~2025-06 /
  test 2025-07~2026-06 / oos 2026-07~2026-11；
- 模型选择与概率校准只用 valid；test/oos 只读一次；
- 三族模型 LGB / CatBoost / Logistic + 等权集成；
- 消融：base（指数技术+广度+两融+宏观+基差）/ factor（L1-L2 截面聚合）/ full。

用法（容器内）：
  cd /app && python3 -m backend.scripts.market_direction.train \
      --index 000300.SH --horizons 1,3,5 --feature-set full
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.scripts.market_direction.feature_spec import (
    ALL_FEATURE_COLUMNS,
    BASE_FEATURE_COLUMNS,
    BASIS_COLUMNS,
    COMPACT_FEATURES,
    FACTOR_FEATURE_COLUMNS,
    MACRO_COLUMNS,
    TARGET_INDICES,
)
from backend.scripts.market_direction.features import (
    SPLIT_BOUNDS,
    SPLIT_ORDER,
    purge_overlapping_labels,
)
from backend.scripts.market_direction.modeling import (
    MODEL_NAMES,
    evaluate_binary,
    fit_models,
    predict,
    prior_shift_probs,
    selective_table,
    stable_features,
    strategy_eval,
)

log = logging.getLogger("market_direction.train")

DEFAULT_DATA = "/data/market_direction/features/market_direction_features.parquet"
DEFAULT_OUT = "/data/market_direction"
PURGE_EMBARGO = 2


def _feature_columns(feature_set: str, data: pd.DataFrame) -> list[str]:
    if feature_set == "base":
        wanted = BASE_FEATURE_COLUMNS
    elif feature_set == "factor":
        wanted = FACTOR_FEATURE_COLUMNS
    elif feature_set == "compact":
        wanted = COMPACT_FEATURES
    else:
        wanted = ALL_FEATURE_COLUMNS
    return [c for c in wanted if c in data.columns]


def _feature_group(name: str) -> str:
    if name.startswith("fa_"):
        return "factor_agg"
    if name.startswith("idx_"):
        return "index_tech"
    if name.startswith("mkt_"):
        return "breadth_all"
    if name.startswith("lc_"):
        return "breadth_largecap"
    if name.startswith("mg_"):
        return "margin"
    if name in MACRO_COLUMNS:
        return "macro"
    if name in BASIS_COLUMNS:
        return "basis"
    return "other"


def _agg_kind(name: str) -> str:
    for kind in ("_mean_pct", "_capw_pct", "_ls_pct", "_std_pct"):
        if name.endswith(kind):
            return kind.strip("_").replace("_pct", "")
    return "other"


def _group_importance(importance: dict[str, float]) -> dict[str, float]:
    total = float(sum(importance.values())) or 1.0
    grouped: dict[str, float] = {}
    for name, value in importance.items():
        key = _feature_group(name)
        grouped[key] = grouped.get(key, 0.0) + float(value)
    return {
        k: round(v / total, 4)
        for k, v in sorted(grouped.items(), key=lambda kv: -kv[1])
    }


def _agg_kind_importance(importance: dict[str, float]) -> dict[str, float]:
    factor = {k: v for k, v in importance.items() if k.startswith("fa_")}
    total = float(sum(factor.values())) or 1.0
    grouped: dict[str, float] = {}
    for name, value in factor.items():
        key = _agg_kind(name)
        grouped[key] = grouped.get(key, 0.0) + float(value)
    return {
        k: round(v / total, 4)
        for k, v in sorted(grouped.items(), key=lambda kv: -kv[1])
    }


def _prepare_index(
    data: pd.DataFrame, index_code: str, horizon: int, features: list[str]
) -> dict[str, pd.DataFrame]:
    frame = data[data["index_code"] == index_code].sort_values("trade_date")
    frame = frame.reset_index(drop=True)
    y = frame[f"y_{horizon}"].to_numpy(float)
    split = frame["split"].to_numpy(object)
    keep = purge_overlapping_labels(split, horizon, embargo_extra=PURGE_EMBARGO)

    # 逐日「当前基础上涨率」：只用 t-h 及以前已实现的标签（PIT）
    base_now = (
        pd.Series(y).shift(horizon).rolling(252, min_periods=60).mean().to_numpy()
    )

    out: dict[str, pd.DataFrame] = {}
    for name in SPLIT_ORDER:
        mask = (split == name) & keep & np.isfinite(y)
        part = frame.loc[mask, ["trade_date", *features]].copy()
        part["y"] = y[mask]
        part["base_rate_now"] = base_now[mask]
        part[f"fwd_ret_{horizon}"] = frame.loc[mask, f"fwd_ret_{horizon}"].to_numpy()
        out[name] = part.reset_index(drop=True)
    return out


def run_one(
    data: pd.DataFrame,
    index_code: str,
    horizon: int,
    feature_set: str,
    models: tuple[str, ...],
    selection: str = "none",
) -> dict[str, Any]:
    features = _feature_columns(feature_set, data)
    parts = _prepare_index(data, index_code, horizon, features)
    train, valid = parts["train"], parts["valid"]
    if len(train) < 400 or len(valid) < 100:
        raise RuntimeError(f"{index_code} h={horizon} 样本不足")

    selected = features
    if selection == "stable":
        selected = stable_features(
            train[features], train["y"].to_numpy(float), max_features=20
        )
        if len(selected) < 3:
            selected = features
    fitted = fit_models(train[selected], train["y"].to_numpy(float), horizon, models)

    from backend.scripts.market_regime.calibration import apply_platt, fit_platt

    result: dict[str, Any] = {
        "index_code": index_code,
        "index_name": TARGET_INDICES.get(index_code, index_code),
        "horizon": horizon,
        "feature_set": feature_set,
        "feature_count": len(selected),
        "selection": selection,
        "selected_features": selected if selection != "none" else [],
        "rows": {k: int(len(v)) for k, v in parts.items()},
        "models": {},
    }
    for name in models:
        if name not in fitted:
            continue
        model = fitted[name]
        raw = {split: predict(model, parts[split][selected]) for split in SPLIT_ORDER}
        params = fit_platt(raw["valid"], valid["y"].to_numpy(float))
        calibrated = {split: apply_platt(raw[split], params) for split in SPLIT_ORDER}

        source_rate = float(np.mean(valid["y"].to_numpy(float)))
        prior = {
            split: prior_shift_probs(
                calibrated[split],
                source_rate,
                parts[split]["base_rate_now"].fillna(source_rate).to_numpy(float),
            )
            for split in SPLIT_ORDER
        }

        entry: dict[str, Any] = {
            "best_iteration": model.best_iteration,
            "platt": [round(float(params[0]), 5), round(float(params[1]), 5)],
            "splits": {},
        }
        for split in SPLIT_ORDER:
            y = parts[split]["y"].to_numpy(float)
            entry["splits"][split] = {
                "raw": evaluate_binary(y, raw[split], horizon, "raw"),
                "calibrated": evaluate_binary(
                    y, calibrated[split], horizon, "calibrated"
                ),
                "prior_adjusted": evaluate_binary(
                    y, prior[split], horizon, "prior_adjusted"
                ),
            }
        entry["selective"] = {
            split: selective_table(parts[split]["y"].to_numpy(float), prior[split])
            for split in ("test", "oos")
        }
        entry["strategy"] = {
            split: [
                strategy_eval(
                    parts[split][f"fwd_ret_{horizon}"].to_numpy(float),
                    prior[split],
                    horizon,
                    threshold=threshold,
                )
                for threshold in (0.5, 0.55)
            ]
            for split in ("valid", "test", "oos")
        }
        top = sorted(model.feature_importance.items(), key=lambda kv: -float(kv[1]))[
            :20
        ]
        entry["top_features"] = [[k, round(float(v), 2)] for k, v in top]
        entry["group_importance"] = _group_importance(model.feature_importance)
        entry["agg_kind_importance"] = _agg_kind_importance(model.feature_importance)
        result["models"][name] = entry
    result["predictions"] = {
        name: {
            split: {
                "trade_date": parts[split]["trade_date"]
                .dt.strftime("%Y-%m-%d")
                .tolist(),
                "y": parts[split]["y"].tolist(),
                "p": predict(fitted[name], parts[split][selected]).round(5).tolist(),
            }
            for split in SPLIT_ORDER
        }
        for name in fitted
    }
    result["_fitted"] = fitted
    result["_features"] = selected
    return result


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and not np.isfinite(value):
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _signal_table(result: dict[str, Any]) -> str:
    lines = [
        "| 模型 | 切分 | n | 基准上涨率 | AUC | AUC 95%CI | 准确率 | 准确率(先验修正) | 平衡准确率 | Brier skill | ECE(校准) | ECE(先验修正) |",
        "|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model_name, entry in result["models"].items():
        for split in SPLIT_ORDER:
            cal = entry["splits"][split]["calibrated"]
            pri = entry["splits"][split]["prior_adjusted"]
            ci = cal.get("auc_ci") or [np.nan, np.nan]
            lines.append(
                "| {m} | {s} | {n} | {base} | {auc} | [{lo}, {hi}] | {acc} | {pacc} | "
                "{bacc} | {skill} | {ece} | {pece} |".format(
                    m=model_name,
                    s=split,
                    n=cal.get("n", 0),
                    base=_fmt(cal.get("base_rate")),
                    auc=_fmt(cal.get("auc")),
                    lo=_fmt(ci[0]),
                    hi=_fmt(ci[1]),
                    acc=_fmt(cal.get("acc")),
                    pacc=_fmt(pri.get("acc")),
                    bacc=_fmt(cal.get("balanced_acc")),
                    skill=_fmt(cal.get("brier_skill")),
                    ece=_fmt(cal.get("ece")),
                    pece=_fmt(pri.get("ece")),
                )
            )
    return "\n".join(lines)


def _selective_markdown(result: dict[str, Any], model_name: str) -> str:
    entry = result["models"][model_name]
    lines = [
        "| 切分 | 方向 | 阈值 | n | 占比 | 命中率 | 95%CI |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for split in ("test", "oos"):
        for row in entry["selective"].get(split, []):
            lines.append(
                f"| {split} | {row['side']} | {row['threshold']:.2f} | {row['n']} | "
                f"{row['share']:.1%} | {row['hit_rate']:.3f} | "
                f"[{row['wilson_lo']:.3f}, {row['wilson_hi']:.3f}] |"
            )
    return "\n".join(lines)


def _strategy_markdown(result: dict[str, Any], model_name: str) -> str:
    entry = result["models"][model_name]
    lines = [
        "| 切分 | 阈值 | 策略收益 | 策略CAGR | 策略MaxDD | 策略Sharpe | 暴露 | 买入持有收益 | 买入持有MaxDD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for split in ("valid", "test", "oos"):
        for row in entry["strategy"].get(split, []):
            s, b = row["strategy"], row["buy_hold"]
            lines.append(
                f"| {split} | {row['threshold']:.2f} | {s.get('total_return', 0):.2%} | "
                f"{s.get('cagr', 0):.2%} | {s.get('max_drawdown', 0):.2%} | "
                f"{s.get('sharpe', 0):.2f} | {s.get('exposure', 0):.1%} | "
                f"{b.get('total_return', 0):.2%} | {b.get('max_drawdown', 0):.2%} |"
            )
    return "\n".join(lines)


def _ablation_markdown(ablation: dict[str, Any]) -> str:
    lines = [
        "| 特征集 | 模型 | h | valid AUC | test AUC | oos AUC | test 平衡准确率 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for key, payload in sorted(ablation.items()):
        feature_set, model_name, horizon = key.split("|")
        splits = payload["splits"]
        lines.append(
            f"| {feature_set} | {model_name} | {horizon} | "
            f"{_fmt(splits['valid']['auc'])} | {_fmt(splits['test']['auc'])} | "
            f"{_fmt(splits['oos']['auc'])} | {_fmt(splits['test']['balanced_acc'])} |"
        )
    return "\n".join(lines)


def write_report(
    results: list[dict[str, Any]],
    ablation: dict[str, Any],
    out_dir: Path,
    tag: str,
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    json_path = out_dir / "reports" / f"market_direction_{tag}_{stamp}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)

    compact = []
    for result in results:
        item = {k: v for k, v in result.items() if not k.startswith("_")}
        compact.append(item)
    json_path.write_text(
        json.dumps(
            {"results": compact, "ablation": ablation}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )

    lines: list[str] = [
        "# 大盘方向模型 · 训练与样本外评估",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 特征表：`{DEFAULT_DATA}`",
        "- 切分：" + "；".join(f"{k} {v[0]}~{v[1]}" for k, v in SPLIT_BOUNDS.items()),
        "- 标签：`fwd_ret_h = close[T+h]/close[T]-1`，`y=1 if >0`；"
        "另报次日开盘进场口径 `fwd_ret_open_h`（见 JSON）。",
        "- 模型选择与 Platt 校准只用 valid；test/oos 不参与任何拟合。",
        "",
    ]
    for result in results:
        models_present = list(result["models"])
        if not models_present:
            continue
        best_model = max(
            models_present,
            key=lambda m: result["models"][m]["splits"]["valid"]["calibrated"].get(
                "auc", -1
            ),
        )
        lines.extend(
            [
                f"## {result['index_name']}（{result['index_code']}）· T+{result['horizon']}",
                "",
                f"- 特征数：{result['feature_count']}；样本："
                + "，".join(f"{k}={v}" for k, v in result["rows"].items()),
                f"- 按 valid AUC 选中的模型：**{best_model}**"
                f"（best_iteration={result['models'][best_model]['best_iteration']}）",
                "",
                _signal_table(result),
                "",
                f"### 高置信子集命中率（{best_model}）",
                "",
                _selective_markdown(result, best_model),
                "",
                f"### Long/Flat 策略（{best_model}，5bp/次切换，非重叠采样）",
                "",
                _strategy_markdown(result, best_model),
                "",
                f"### 特征重要性 Top10（{best_model}，gain）",
                "",
                "```",
                "\n".join(
                    f"{name}: {value:.1f}"
                    for name, value in result["models"][best_model]["top_features"][:10]
                ),
                "```",
                "",
                f"- 分组重要性：{result['models'][best_model]['group_importance']}",
                f"- 因子聚合方式重要性：{result['models'][best_model]['agg_kind_importance']}",
                "",
            ]
        )
    if ablation:
        lines.extend(
            [
                "## 消融：base vs factor vs full（LightGBM）",
                "",
                _ablation_markdown(ablation),
                "",
            ]
        )

    md_path = out_dir / "reports" / f"market_direction_{tag}_{stamp}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="训练大盘方向模型")
    parser.add_argument("--data", default=DEFAULT_DATA)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--index", default=",".join(TARGET_INDICES))
    parser.add_argument("--horizons", default="1,3,5")
    parser.add_argument(
        "--feature-set",
        default="full",
        choices=("full", "base", "factor", "compact"),
    )
    parser.add_argument("--models", default=",".join(MODEL_NAMES))
    parser.add_argument("--selection", default="none", choices=("none", "stable"))
    parser.add_argument(
        "--ablation", action="store_true", help="额外跑 base/factor 消融"
    )
    parser.add_argument("--tag", default="full")
    args = parser.parse_args()

    data = pd.read_parquet(args.data)
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    out_dir = Path(args.out)
    indices = [c.strip() for c in args.index.split(",") if c.strip()]
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    models = tuple(m.strip() for m in args.models.split(",") if m.strip())

    results: list[dict[str, Any]] = []
    for index_code in indices:
        for horizon in horizons:
            log.info(
                "训练 %s T+%d feature_set=%s", index_code, horizon, args.feature_set
            )
            result = run_one(
                data, index_code, horizon, args.feature_set, models, args.selection
            )
            results.append(result)
            for model_name, entry in result["models"].items():
                splits = entry["splits"]
                log.info(
                    "  %-9s valid AUC=%.3f test AUC=%.3f oos AUC=%.3f",
                    model_name,
                    splits["valid"]["calibrated"].get("auc", float("nan")),
                    splits["test"]["calibrated"].get("auc", float("nan")),
                    splits["oos"]["calibrated"].get("auc", float("nan")),
                )
            # 保存模型
            model_dir = out_dir / "models" / index_code / f"h{horizon}"
            model_dir.mkdir(parents=True, exist_ok=True)
            for name, fitted in result["_fitted"].items():
                with (model_dir / f"{name}.pkl").open("wb") as fh:
                    pickle.dump(
                        {
                            "model": fitted,
                            "features": result["_features"],
                            "feature_set": args.feature_set,
                            "horizon": horizon,
                            "index_code": index_code,
                        },
                        fh,
                    )
            (model_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "index_code": index_code,
                        "horizon": horizon,
                        "feature_set": args.feature_set,
                        "features": result["_features"],
                        "splits": dict(result["rows"]),
                        "metrics": {
                            name: {
                                split: entry["splits"][split]["calibrated"]
                                for split in SPLIT_ORDER
                            }
                            for name, entry in result["models"].items()
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            result.pop("_fitted", None)

    ablation: dict[str, Any] = {}
    if args.ablation:
        for feature_set in ("base", "factor"):
            for index_code in indices:
                for horizon in horizons:
                    log.info("消融 %s %s T+%d", feature_set, index_code, horizon)
                    res = run_one(
                        data,
                        index_code,
                        horizon,
                        feature_set,
                        ("lightgbm",),
                        args.selection,
                    )
                    payload = res["models"]["lightgbm"]["splits"]
                    ablation[f"{feature_set}|lightgbm|{horizon}"] = {
                        "index_code": index_code,
                        "splits": {
                            split: payload[split]["calibrated"] for split in SPLIT_ORDER
                        },
                    }
                    res.pop("_fitted", None)

    report_path = write_report(results, ablation, out_dir, args.tag)
    log.info("报告写出：%s", report_path)
    print(report_path)


if __name__ == "__main__":
    main()
