"""尾盘买入模型（open_rank 标签）：只用可买池训练 + 剔除 px 的 CatBoost 版本。

## 口径

- 样本：面板 `14:50` 时点、`tradable AND ask_depth_1 > 0 AND px IS NOT NULL`
  ——只学「真能买进」的票（原版把封板票也放进训练集，模型退化成涨停识别器）
- 标签：`gap_next = 次日开盘价 / 当日 14:50 价 - 1`，同日截面 `percent_rank - 0.5`
  ——标签的兑现动作必须等于实际交易动作（次日开盘卖），不要用 `ret_4h`（跨 5h20m）
- 特征：`--base-model` 的特征表去掉 `px`（价格水平）与 `cutoff_idx`
  ——水平量特征让树模型学「低价股记忆」，训练期有效、样本外不泛化；比率/状态量才是该留的
- 切分（干净）：训练 2024 全年 / 验证 2025H1（early stopping）/ 测试 2025-07 ~ 2026-10
  ——两段测试期都未参与任何选型；交界各留 4 个交易日 embargo
- 评估：每日 Top20 等权、成本 0.15%，毛/净、胜率、t 值、月度、净值/回撤/夏普
- `--placebo`：同日打乱标签重训，用于对照（真模型的净收益必须显著高于安慰剂）

输出：
  data/minute_model/model/{tag}.pkl                 模型（model/features/label/params）
  data/minute_model/model/report_{tag}.json         分段指标 + 月度 + 成本敏感性
  data/minute_model/model/pred_{tag}_test.parquet   测试期逐笔打分

用法：
  docker exec -i -w / -e PYTHONPATH=/app quantmind \\
    python -m backend.scripts.minute_model.train_open_rank --tag ab_clean_full

  # 安慰剂对照
  ... --tag ab_clean_full_placebo --placebo
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import pickle
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger("minute_model.open_rank")
PANEL = Path("data/minute_model/panel")
MODEL = Path("data/minute_model/model")
DAILY = Path("data/quantdb/1_kline_data/daily_unadjusted")

CUTOFF = "14:50"
COST = 0.0015
TOPN = 20
Y_CLIP = 0.35
SPLIT = {
    "train": ("20240101", "20241231"),
    "valid": ("20250101", "20250630"),
    "test": ("20250701", "20261029"),
}
SEGMENTS = (("2025H2", "202507", "202512"), ("2026", "202601", "202610"))
EMBARGO_DAYS = 4

PARAMS = {
    "iterations": 600,
    "learning_rate": 0.05,
    "depth": 6,
    "l2_leaf_reg": 8,
    "random_seed": 42,
    "verbose": 0,
    "od_type": "Iter",
    "od_wait": 60,
}


def _panel_days(start: str, end: str) -> list[str]:
    days = []
    for p in PANEL.glob("date=*"):
        n = p.name.split("=", 1)
        if (
            len(n) == 2
            and n[1].isdigit()
            and start <= n[1] <= end
            and (p / "data.parquet").exists()
        ):
            days.append(n[1])
    return sorted(days)


def _next_open_table(con) -> None:
    """注册 `lab`：每个交易日 × 标的的次日开盘价（daily_unadjusted 不复权）。"""
    cal = sorted(p.split("dt=")[1] for p in glob.glob(str(DAILY / "dt=*")))
    files = [str(DAILY / f"dt={d}" / "data.parquet") for d in cal]
    dly = con.sql(
        f"SELECT CAST(dt AS VARCHAR) d, symbol, CAST(open AS DOUBLE) o FROM read_parquet({files!r})"
    ).df()
    nxt = (
        dly.pivot_table(index="symbol", columns="d", values="o")
        .shift(-1, axis=1)
        .stack()
        .rename("o_n")
        .reset_index()
        .rename(columns={"d": "date"})
    )
    nxt["date"] = nxt["date"].astype(str)
    con.register("lab", nxt)


def _load(
    con, days: list[str], feats: list[str], frac: float, seed: int = 42
) -> pd.DataFrame:
    files = [str(PANEL / f"date={d}" / "data.parquet") for d in days]
    cols = ",".join(f'p."{c}"' for c in feats)
    sample = (
        f" USING SAMPLE {frac * 100:.2f} PERCENT (bernoulli, {seed})"
        if frac < 1.0
        else ""
    )
    df = con.sql(
        f"""
        SELECT p.date, p.symbol, p.px, p.cum_amount, {cols}, p.mkt_ret_20d, p.mkt_dd_20d,
               l.o_n / p.px - 1.0 AS y_open
        FROM read_parquet({files!r}) p
        JOIN lab l ON l.date = CAST(p.date AS VARCHAR) AND l.symbol = p.symbol
        WHERE p.cutoff = '{CUTOFF}' AND p.tradable AND p.ask_depth_1 > 0
          AND p.px IS NOT NULL{sample}
        """
    ).df()
    df["date"] = df["date"].astype(str)
    return df[df["y_open"].between(-Y_CLIP, Y_CLIP)].reset_index(drop=True)


def _rank_label(df: pd.DataFrame) -> pd.Series:
    return (df.groupby("date")["y_open"].rank(pct=True) - 0.5).astype("float32")


def _daily_top20(df: pd.DataFrame, pred: np.ndarray) -> pd.DataFrame:
    """逐日取 Top20，返回 date/ret/win/regime + 组合价格/成交额画像。"""
    d = df[["date", "y_open", "px", "cum_amount", "mkt_ret_20d", "mkt_dd_20d"]].copy()
    d["pred"] = pred
    rows = []
    for dt, g in d.groupby("date", sort=True):
        top = g.nlargest(TOPN, "pred")
        rows.append(
            (
                dt,
                top["y_open"].mean(),
                (top["y_open"] > 0).mean(),
                top["mkt_ret_20d"].iloc[0],
                top["mkt_dd_20d"].iloc[0],
                top["px"].median(),
                top["cum_amount"].median(),
            )
        )
    out = pd.DataFrame(
        rows, columns=["date", "ret", "win", "r20", "dd", "px_med", "amt_med"]
    )
    out["gate"] = (out["r20"] > 0) & (out["dd"] > -0.03)
    return out


def _segment_stats(daily: pd.DataFrame) -> dict:
    res = {}
    for name, lo, hi in SEGMENTS + (("全期", "202507", "202610"),):
        s = daily[(daily["date"].str[:6] >= lo) & (daily["date"].str[:6] <= hi)]
        if s.empty:
            continue
        net = s["ret"].mean() - COST
        res[name] = {
            "days": int(len(s)),
            "gross_pct": round(float(s["ret"].mean()) * 100, 3),
            "net_pct": round(float(net) * 100, 3),
            "win_pct": round(float(s["win"].mean()) * 100, 1),
            "t": round(float(net / (s["ret"].std() / np.sqrt(len(s)))), 2),
        }
    return res


def _curve_stats(daily: pd.DataFrame) -> dict:
    def one(frame: pd.DataFrame) -> dict:
        r = frame["ret"] - COST
        if frame.empty:
            return {"days": 0}
        eq = (1 + r).cumprod()
        mdd = (eq / eq.cummax() - 1).min()
        return {
            "days": int(len(frame)),
            "total_return_pct": round(float(eq.iloc[-1] - 1) * 100, 1),
            "max_drawdown_pct": round(float(mdd) * 100, 2),
            "sharpe": round(float(r.mean() / r.std() * np.sqrt(242)), 2)
            if r.std() > 0
            else 0.0,
            "calmar": round(float((eq.iloc[-1] - 1) / abs(mdd)), 1)
            if mdd < 0
            else None,
            "day_win_pct": round(float((r > 0).mean()) * 100, 1),
        }

    return {"all": one(daily), "regime_gate": one(daily[daily["gate"]])}


def _monthly(daily: pd.DataFrame) -> dict:
    d = daily.copy()
    d["ym"] = d["date"].str[:6]
    m = d.groupby("ym")["ret"].mean() - COST
    return {k: round(float(v) * 100, 2) for k, v in m.items()}


def _cost_sensitivity(daily: pd.DataFrame) -> dict:
    out = {}
    for c in (0.0010, 0.0015, 0.0025, 0.0030):
        r = daily["ret"] - c
        eq = (1 + r).cumprod()
        out[f"{c * 100:.2f}%"] = round(float(eq.iloc[-1] - 1) * 100, 1)
    return out


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--base-model", default="lgb_4h_snap.pkl", help="提供特征表的基础模型"
    )
    ap.add_argument("--tag", default="ab_clean_full", help="输出文件名后缀")
    ap.add_argument("--sample-frac", type=float, default=1.0, help="训练/验证抽样比例")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（换种子测训练噪声）")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--placebo", action="store_true", help="同日打乱标签重训（对照）")
    ap.add_argument(
        "--no-artifacts",
        action="store_true",
        help="只写 report，不落模型/预测（扫参用）",
    )
    args = ap.parse_args()

    from catboost import CatBoostRegressor, Pool

    MODEL.mkdir(parents=True, exist_ok=True)
    base = pickle.load(open(MODEL / args.base_model, "rb"))
    feats = [c for c in base["features"] if c not in ("px", "cutoff_idx")]
    log.info(
        "特征 %d 个（base %d，去掉 px/cutoff_idx）", len(feats), len(base["features"])
    )

    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")
    _next_open_table(con)

    train_days = _panel_days(*SPLIT["train"])
    valid_days = _panel_days(*SPLIT["valid"])[EMBARGO_DAYS:]
    test_days = _panel_days(*SPLIT["test"])[EMBARGO_DAYS:]
    log.info(
        "训练 %d 天 / 验证 %d 天 / 测试 %d 天",
        len(train_days),
        len(valid_days),
        len(test_days),
    )

    t0 = time.time()
    train = _load(con, train_days, feats, args.sample_frac, args.seed)
    valid = _load(con, valid_days, feats, args.sample_frac, args.seed)
    log.info(
        "训练样本 %d 行 / 验证 %d 行，%.1f 分钟",
        len(train),
        len(valid),
        (time.time() - t0) / 60,
    )
    if args.placebo:
        log.info("安慰剂：同日打乱 y_open")
        train["y_open"] = train.groupby("date")["y_open"].transform(
            lambda s: s.sample(frac=1.0, random_state=42).to_numpy()
        )
        valid["y_open"] = valid.groupby("date")["y_open"].transform(
            lambda s: s.sample(frac=1.0, random_state=42).to_numpy()
        )
    train["y"] = _rank_label(train)
    valid["y"] = _rank_label(valid)

    params = dict(PARAMS, thread_count=args.threads, random_seed=args.seed)
    t0 = time.time()
    model = CatBoostRegressor(loss_function="RMSE", **params)
    model.fit(Pool(train[feats], train["y"]), eval_set=Pool(valid[feats], valid["y"]))
    log.info(
        "训练完成，%.1f 分钟，树 %d 棵", (time.time() - t0) / 60, model.tree_count_
    )

    t0 = time.time()
    test = _load(con, test_days, feats, 1.0)
    test["pred"] = model.predict(test[feats].astype("float32"))
    log.info("测试样本 %d 行，%.1f 分钟", len(test), (time.time() - t0) / 60)

    daily = _daily_top20(test, test["pred"].to_numpy())
    report = {
        "tag": args.tag,
        "placebo": args.placebo,
        "label": "next_open_rank_minus_0.5",
        "features": len(feats),
        "dropped_features": ["px", "cutoff_idx"],
        "cost": COST,
        "params": params,
        "sample": {
            "train_rows": len(train),
            "valid_rows": len(valid),
            "test_rows": len(test),
        },
        "split": {k: list(v) for k, v in SPLIT.items()},
        "segments": _segment_stats(daily),
        "curve": _curve_stats(daily),
        "monthly_net_pct": _monthly(daily),
        "cost_sensitivity_total_return_pct": _cost_sensitivity(daily),
        "top20_price_median": round(float(daily["px_med"].median()), 2),
        "top20_amt_median_wan": round(float(daily["amt_med"].median()), 0),
        "universe_price_median": round(float(test["px"].median()), 2),
    }
    log.info("分段：%s", json.dumps(report["segments"], ensure_ascii=False))
    log.info("净值：%s", json.dumps(report["curve"]["all"], ensure_ascii=False))

    if not args.placebo and not args.no_artifacts:
        pickle.dump(
            {
                "model": model,
                "features": feats,
                "label": "open_rank",
                "params": params,
                "dropped": ["px", "cutoff_idx"],
                "split": SPLIT,
            },
            open(MODEL / f"{args.tag}.pkl", "wb"),
        )
        test[
            ["date", "symbol", "px", "pred", "y_open", "mkt_ret_20d", "mkt_dd_20d"]
        ].to_parquet(MODEL / f"pred_{args.tag}_test.parquet", index=False)
        daily.to_parquet(MODEL / f"daily_{args.tag}_test.parquet", index=False)
    (MODEL / f"report_{args.tag}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    log.info("产物：%s", MODEL / f"report_{args.tag}.json")


if __name__ == "__main__":
    main()
