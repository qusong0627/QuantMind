"""Kronos 特征增量对照训练（LGB 增益检验）。

问题：把 Kronos 的预测特征加进现有因子面板，样本外排序能力有没有提升？
做法：同一批样本、同一套超参、同一个随机种子，只改「有没有 k_* 列」。

协议（默认）：
  train 2024-06-03~2025-09-30 / valid 2025-10-01~2025-12-31 / test 2026-01-01~2026-06-30
  —— train 尾部按 horizon+2 天 purge，valid 只做早停，test 完全不参与拟合。

标签：逐日截面 5 等分分位（与 ``stock_classifier`` 同口径），模型分 = 期望类别号。
上证口径：T 日收盘涨停的样本剔除（买不进）。

用法（容器内）：
  python3 -m backend.scripts.kronos_ashare.feature_ablation \
    --features /data/kronos_ashare/features/base_csi300_daily.parquet \
    --horizon 5 --output /data/kronos_ashare/ablation_h5.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.stock_classifier.panel import (
    N_CLASSES_DEFAULT,
    assign_quantile_classes,
    is_limit_up,
    rankdata_nan,
)
from backend.shared.stock_utils import StockCodeUtil

log = logging.getLogger("kronos_ashare.feature_ablation")

FACTOR_DIR = Path("/data/quantdb/6_ml_datasets/l1_l2_factors")
PRICE_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
KRONOS_COLUMNS = (
    "k_score_mean",
    "k_score_end",
    "k_path_vol",
    "k_path_range",
    "k_path_slope",
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kronos 特征 LGB 增量对照")
    p.add_argument("--features", required=True)
    p.add_argument("--factor-dir", default=str(FACTOR_DIR))
    p.add_argument("--start", default="2024-06-03")
    p.add_argument("--end", default="2026-06-30")
    p.add_argument("--train-end", default="2025-09-30")
    p.add_argument("--valid-end", default="2025-12-31")
    p.add_argument("--horizon", type=int, default=5)
    p.add_argument("--n-classes", type=int, default=N_CLASSES_DEFAULT)
    p.add_argument("--min-amount-wan", type=float, default=1000.0)
    p.add_argument("--rounds", type=int, default=400)
    p.add_argument("--early-stop", type=int, default=40)
    p.add_argument("--threads", type=int, default=16)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--shuffle-kronos",
        action="store_true",
        help="对照组：在每个交易日内打乱 Kronos 特征（应让增益归零，用于排除管道假象）",
    )
    p.add_argument("--output", default=None)
    return p.parse_args(argv)


def load_factor_panel(
    factor_dir: Path, start: str, end: str, symbols: set[str]
) -> pd.DataFrame:
    """读取区间内因子分区，只保留 Kronos 覆盖的标的。"""
    start_key = pd.Timestamp(start).strftime("%Y%m%d")
    end_key = pd.Timestamp(end).strftime("%Y%m%d")
    frames = []
    for part in sorted(factor_dir.glob("dt=*/data.parquet")):
        key = part.parent.name.split("=", 1)[1]
        if key < start_key or key > end_key:
            continue
        frame = pq.read_table(part).to_pandas()
        frame = frame[frame["symbol"].isin(symbols)]
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise RuntimeError(f"没有可用因子分区：{start}~{end}")
    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    return panel.sort_values(["symbol", "date"]).reset_index(drop=True)


def add_labels(panel: pd.DataFrame, horizon: int, min_amount_wan: float) -> pd.DataFrame:
    """加前视收益、涨停剔除与逐日分位标签。"""
    panel = panel.copy()
    close = panel["close"].astype(float)
    prev_close = panel.groupby("symbol")["close"].shift(1)
    day_ret = close / prev_close - 1.0
    panel["limit_up"] = is_limit_up(panel["symbol"], day_ret)

    future = panel.groupby("symbol")["close"].shift(-horizon).astype(float)
    panel["fwd_ret"] = future / close - 1.0

    tradable = (
        (~panel["limit_up"].fillna(True))
        & np.isfinite(panel["fwd_ret"])
        & (panel["amount"].astype(float) >= min_amount_wan)
    )
    panel["tradable"] = tradable
    labels = np.full(len(panel), -1, dtype=np.int8)
    for _, idx in panel.groupby("date").groups.items():
        mask = panel.loc[idx, "tradable"].to_numpy(bool)
        y = panel.loc[idx, "fwd_ret"].to_numpy(float)
        y_masked = np.where(mask, y, np.nan)
        labels[panel.index.get_indexer(idx)] = assign_quantile_classes(y_masked)
    panel["y"] = labels
    return panel


def _params(n_classes: int, threads: int, seed: int = 42) -> dict:
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
        "max_bin": 127,
        "num_threads": threads,
        "verbosity": -1,
        "seed": seed,
        "deterministic": True,
        "force_row_wise": True,
    }


def _rank_ic(scores: np.ndarray, fwd: np.ndarray) -> float:
    rx = rankdata_nan(scores)
    ry = rankdata_nan(fwd)
    ok = np.isfinite(rx) & np.isfinite(ry)
    if ok.sum() < 20:
        return float("nan")
    xd = rx[ok] - rx[ok].mean()
    yd = ry[ok] - ry[ok].mean()
    denom = float(np.sqrt((xd * xd).sum() * (yd * yd).sum()))
    return float((xd * yd).sum() / denom) if denom > 0 else float("nan")


def evaluate(frame: pd.DataFrame, scores: np.ndarray, topk: int = 20) -> dict:
    """逐日 RankIC / TopK 超额，汇总为均值与 ICIR；同时返回逐日 IC 供配对检验。"""
    out = frame[["date", "fwd_ret"]].copy()
    out["score"] = scores
    ics, excesses = [], []
    per_date_ic: dict[str, float] = {}
    for date, day in out.groupby("date"):
        day = day.dropna(subset=["score", "fwd_ret"])
        if len(day) < 20:
            continue
        ic = _rank_ic(day["score"].to_numpy(float), day["fwd_ret"].to_numpy(float))
        ics.append(ic)
        per_date_ic[str(pd.Timestamp(date).date())] = ic
        k = max(1, min(topk, len(day)))
        top = day.nlargest(k, "score")
        excesses.append(float(top["fwd_ret"].mean() - day["fwd_ret"].mean()))
    ic = np.array([v for v in ics if np.isfinite(v)], dtype=float)
    ex = np.array([v for v in excesses if np.isfinite(v)], dtype=float)
    std = float(ic.std(ddof=1)) if len(ic) > 1 else float("nan")
    return {
        "n_dates": float(len(ic)),
        "rank_ic_mean": float(ic.mean()) if len(ic) else float("nan"),
        "rank_ic_std": std,
        "rank_ic_icir": (
            float(ic.mean() / std * np.sqrt(len(ic))) if std and np.isfinite(std) else float("nan")
        ),
        "topk_excess_mean": float(ex.mean()) if len(ex) else float("nan"),
        "topk_hit": float((ex > 0).mean()) if len(ex) else float("nan"),
        "per_date_ic": per_date_ic,
    }


def _paired_delta(base_ic: dict[str, float], other_ic: dict[str, float]) -> dict[str, float]:
    """同一批日期上逐日 IC 相减，给出均值与 t 值（配对检验）。"""
    common = sorted(set(base_ic) & set(other_ic))
    deltas = np.array(
        [other_ic[d] - base_ic[d] for d in common], dtype=float
    )
    deltas = deltas[np.isfinite(deltas)]
    if len(deltas) < 2:
        return {"n": float(len(deltas)), "mean": float("nan"), "std": float("nan"), "t": float("nan")}
    std = float(deltas.std(ddof=1))
    return {
        "n": float(len(deltas)),
        "mean": float(deltas.mean()),
        "std": std,
        "t": float(deltas.mean() / std * np.sqrt(len(deltas))) if std > 0 else float("nan"),
        "hit": float((deltas > 0).mean()),
    }


def run_arm(
    train: pd.DataFrame,
    valid: pd.DataFrame,
    test: pd.DataFrame,
    features: list[str],
    args: argparse.Namespace,
) -> dict:
    """训练一个 arm 并返回测试集评估。"""
    import lightgbm as lgb

    dtrain = lgb.Dataset(train[features].to_numpy("float32"), label=train["y"].to_numpy("int8"))
    dvalid = lgb.Dataset(valid[features].to_numpy("float32"), label=valid["y"].to_numpy("int8"))
    booster = lgb.train(
        _params(args.n_classes, args.threads, args.seed),
        dtrain,
        num_boost_round=args.rounds,
        valid_sets=[dvalid],
        callbacks=[lgb.early_stopping(args.early_stop, verbose=False), lgb.log_evaluation(0)],
    )
    proba = booster.predict(test[features].to_numpy("float32"))
    score = (proba * np.arange(args.n_classes)).sum(axis=1)
    metrics = evaluate(test, score)
    metrics["best_iter"] = float(booster.best_iteration or args.rounds)
    metrics["n_features"] = float(len(features))
    gain = booster.feature_importance("gain")
    order = np.argsort(gain)[::-1][:15]
    metrics["top_gain"] = [
        {"feature": features[i], "gain": float(gain[i])} for i in order
    ]
    return metrics


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    kronos = pd.read_parquet(args.features)
    kronos["date"] = pd.to_datetime(kronos["date"])
    # Qlib instruments 用小写前缀式（sh600000），QuantDB 因子面板用后缀式（600000.SH）
    kronos["symbol"] = kronos["symbol"].map(StockCodeUtil.to_suffix)
    kronos = kronos[
        (kronos["date"] >= pd.Timestamp(args.start)) & (kronos["date"] <= pd.Timestamp(args.end))
    ]
    if kronos.empty:
        log.error("Kronos 特征在区间内为空：%s", args.features)
        return 2
    symbols = set(kronos["symbol"].unique())
    log.info("Kronos 特征：%d 行 / %d 只 / %d 个交易日", len(kronos), len(symbols), kronos["date"].nunique())

    panel = load_factor_panel(Path(args.factor_dir), args.start, args.end, symbols)
    panel = add_labels(panel, args.horizon, args.min_amount_wan)
    panel = panel[(panel["y"] >= 0) & panel["tradable"]]
    log.info("因子面板：%d 行 / %d 个交易日", len(panel), panel["date"].nunique())

    merged = panel.merge(kronos, on=["date", "symbol"], how="inner")
    if merged.empty:
        log.error("因子面板与 Kronos 特征没有交集")
        return 2
    log.info("合并后：%d 行 / %d 个交易日 / %d 只", len(merged), merged["date"].nunique(), merged["symbol"].nunique())

    if args.shuffle_kronos:
        cols = [c for c in KRONOS_COLUMNS if c in merged.columns]
        rng_state = np.random.default_rng(0)
        shuffled = []
        for _, idx in merged.groupby("date").groups.items():
            block = merged.loc[idx, cols].to_numpy("float32").copy()
            rng_state.shuffle(block, axis=0)
            shuffled.append(pd.DataFrame(block, index=idx, columns=cols))
        merged.loc[:, cols] = pd.concat(shuffled).sort_index()
        log.warning("对照组：Kronos 特征已在每个交易日内打乱")

    base_features = [c for c in panel.columns if c not in PRICE_COLUMNS]
    base_features = [c for c in base_features if c not in ("limit_up", "fwd_ret", "tradable", "y")]
    base_features = [c for c in base_features if merged[c].notna().mean() > 0.5]
    kronos_features = [c for c in KRONOS_COLUMNS if c in merged.columns]
    log.info("基础特征 %d 个，Kronos 特征 %d 个", len(base_features), len(kronos_features))

    train_end = pd.Timestamp(args.train_end)
    valid_end = pd.Timestamp(args.valid_end)
    purge = pd.Timedelta(days=0)
    dates = np.sort(merged["date"].unique())
    train_dates = [d for d in dates if d <= train_end]
    keep = int(len(train_dates) - (args.horizon + 2))
    purge_cut = train_dates[keep] if keep > 0 else train_end
    purge = pd.Timestamp(purge_cut)

    train = merged[merged["date"] <= purge]
    valid = merged[(merged["date"] > train_end) & (merged["date"] <= valid_end)]
    test = merged[merged["date"] > valid_end]
    if min(len(train), len(valid), len(test)) == 0:
        log.error("训练/验证/测试集为空：train=%d valid=%d test=%d", len(train), len(valid), len(test))
        return 2
    log.info("样本：train=%d valid=%d test=%d", len(train), len(valid), len(test))

    results = {}
    results["base"] = run_arm(train, valid, test, base_features, args)
    results["base_plus_kronos"] = run_arm(
        train, valid, test, base_features + kronos_features, args
    )
    results["kronos_only"] = run_arm(train, valid, test, kronos_features, args)
    results["paired_delta_plus_vs_base"] = _paired_delta(
        results["base"]["per_date_ic"], results["base_plus_kronos"]["per_date_ic"]
    )

    for name, m in results.items():
        if name == "paired_delta_plus_vs_base":
            log.info(
                "配对检验(+Kronos vs 基线)：ΔRankIC=%+.4f t=%+.2f 胜率=%.2f n=%d",
                m["mean"], m["t"], m.get("hit", float("nan")), int(m["n"]),
            )
            continue
        log.info(
            "%s: rankIC=%+.4f ICIR=%+.2f TopK=%+.4f%% n=%d",
            name,
            m["rank_ic_mean"],
            m["rank_ic_icir"],
            m["topk_excess_mean"] * 100,
            int(m["n_dates"]),
        )

    payload = {
        "config": {
            **vars(args),
            "n_train": len(train),
            "n_valid": len(valid),
            "n_test": len(test),
            "purge_end": str(purge.date()),
            "base_features": base_features,
            "kronos_features": kronos_features,
        },
        "results": results,
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("写入 %s", args.output)
    print(json.dumps(
        {
            k: {kk: vv for kk, vv in v.items() if kk not in ("top_gain", "per_date_ic")}
            for k, v in results.items()
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
