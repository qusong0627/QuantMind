"""盘中 4h 买入模型：LightGBM + purged 切分 + 截面 rank 标签 + 扣成本评估。

## 口径

- 样本：`(date, symbol, cutoff)`，5 个时点；只用 `tradable AND label_ok`
- 标签：`rank_4h - 0.5`（同日同 cutoff 的截面分位，抗市场涨跌）
- 特征：45 个分钟特征 + 58 个前一日日频上下文 + `ret_today_sofar` + `cutoff_idx`
- 切分（按日，禁随机）：
  - 训练 2024-01-01 ~ 2025-06-30
  - 验证 2025-07-01 ~ 2025-12-31（early stopping）
  - 样本外 2026-01-01 ~ 2026-10-30
  - 两段交界各丢 4 个交易日（embargo，覆盖 4h 跨日标签）
- 评估：逐日逐 cutoff 的 Spearman IC、ICIR、5 档分层、Top50 扣成本收益、分 cutoff 拆解
- 成本：双边万 3 手续费 + 0.1% 滑点（单边按 0.05%+0.015% 计）

用法：
  .venv/bin/python -m backend.scripts.minute_model.train_intraday_model --sample-frac 0.5
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger("minute_model.train")
PANEL = Path("data/minute_model/panel")
OUT = Path("data/minute_model/model")
EXCLUDE = {
    "symbol", "date", "cutoff",
    "ret_4h", "ret_5h", "ret_5h_capped", "rank_4h", "label_ok", "tradable",
    "is_limit_up", "is_suspended", "is_new", "has_60d_history",
    "prev_close", "next_close", "px_next_same", "px_next_5h",
    "ret_today_sofar",  # 作为特征单独加入
}
CUTOFF_IDX = {"10:30": 0, "11:30": 1, "13:30": 2, "14:30": 3, "14:50": 4}


def _json_default(o):
    """numpy/pandas 标量 -> Python 原生。"""
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def _panel_days(start: str, end: str) -> list[str]:
    days = []
    for p in PANEL.glob("date=*"):
        n = p.name.split("=", 1)
        if len(n) == 2 and n[1].isdigit() and start <= n[1] <= end and (p / "data.parquet").exists():
            days.append(n[1])
    return sorted(days)


def load_frames(days: list[str], feature_cols: list[str], sample_frac: float, seed: int) -> pd.DataFrame:
    files = [str(PANEL / f"date={d}" / "data.parquet") for d in days]
    cols = feature_cols + ["ret_4h", "rank_4h", "ret_today_sofar", "cutoff"]
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")
    sample_sql = (f" USING SAMPLE {sample_frac * 100:.2f} PERCENT (bernoulli)"
                  if sample_frac < 1.0 else "")
    q = f"""
        SELECT {", ".join(cols)}
        FROM read_parquet({files!r})
        WHERE label_ok AND tradable AND rank_4h IS NOT NULL{sample_sql}
    """
    df = con.sql(q).df()
    con.close()
    df["cutoff_idx"] = df["cutoff"].map(CUTOFF_IDX).astype("int8")
    df["y"] = (df["rank_4h"].astype("float32") - 0.5).astype("float32")
    df = df.drop(columns=["cutoff", "rank_4h"])
    for c in df.columns:
        if c not in ("y", "cutoff_idx", "ret_4h"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    return df


def evaluate(pred_df: pd.DataFrame, label: str) -> dict:
    """pred_df: date, cutoff, pred, ret_4h, tradable。返回 IC/分层/Top50。"""
    con = duckdb.connect()
    con.register("p", pred_df)
    ic = con.sql(f"""
        WITH r AS (
            SELECT date, cutoff, pred, ret_4h,
                   rank() OVER (PARTITION BY date, cutoff ORDER BY pred) AS rp,
                   rank() OVER (PARTITION BY date, cutoff ORDER BY ret_4h) AS rr
            FROM p
        )
        SELECT date, cutoff, corr(rp, rr) AS ic, count(*) AS n
        FROM r GROUP BY 1, 2 HAVING count(*) >= 100
    """).df()
    by_cut = ic.groupby("cutoff")["ic"].agg(["mean", "std", "count"]).reset_index()
    mean_ic = ic["ic"].mean()
    icir = mean_ic / ic["ic"].std() * np.sqrt(len(ic)) if len(ic) > 1 else np.nan
    dec = con.sql("""
        WITH r AS (
            SELECT *, ntile(5) OVER (PARTITION BY date, cutoff ORDER BY pred) AS q
            FROM p WHERE ret_4h IS NOT NULL
        )
        SELECT q, avg(ret_4h) AS ret, count(*) AS n FROM r GROUP BY q ORDER BY q
    """).df()
    top = con.sql("""
        WITH r AS (
            SELECT *, row_number() OVER (PARTITION BY date, cutoff ORDER BY pred DESC) AS rn
            FROM p WHERE ret_4h IS NOT NULL AND tradable
        )
        SELECT avg(ret_4h) AS gross, avg(CASE WHEN ret_4h > 0 THEN 1.0 ELSE 0.0 END) AS win,
               median(ret_4h) AS med, quantile_cont(ret_4h, 0.1) AS p10,
               quantile_cont(ret_4h, 0.9) AS p90,
               avg(CASE WHEN abs(ret_4h) >= 0.095 THEN 1.0 ELSE 0.0 END) AS frac_limit,
               count(*) AS n
        FROM r WHERE rn <= 50
    """).df()
    con.close()
    cost = 0.0015 + 0.001  # 双边万3 + 0.2% 滑点（保守）
    return {
        "label": label,
        "ic_mean": float(mean_ic),
        "ic_std": float(ic["ic"].std()),
        "icir": float(icir),
        "ic_days": int(len(ic)),
        "ic_pos_ratio": float((ic["ic"] > 0).mean()),
        "ic_by_cutoff": by_cut.to_dict("records"),
        "deciles": dec.to_dict("records"),
        "top50_gross": float(top["gross"].iloc[0]),
        "top50_net": float(top["gross"].iloc[0] - cost),
        "top50_win": float(top["win"].iloc[0]),
        "top50_med": float(top["med"].iloc[0]),
        "top50_p10": float(top["p10"].iloc[0]),
        "top50_p90": float(top["p90"].iloc[0]),
        "top50_frac_limit": float(top["frac_limit"].iloc[0]),
        "top50_n": int(top["n"].iloc[0]),
        "cost_assumed": cost,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-frac", type=float, default=0.5)
    ap.add_argument("--rounds", type=int, default=800)
    ap.add_argument("--leaves", type=int, default=63)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    import lightgbm as lgb

    OUT.mkdir(parents=True, exist_ok=True)
    train_days = _panel_days("20240101", "20250630")
    valid_days = _panel_days("20250701", "20251231")[4:]   # embargo 4 天
    test_days = _panel_days("20260101", "20261030")[4:]
    log.info("训练 %d 天 / 验证 %d 天 / 测试 %d 天", len(train_days), len(valid_days), len(test_days))

    import pyarrow.parquet as pq
    sample = PANEL / f"date={train_days[0]}" / "data.parquet"
    all_cols = pq.ParquetFile(sample).schema_arrow.names
    feature_cols = [c for c in all_cols if c not in EXCLUDE]

    t0 = time.time()
    train = load_frames(train_days, feature_cols, args.sample_frac, args.seed)
    log.info("训练样本 %d 行，%.1f 分钟", len(train), (time.time() - t0) / 60)
    t0 = time.time()
    valid = load_frames(valid_days, feature_cols, 1.0, args.seed)
    log.info("验证样本 %d 行，%.1f 分钟", len(valid), (time.time() - t0) / 60)

    feats = feature_cols + ["ret_today_sofar", "cutoff_idx"]
    Xtr, ytr = train[feats], train["y"]
    Xva, yva = valid[feats], valid["y"]

    model = lgb.LGBMRegressor(
        objective="regression", n_estimators=args.rounds, learning_rate=args.lr,
        num_leaves=args.leaves, min_child_samples=200, subsample=0.8, subsample_freq=1,
        colsample_bytree=0.8, reg_lambda=5.0, n_jobs=18, random_state=args.seed,
        verbose=-1,
    )
    model.fit(
        Xtr, ytr, eval_set=[(Xva, yva)],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(100)],
    )
    log.info("最优迭代 %d，验证 best=%.6f", model.best_iteration_, model.best_score_["valid_0"]["l2"])

    with open(OUT / "lgb_4h.pkl", "wb") as fh:
        pickle.dump({"model": model, "features": feats, "cutoffs": CUTOFF_IDX}, fh)

    # 样本外：逐日预测（控制内存）
    t0 = time.time()
    preds = []
    con = duckdb.connect()
    for i, d in enumerate(test_days, 1):
        p = PANEL / f"date={d}" / "data.parquet"
        df = con.sql(f"SELECT * FROM read_parquet('{p}')").df()
        df["cutoff_idx"] = df["cutoff"].map(CUTOFF_IDX).astype("int8")
        for c in feats:
            if c not in df.columns:
                df[c] = np.nan
        df["pred"] = model.predict(df[feats].astype("float32"))
        preds.append(df[["date", "cutoff", "pred", "ret_4h", "tradable", "label_ok", "ret_5h"]])
        if i % 40 == 0:
            log.info("预测 %d/%d", i, len(test_days))
    con.close()
    test = pd.concat(preds, ignore_index=True)
    test = test[test["label_ok"] & test["tradable"]]
    test.to_parquet(OUT / "pred_4h_test.parquet", index=False)
    log.info("测试预测 %d 行，%.1f 分钟", len(test), (time.time() - t0) / 60)

    report = {
        "model": "lightgbm",
        "split": {"train": [train_days[0], train_days[-1]], "valid": [valid_days[0], valid_days[-1]],
                  "test": [test_days[0], test_days[-1]]},
        "sample": {"train_rows": int(len(train)), "valid_rows": int(len(valid)),
                   "test_rows": int(len(test)), "sample_frac": args.sample_frac},
        "features": len(feats),
        "test": evaluate(test, "ret_4h"),
        "importance_top30": sorted(
            zip(feats, model.feature_importances_), key=lambda x: -x[1]
        )[:30],
    }
    (OUT / "report_4h.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8"
    )
    print(json.dumps(report["test"], ensure_ascii=False, indent=2, default=_json_default))
    print("TOP30:", [(k, int(v)) for k, v in report["importance_top30"][:15]])


if __name__ == "__main__":
    main()
