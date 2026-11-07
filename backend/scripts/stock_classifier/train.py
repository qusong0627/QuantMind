"""全市场多分类（LightGBM）训练与样本外评估。

协议：
  train 2018-01-02~2024-05-31 / valid 2024-06-03~2025-06-30 /
  test 2025-07-01~2026-06-30 / oos 2026-07-01~2026-11-05
  —— 训练窗最后 126 个交易日做早停（purge h+2），valid/test/oos 不参与拟合。

用法（容器内）：
  cd /app && python3 -m backend.scripts.stock_classifier.train --horizon 5
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
import pyarrow.parquet as pq

from backend.scripts.stock_classifier.panel import (
    N_CLASSES_DEFAULT,
    class_mean_returns,
    rankdata_nan,
)

log = logging.getLogger("stock_classifier.train")

ROOT = Path("/data/stock_classifier")
SPLIT_BOUNDS = {
    "train": ("2018-01-02", "2024-05-31"),
    "valid": ("2024-06-03", "2025-06-30"),
    "test": ("2025-07-01", "2026-06-30"),
    "oos": ("2026-07-01", "2026-11-05"),
}
EARLY_STOP_DAYS = 126
PURGE_EXTRA = 2


def _params(n_classes: int, seed: int = 42, max_bin: int = 127) -> dict[str, Any]:
    return {
        "objective": "multiclass",
        "num_class": n_classes,
        "metric": "multi_logloss",
        "learning_rate": 0.06,
        "num_leaves": 31,
        "max_depth": 8,
        "min_data_in_leaf": 500,
        "feature_fraction": 0.6,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambda_l2": 20.0,
        "max_bin": max_bin,
        "num_threads": 20,
        "verbosity": -1,
        "seed": seed,
        "deterministic": True,
        "force_row_wise": True,
    }


def split_frame(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    start, end = SPLIT_BOUNDS[name]
    mask = (frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))
    return frame.loc[mask].reset_index(drop=True)


def load_split(
    horizon: int, features: list[str], name: str, variant: str = ""
) -> pd.DataFrame:
    """按切分惰性加载（避免把全样本同时驻留内存）。"""
    start, end = SPLIT_BOUNDS[name]
    frames: list[pd.DataFrame] = []
    for year in range(2018, 2027):
        path = ROOT / f"panel_h{horizon}{variant}" / f"year={year}" / "data.parquet"
        if not path.exists():
            continue
        frame = pq.read_table(path).to_pandas()
        mask = (frame["date"] >= pd.Timestamp(start)) & (
            frame["date"] <= pd.Timestamp(end)
        )
        if not mask.any():
            continue
        part = frame.loc[mask, ["date", "symbol", "fwd_ret", "y", *features]]
        frames.append(part.reset_index(drop=True))
        del frame
    if not frames:
        return pd.DataFrame(columns=["date", "symbol", "fwd_ret", "y", *features])
    return pd.concat(frames, ignore_index=True)


def purge_tail(frame: pd.DataFrame, horizon: int, n_days: int) -> pd.DataFrame:
    """丢弃最后 ``n_days`` 个交易日（标签窗口伸进下一段）。"""
    dates = np.sort(frame["date"].unique())
    if len(dates) <= n_days:
        return frame.iloc[0:0]
    cutoff = dates[-n_days]
    return frame.loc[frame["date"] < cutoff].reset_index(drop=True)


def daily_ic(
    frame: pd.DataFrame, score_col: str, ret_col: str = "fwd_ret"
) -> dict[str, float]:
    """逐日截面 Rank IC（score vs 实际收益）。"""
    ics: list[float] = []
    for _, group in frame.groupby("date", sort=True):
        s = group[score_col].to_numpy(float)
        r = group[ret_col].to_numpy(float)
        ok = np.isfinite(s) & np.isfinite(r)
        if ok.sum() < 30:
            continue
        sr = rankdata_nan(s[ok])
        rr = rankdata_nan(r[ok])
        sd = sr - sr.mean()
        rd = rr - rr.mean()
        denom = np.sqrt((sd * sd).sum() * (rd * rd).sum())
        if denom > 0:
            ics.append(float((sd * rd).sum() / denom))
    if not ics:
        return {}
    arr = np.asarray(ics)
    return {
        "n_days": int(len(arr)),
        "ic_mean": round(float(arr.mean()), 4),
        "ic_ir": round(float(arr.mean() / arr.std()), 3)
        if arr.std() > 0
        else float("nan"),
        "ic_pos_share": round(float((arr > 0).mean()), 3),
        "ic_std": round(float(arr.std()), 4),
    }


def decile_stats(
    frame: pd.DataFrame, score_col: str, n_bins: int = 10
) -> dict[str, Any]:
    """逐日按分数分箱，统计各箱平均收益与多空价差（非重叠采样另算）。"""
    per_day: list[np.ndarray] = []
    for _, group in frame.groupby("date", sort=True):
        if len(group) < 100:
            continue
        s = group[score_col].to_numpy(float)
        r = group["fwd_ret"].to_numpy(float)
        ok = np.isfinite(s) & np.isfinite(r)
        if ok.sum() < 100:
            continue
        s, r = s[ok], r[ok]
        ranks = pd.Series(s).rank(method="first").to_numpy()
        bins = np.clip((ranks - 1) * n_bins // len(s), 0, n_bins - 1).astype(int)
        means = np.array([r[bins == k].mean() for k in range(n_bins)])
        per_day.append(means)
    if not per_day:
        return {}
    arr = np.vstack(per_day)
    return {
        "n_days": int(len(arr)),
        "bin_means": [round(float(x), 5) for x in arr.mean(axis=0)],
        "top_minus_bottom": round(float(arr[:, -1].mean() - arr[:, 0].mean()), 5),
        "spread_daily_ir": round(
            float(
                np.mean(arr[:, -1] - arr[:, 0])
                / (np.std(arr[:, -1] - arr[:, 0]) + 1e-12)
                * np.sqrt(252 / 5)
            ),
            3,
        ),
    }


def classification_metrics(y: np.ndarray, proba: np.ndarray) -> dict[str, Any]:
    from sklearn.metrics import (
        accuracy_score,
        f1_score,
        log_loss,
        precision_recall_fscore_support,
    )

    pred = proba.argmax(axis=1)
    precision, recall, f1, support = precision_recall_fscore_support(
        y, pred, labels=list(range(proba.shape[1])), zero_division=0
    )
    return {
        "n": int(len(y)),
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "macro_f1": round(float(f1_score(y, pred, average="macro")), 4),
        "logloss": round(
            float(log_loss(y, proba, labels=list(range(proba.shape[1])))), 4
        ),
        "per_class": [
            {
                "class": k,
                "precision": round(float(precision[k]), 4),
                "recall": round(float(recall[k]), 4),
                "f1": round(float(f1[k]), 4),
                "n": int(support[k]),
            }
            for k in range(proba.shape[1])
        ],
    }


def run(
    horizon: int,
    n_classes: int,
    max_rounds: int,
    tag: str,
    variant: str = "",
    sample_frac: float = 1.0,
    max_bin: int = 127,
    drop_prefixes: tuple[str, ...] = (),
    seed: int = 42,
    model_suffix: str = "",
) -> Path:
    import lightgbm as lgb

    screen = json.loads(
        (ROOT / "screening" / f"screen_h{horizon}{variant}.json").read_text("utf-8")
    )
    features = [
        item["name"]
        for item in screen["selected"]
        if not any(item["name"].startswith(p) for p in drop_prefixes)
    ]
    log.info("h%d 特征 %d 个（筛选自 %d）", horizon, len(features), screen["n_factors"])

    parts = {
        name: load_split(horizon, features, name, variant)
        for name in ("valid", "test", "oos")
    }
    # 训练集尾部剔除 h+2 天：标签窗口不伸进 valid
    train = purge_tail(
        load_split(horizon, features, "train", variant), horizon, PURGE_EXTRA
    )
    if sample_frac < 1.0:
        train = (
            train.sample(frac=sample_frac, random_state=7)
            .sort_values("date")
            .reset_index(drop=True)
        )
    train_dates = np.sort(train["date"].unique())
    if len(train_dates) <= EARLY_STOP_DAYS + horizon:
        raise RuntimeError("训练窗太短，无法切内层早停集")
    val_start = train_dates[-EARLY_STOP_DAYS]
    inner_valid = train.loc[train["date"] >= val_start].reset_index(drop=True)
    inner_train = purge_tail(train.loc[train["date"] < val_start], horizon, PURGE_EXTRA)
    log.info(
        "样本：train=%d inner_valid=%d valid=%d test=%d oos=%d",
        len(inner_train),
        len(inner_valid),
        len(parts["valid"]),
        len(parts["test"]),
        len(parts["oos"]),
    )

    Xtr = inner_train[features].to_numpy("float32")
    ytr = inner_train["y"].to_numpy("int8")
    Xva = inner_valid[features].to_numpy("float32")
    yva = inner_valid["y"].to_numpy("int8")
    del inner_train, inner_valid

    dtrain = lgb.Dataset(Xtr, label=ytr, feature_name=features)
    dvalid = lgb.Dataset(Xva, label=yva, reference=dtrain)
    booster = lgb.train(
        _params(n_classes, seed=seed, max_bin=max_bin),
        dtrain,
        num_boost_round=max_rounds,
        valid_sets=[dvalid],
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(50)],
    )
    best_iter = int(booster.best_iteration or max_rounds)
    log.info("early stopping @ %d / %d", best_iter, max_rounds)

    # 全量训练窗重训
    Xfull = train[features].to_numpy("float32")
    yfull = train["y"].to_numpy("int8")
    final = lgb.train(
        _params(n_classes, seed=seed, max_bin=max_bin),
        lgb.Dataset(Xfull, label=yfull),
        num_boost_round=best_iter,
    )
    del Xtr, Xva, Xfull
    importance = dict(
        sorted(
            zip(features, final.feature_importance("gain"), strict=False),
            key=lambda kv: -kv[1],
        )
    )

    mu = class_mean_returns(
        train["fwd_ret"].to_numpy(float), train["y"].to_numpy(int), n_classes
    )
    log.info("分类均值收益：%s", np.round(mu, 5).tolist())

    report: dict[str, Any] = {
        "horizon": horizon,
        "n_classes": n_classes,
        "best_iteration": best_iter,
        "features": features,
        "class_mean_returns": [round(float(x), 5) for x in mu],
        "rows": {k: int(len(v)) for k, v in parts.items()},
        "splits": {},
        "importance_top": [
            [k, round(float(v), 1)] for k, v in list(importance.items())[:25]
        ],
    }
    for name in ("train", "valid", "test", "oos"):
        if name == "train":
            part = train.sample(n=min(len(train), 200_000), random_state=7)
        else:
            part = parts[name]
        if part.empty:
            continue
        proba = final.predict(part[features].to_numpy("float32"))
        expected = proba @ mu
        scored = part[["date", "symbol", "fwd_ret", "y"]].copy()
        scored["p_max"] = proba.max(axis=1)
        scored["exp_ret"] = expected
        scored["pred"] = proba.argmax(axis=1)
        report["splits"][name] = {
            "classification": classification_metrics(part["y"].to_numpy("int8"), proba),
            "ic": daily_ic(scored, "exp_ret"),
            "decile": decile_stats(scored, "exp_ret"),
            "selective": _selective(scored),
        }
        log.info(
            "  %-5s acc=%.3f macroF1=%.3f IC=%.4f IR=%.2f 多空=%.4f",
            name,
            report["splits"][name]["classification"]["accuracy"],
            report["splits"][name]["classification"]["macro_f1"],
            report["splits"][name]["ic"].get("ic_mean", float("nan")),
            report["splits"][name]["ic"].get("ic_ir", float("nan")),
            report["splits"][name]["decile"].get("top_minus_bottom", float("nan")),
        )

    out_dir = ROOT / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"lgb_h{horizon}{variant}{model_suffix}.pkl").open("wb") as fh:
        pickle.dump(
            {
                "booster": final,
                "features": features,
                "mu": mu,
                "horizon": horizon,
                "n_classes": n_classes,
            },
            fh,
        )
    (out_dir / f"metadata_h{horizon}{variant}{model_suffix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = (
        ROOT / "reports" / f"stock_multiclass_h{horizon}{variant}_{tag}_{stamp}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path = report_path.with_suffix(".md")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return report_path


def _selective(
    scored: pd.DataFrame, quantiles: tuple[float, ...] = (0.8, 0.9, 0.95)
) -> dict:
    """最高置信子集的命中率：p_max 超过当日分位阈值的样本。"""
    rows: dict[str, Any] = {}
    for q in quantiles:
        hits = 0
        total = 0
        for _, group in scored.groupby("date", sort=False):
            threshold = group["p_max"].quantile(q)
            mask = group["p_max"] >= threshold
            hits += int((group.loc[mask, "pred"] == group.loc[mask, "y"]).sum())
            total += int(mask.sum())
        rows[f"top_{int((1 - q) * 100)}pct"] = {
            "n": total,
            "hit_rate": round(hits / total, 4) if total else float("nan"),
        }
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# 全市场多分类模型 · T+{report['horizon']} · {report['n_classes']} 类",
        "",
        f"- 特征数：{len(report['features'])}；早停轮数：{report['best_iteration']}",
        f"- 分类均值收益（训练窗）：{[round(x, 4) for x in report['class_mean_returns']]}",
        f"- 样本：{report['rows']}",
        "",
        "| 切分 | n | 准确率 | macro-F1 | LogLoss | IC 均值 | ICIR | IC>0 占比 | 多空价差 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("train", "valid", "test", "oos"):
        if name not in report["splits"]:
            continue
        s = report["splits"][name]
        c, ic, dc = s["classification"], s["ic"], s["decile"]
        lines.append(
            f"| {name} | {c['n']} | {c['accuracy']:.3f} | {c['macro_f1']:.3f} | "
            f"{c['logloss']:.4f} | {ic.get('ic_mean', float('nan')):.4f} | "
            f"{ic.get('ic_ir', float('nan')):.2f} | {ic.get('ic_pos_share', float('nan')):.1%} | "
            f"{dc.get('top_minus_bottom', float('nan')):.4f} |"
        )
    lines += ["", "## 分箱平均收益（0=最差，9=最好）", ""]
    for name in ("valid", "test", "oos"):
        if name not in report["splits"]:
            continue
        bins = report["splits"][name]["decile"].get("bin_means", [])
        lines.append(f"- **{name}**：" + " / ".join(f"{x:+.4f}" for x in bins))
    lines += ["", "## 高置信子集命中率", ""]
    for name in ("valid", "test", "oos"):
        if name not in report["splits"]:
            continue
        sel = report["splits"][name].get("selective", {})
        lines.append(
            f"- **{name}**："
            + "，".join(
                f"{k} n={v['n']} 命中={v['hit_rate']:.3f}" for k, v in sel.items()
            )
        )
    lines += ["", "## Top20 特征（gain）", "", "```"]
    lines += [f"{name}: {value}" for name, value in report["importance_top"][:20]]
    lines += ["```", ""]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(description="全市场多分类 LightGBM")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--classes", type=int, default=N_CLASSES_DEFAULT)
    parser.add_argument("--rounds", type=int, default=300)
    parser.add_argument("--tag", default="v1")
    parser.add_argument("--variant", default="", help="面板/模型后缀，如 _l1")
    parser.add_argument("--sample-frac", type=float, default=1.0, help="训练行抽样比例")
    parser.add_argument("--max-bin", type=int, default=127)
    parser.add_argument(
        "--drop-prefixes",
        default="",
        help="剔除的特征前缀，逗号分隔（如 chip_,ind_,concept_）",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model-suffix", default="", help="模型文件名后缀（多种子时区分）")
    args = parser.parse_args()
    drop = tuple(p.strip() for p in args.drop_prefixes.split(",") if p.strip())
    path = run(
        args.horizon,
        args.classes,
        args.rounds,
        args.tag,
        args.variant,
        args.sample_frac,
        args.max_bin,
        drop,
        args.seed,
        args.model_suffix,
    )
    print(path)


if __name__ == "__main__":
    main()
