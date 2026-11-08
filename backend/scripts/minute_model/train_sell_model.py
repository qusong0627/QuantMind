"""分钟级卖出模型：对买入模型选出的持仓，次日盘中逐分钟决定「卖 or 继续持有」。

## 样本

- 持仓：每个交易日 14:50 时点，买入模型 Top20（tradable），买入价 = 14:50 bar 收盘
- 持有日：下一交易日 09:31 ~ 14:50（4h 终点），每分钟一个状态
- 标签：`hold_ret = 终点价/当前价 - 1`；`label = 1 if hold_ret > 0.001`（继续持有更优）
- 特征：近 5/15/30 分钟动量、量比、订单流不平衡、盘口、持仓浮盈、距高回撤、剩余分钟
- 训练 2024-2025（买入模型 Top20 持仓），测试 2026

## 端到端

测试段四种退出策略对比（同一批买入）：
  A 卖出模型（逐分钟） / B 次日开盘卖 / C 次日收盘卖 / D 固定 -5% 止损否则终点卖

用法：
  .venv/bin/python -m backend.scripts.minute_model.train_sell_model
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

log = logging.getLogger("minute_model.sell")
PANEL = Path("data/minute_model/panel")
MIN1 = Path("data/quantdb/1_kline_data/l2_min1")
OUT = Path("data/minute_model/model")
BUY_MODEL = OUT / "lgb_4h.pkl"
CUTOFF = "14:50"
TOPN = 20
COST = 0.0025  # 单次进出总成本（双边万3 + 0.2% 滑点，保守）


def _days(start: str, end: str) -> list[str]:
    out = []
    for p in PANEL.glob("date=*"):
        n = p.name.split("=", 1)
        if len(n) == 2 and n[1].isdigit() and start <= n[1] <= end and (p / "data.parquet").exists():
            out.append(n[1])
    return sorted(out)


def _next_day(d: str, days: list[str]) -> str | None:
    i = days.index(d)
    return days[i + 1] if i + 1 < len(days) else None


def positions_for(days: list[str], buy) -> pd.DataFrame:
    """用买入模型在 14:50 选 Top20，返回 (date, next_date, symbol, entry_px, endpoint_px)。"""
    model, feats = buy["model"], buy["features"]
    con = duckdb.connect()
    rows = []
    for i, d in enumerate(days):
        if i + 1 >= len(days):
            break
        nd = days[i + 1]
        df = con.sql(f"SELECT * FROM read_parquet('{PANEL}/date={d}/data.parquet') "
                     f"WHERE cutoff = '{CUTOFF}' AND tradable AND label_ok").df()
        if len(df) < 100:
            continue
        df["cutoff_idx"] = 4
        for c in feats:
            if c not in df.columns:
                df[c] = np.nan
        df["pred"] = model.predict(df[feats].astype("float32"))
        df = df.nlargest(TOPN, "pred")
        # 终点价 = 下一交易日同 cutoff 的 px（面板 px_next_same）
        df = df[df["px_next_same"].notna()]
        out = df[["symbol", "px", "px_next_same"]].copy()
        out["date"] = d
        out["next_date"] = nd
        rows.append(out)
    con.close()
    return pd.concat(rows, ignore_index=True).rename(
        columns={"px": "entry_px", "px_next_same": "endpoint_px"}
    )


def _sample_sql(next_date: str, n_symbols: int) -> str:
    """对某持有日的分钟 bar 计算逐分钟特征（不依赖持仓名单，按 symbol 分组）。"""
    return f"""
    WITH b AS (
        SELECT * FROM read_parquet('{MIN1}/dt={next_date}/data.parquet')
        WHERE time >= TIMESTAMP '{next_date[:4]}-{next_date[4:6]}-{next_date[6:]} 09:31:00'
          AND time <= TIMESTAMP '{next_date[:4]}-{next_date[4:6]}-{next_date[6:]} 14:50:00'
    ), w AS (
        SELECT *,
            close / lag(close, 5) OVER pw - 1.0 AS ret_5m,
            close / lag(close, 15) OVER pw - 1.0 AS ret_15m,
            close / lag(close, 30) OVER pw - 1.0 AS ret_30m,
            close / lag(close) OVER pw - 1.0 AS r1,
            sum(amount) OVER (PARTITION BY symbol ORDER BY time ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS amt5,
            sum(amount) OVER (PARTITION BY symbol ORDER BY time ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS amt30,
            sum(buy_amount - sell_amount) OVER (PARTITION BY symbol ORDER BY time ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ofi_5m,
            sum(buy_amount - sell_amount) OVER (PARTITION BY symbol ORDER BY time ROWS BETWEEN 14 PRECEDING AND CURRENT ROW) AS ofi_15m,
            sum(large_buy_amount - large_sell_amount) OVER (PARTITION BY symbol ORDER BY time ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS large_net_5m,
            max(high) OVER (PARTITION BY symbol ORDER BY time ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS day_high_so_far,
            min(low) OVER (PARTITION BY symbol ORDER BY time ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS day_low_so_far,
            (total_bidv + total_askv) AS depth,
            (bidv1 - askv1) / nullif(bidv1 + askv1, 0) AS imb_1,
            (ask1 - bid1) / nullif((ask1 + bid1) / 2.0, 0) AS spread,
            row_number() OVER (PARTITION BY symbol ORDER BY time) AS rn,
            row_number() OVER (PARTITION BY symbol ORDER BY time DESC) AS rn_end
        FROM b
        WINDOW pw AS (PARTITION BY symbol ORDER BY time)
    )
    SELECT symbol, time, close,
        ret_5m, ret_15m, ret_30m,
        stddev_samp(r1) OVER (PARTITION BY symbol ORDER BY time ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS vol_5m,
        amt5 / nullif(amt30 / 6.0, 0) AS amt_ratio_5_30,
        ofi_5m, ofi_15m, large_net_5m,
        spread, imb_1,
        depth / nullif(lag(depth, 5) OVER pw2, 0) - 1.0 AS depth_chg_5m,
        close / nullif(day_high_so_far, 0) - 1.0 AS dd_from_high,
        close / nullif(day_low_so_far, 0) - 1.0 AS up_from_low,
        rn AS minutes_held, rn_end AS minutes_to_end
    FROM w
    WINDOW pw2 AS (PARTITION BY symbol ORDER BY time)
    """


def build_samples(pos: pd.DataFrame, every: int = 5) -> pd.DataFrame:
    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")
    con.register("pos", pos)
    frames = []
    for nd, g in pos.groupby("next_date"):
        syms = g["symbol"].unique().tolist()
        q = _sample_sql(nd, len(syms))
        bars = con.sql(q).df()
        bars = bars[bars["symbol"].isin(syms)]
        # 每 5 分钟抽一个状态，降低样本自相关
        bars = bars[bars["minutes_held"] % every == 0]
        m = bars.merge(g[["symbol", "entry_px", "endpoint_px", "date", "next_date"]], on="symbol")
        m["cur_ret"] = m["close"] / m["entry_px"] - 1.0
        m["hold_ret"] = m["endpoint_px"] / m["close"] - 1.0
        m["label"] = (m["hold_ret"] > 0.001).astype("int8")
        frames.append(m)
    con.close()
    return pd.concat(frames, ignore_index=True)


def simulate(pos: pd.DataFrame, samples: pd.DataFrame, model, feats) -> dict:
    """逐持仓逐分钟模拟卖出模型 vs 三基线。"""
    res = []
    for nd, g in samples.groupby("next_date"):
        p = pos[pos["next_date"] == nd]
        for _, row in p.iterrows():
            s = g[g["symbol"] == row["symbol"]].sort_values("time")
            if s.empty:
                continue
            X = s[feats].astype("float32")
            prob = model.predict_proba(X)[:, 1]
            # A：卖出模型——第一次 prob<0.5 时卖；否则终点卖
            idx = np.where(prob < 0.5)[0]
            sell_i = idx[0] if len(idx) else len(s) - 1
            ret_model = s["close"].iloc[sell_i] / row["entry_px"] - 1.0 - COST
            ret_open = s["close"].iloc[0] / row["entry_px"] - 1.0 - COST
            ret_close = s["close"].iloc[-1] / row["entry_px"] - 1.0 - COST
            # D：-5% 止损，否则终点
            hit = s[s["close"] / row["entry_px"] - 1.0 <= -0.05]
            ret_stop = (hit["close"].iloc[0] if len(hit) else s["close"].iloc[-1]) / row["entry_px"] - 1.0 - COST
            res.append({"date": nd, "symbol": row["symbol"],
                        "model": ret_model, "open": ret_open, "close": ret_close, "stop": ret_stop,
                        "held_min": int(sell_i + 1)})
    r = pd.DataFrame(res)
    out = {}
    for k in ("model", "open", "close", "stop"):
        out[k] = {"mean_pct": float(r[k].mean() * 100), "win_pct": float((r[k] > 0).mean() * 100),
                  "median_pct": float(r[k].median() * 100), "n": int(len(r))}
    out["avg_held_min_model"] = float(r["held_min"].mean())
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=400)
    args = ap.parse_args()
    import lightgbm as lgb

    buy = pickle.load(open(BUY_MODEL, "rb"))
    all_days = _days("20240101", "20261031")
    train_days = [d for d in all_days if d <= "20251231"]
    test_days = [d for d in all_days if d >= "20260101"]

    t0 = time.time()
    pos_train = positions_for(train_days, buy)
    log.info("训练持仓 %d 笔，%.1f 分钟", len(pos_train), (time.time() - t0) / 60)
    t0 = time.time()
    smp_train = build_samples(pos_train)
    log.info("训练样本 %d 行，%.1f 分钟", len(smp_train), (time.time() - t0) / 60)

    feats = ["ret_5m", "ret_15m", "ret_30m", "vol_5m", "amt_ratio_5_30", "ofi_5m", "ofi_15m",
             "large_net_5m", "spread", "imb_1", "depth_chg_5m", "dd_from_high", "up_from_low",
             "cur_ret", "minutes_held", "minutes_to_end"]
    model = lgb.LGBMClassifier(
        objective="binary", n_estimators=args.rounds, learning_rate=0.05, num_leaves=31,
        min_child_samples=200, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
        reg_lambda=10.0, n_jobs=16, random_state=42, verbose=-1,
    )
    model.fit(smp_train[feats], smp_train["label"])
    with open(OUT / "lgb_sell.pkl", "wb") as fh:
        pickle.dump({"model": model, "features": feats, "cutoff": CUTOFF}, fh)

    t0 = time.time()
    pos_test = positions_for(test_days, buy)
    smp_test = build_samples(pos_test)
    log.info("测试持仓 %d 笔 / 样本 %d 行，%.1f 分钟",
             len(pos_test), len(smp_test), (time.time() - t0) / 60)

    out = simulate(pos_test, smp_test, model, feats)
    report = {"cutoff": CUTOFF, "topn": TOPN, "cost": COST,
              "train_positions": int(len(pos_train)), "test_positions": int(len(pos_test)),
              "end_to_end": out}
    (OUT / "report_sell.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=lambda o: o.item()), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=lambda o: o.item()))


if __name__ == "__main__":
    main()
