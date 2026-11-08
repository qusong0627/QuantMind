"""从 QuantDB L2 分钟表（l2_min1）构建 14:50 盘中特征（每股每日一行）。

输入：``data/quantdb/1_kline_data/l2_min1/dt=YYYYMMDD/data.parquet``
输出：``data/minute_model/daily/date=YYYYMMDD/data.parquet``

口径：
- **只用 time <= 14:50:00 的 bar**（09:25 集合竞价 + 09:31..14:50，共约 150 根），
  14:50 之后的 bar 一律不读，无后视；
- 价格用未复权价（与 QuantDB daily_unadjusted 同口径），复权在面板层做；
- 成交额单位万元，成交量单位股，与 l2_min1 / daily_* 一致；
- 缺失分钟不补：n_bars 暴露覆盖度，特征用可用 bar 计算。

用法（宿主机仓库根）：
  .venv/bin/python -m backend.scripts.minute_model.build_intraday_features --probe 20240401
  .venv/bin/python -m backend.scripts.minute_model.build_intraday_features --all
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import duckdb

from backend.scripts.minute_model.build_min1_l2 import OUT as MIN1_DIR

log = logging.getLogger("minute_model.features")
OUT = MIN1_DIR.parents[2] / "minute_model" / "daily"  # data/minute_model/daily（research 产物，不进 quantdb）
CUTOFFS = ("10:30", "11:30", "13:30", "14:30", "14:50")
CUTOFF = "14:50:00"

FEATURE_COLS = [
    "symbol", "date", "cutoff", "n_bars",
    "px", "day_open", "day_high", "day_low",
    "ret_open", "range_pos", "cum_volume", "cum_amount", "vwap", "vwap_gap",
    "ret_5m", "ret_15m", "ret_30m", "vol_15m", "vol_30m",
    "bid1", "ask1", "spread",
    "bid_depth_1", "bid_depth_5", "bid_depth_10",
    "ask_depth_1", "ask_depth_5", "ask_depth_10",
    "imb_1", "imb_5", "imb_10", "big_bid_ratio", "book_ratio",
    "wavg_bid_gap", "wavg_ask_gap",
    "buy_ratio", "net_amt", "trade_n", "avg_trade_amt",
    "large_net", "large_ratio", "xlarge_net", "dir_autocorr",
    "order_n", "big_order_n", "cancel_n", "order_trade_ratio", "cancel_rate",
]


def build_sql(date: str, minute_path: Path, cutoff_hm: str = "14:50") -> str:
    ymd = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    cutoff = f"TIMESTAMP '{ymd} {cutoff_hm}:00'"
    hh, mm = int(cutoff_hm[:2]), int(cutoff_hm[3:5])
    total = hh * 60 + mm
    def _t(delta: int) -> str:
        m = total - delta
        return f"TIMESTAMP '{ymd} {m // 60:02d}:{m % 60:02d}:00'"
    t5, t15, t30 = _t(5), _t(15), _t(30)

    bidv_sum = " + ".join(f"coalesce(bidv{i}, 0)" for i in range(1, 11))
    askv_sum = " + ".join(f"coalesce(askv{i}, 0)" for i in range(1, 11))
    bd1 = " + ".join(f"coalesce(bidv{i}, 0)" for i in range(1, 2))
    bd5 = " + ".join(f"coalesce(bidv{i}, 0)" for i in range(1, 6))
    bd10 = " + ".join(f"coalesce(bidv{i}, 0)" for i in range(1, 11))
    ad1 = " + ".join(f"coalesce(askv{i}, 0)" for i in range(1, 2))
    ad5 = " + ".join(f"coalesce(askv{i}, 0)" for i in range(1, 6))
    ad10 = " + ".join(f"coalesce(askv{i}, 0)" for i in range(1, 11))

    return f"""
WITH b AS (
    SELECT * FROM read_parquet('{minute_path}')
    WHERE time <= {cutoff}
), grid AS (
    SELECT symbol,
           count(*) AS n_bars,
           arg_max(close, time) AS px,
           arg_min(open, time) AS day_open,
           max(high) AS day_high,
           min(low) AS day_low,
           sum(volume) AS cum_volume,
           sum(amount) AS cum_amount,
           arg_max(close, time) FILTER (WHERE time <= {t5}) AS c5,
           arg_max(close, time) FILTER (WHERE time <= {t15}) AS c15,
           arg_max(close, time) FILTER (WHERE time <= {t30}) AS c30,
           arg_max(bid1, time) AS bid1,
           arg_max(ask1, time) AS ask1,
           arg_max(total_bidv, time) AS total_bidv,
           arg_max(total_askv, time) AS total_askv,
           arg_max({bd1}, time) AS bid_depth_1,
           arg_max({bd5}, time) AS bid_depth_5,
           arg_max({bd10}, time) AS bid_depth_10,
           arg_max({ad1}, time) AS ask_depth_1,
           arg_max({ad5}, time) AS ask_depth_5,
           arg_max({ad10}, time) AS ask_depth_10,
           arg_max(wavg_bid, time) AS wavg_bid,
           arg_max(wavg_ask, time) AS wavg_ask,
           sum(buy_amount) AS buy_amount,
           sum(sell_amount) AS sell_amount,
           sum(buy_volume) AS buy_volume,
           sum(sell_volume) AS sell_volume,
           sum(trade_n) AS trade_n,
           sum(large_buy_amount) AS large_buy_amount,
           sum(large_sell_amount) AS large_sell_amount,
           sum(xlarge_buy_amount) AS xlarge_buy_amount,
           sum(xlarge_sell_amount) AS xlarge_sell_amount,
           sum(order_n) AS order_n,
           sum(big_order_n) AS big_order_n,
           sum(cancel_n) AS cancel_n
    FROM b
    GROUP BY symbol
), vol AS (
    SELECT symbol,
           stddev_samp(r) FILTER (WHERE time > {t15}) AS vol_15m,
           stddev_samp(r) FILTER (WHERE time > {t30}) AS vol_30m
    FROM (
        SELECT symbol, time,
               close / lag(close) OVER (PARTITION BY symbol ORDER BY time) - 1.0 AS r
        FROM b
    )
    GROUP BY symbol
), flow AS (
    SELECT symbol,
           corr(sign_net, lag_sign) AS dir_autocorr
    FROM (
        SELECT symbol, time,
               sign(buy_amount - sell_amount) AS sign_net,
               lag(sign(buy_amount - sell_amount)) OVER (PARTITION BY symbol ORDER BY time)
                   AS lag_sign
        FROM b
    )
    GROUP BY symbol
)
SELECT
    g.symbol,
    '{date}' AS date,
    '{cutoff_hm}' AS cutoff,
    g.n_bars,
    g.px,
    g.day_open,
    g.day_high,
    g.day_low,
    g.px / nullif(g.day_open, 0) - 1.0 AS ret_open,
    (g.px - g.day_low) / nullif(g.day_high - g.day_low, 0) AS range_pos,
    g.cum_volume,
    g.cum_amount,
    g.cum_amount * 1e4 / nullif(g.cum_volume, 0) AS vwap,
    g.px / nullif(g.cum_amount * 1e4 / nullif(g.cum_volume, 0), 0) - 1.0 AS vwap_gap,
    g.px / nullif(g.c5, 0) - 1.0 AS ret_5m,
    g.px / nullif(g.c15, 0) - 1.0 AS ret_15m,
    g.px / nullif(g.c30, 0) - 1.0 AS ret_30m,
    v.vol_15m,
    v.vol_30m,
    g.bid1,
    g.ask1,
    (g.ask1 - g.bid1) / nullif((g.ask1 + g.bid1) / 2.0, 0) AS spread,
    g.bid_depth_1,
    g.bid_depth_5,
    g.bid_depth_10,
    g.ask_depth_1,
    g.ask_depth_5,
    g.ask_depth_10,
    (g.bid_depth_1 - g.ask_depth_1) / nullif(g.bid_depth_1 + g.ask_depth_1, 0) AS imb_1,
    (g.bid_depth_5 - g.ask_depth_5) / nullif(g.bid_depth_5 + g.ask_depth_5, 0) AS imb_5,
    (g.bid_depth_10 - g.ask_depth_10) / nullif(g.bid_depth_10 + g.ask_depth_10, 0) AS imb_10,
    g.bid_depth_1 / nullif(g.total_bidv, 0) AS big_bid_ratio,
    g.total_bidv / nullif(g.total_askv, 0) AS book_ratio,
    g.wavg_bid / nullif(g.px, 0) - 1.0 AS wavg_bid_gap,
    g.wavg_ask / nullif(g.px, 0) - 1.0 AS wavg_ask_gap,
    g.buy_amount / nullif(g.buy_amount + g.sell_amount, 0) AS buy_ratio,
    g.buy_amount - g.sell_amount AS net_amt,
    g.trade_n,
    (g.buy_amount + g.sell_amount) / nullif(g.trade_n, 0) AS avg_trade_amt,
    g.large_buy_amount - g.large_sell_amount AS large_net,
    (g.large_buy_amount + g.large_sell_amount)
        / nullif(g.buy_amount + g.sell_amount, 0) AS large_ratio,
    g.xlarge_buy_amount - g.xlarge_sell_amount AS xlarge_net,
    f.dir_autocorr,
    g.order_n,
    g.big_order_n,
    g.cancel_n,
    g.order_n / nullif(g.trade_n, 0) AS order_trade_ratio,
    g.cancel_n / nullif(g.order_n, 0) AS cancel_rate
FROM grid AS g
LEFT JOIN vol AS v USING (symbol)
LEFT JOIN flow AS f USING (symbol)
ORDER BY g.symbol
"""


def _cast_sql(inner: str) -> str:
    casts = ",\n       ".join(
        f"CAST({c} AS DOUBLE) AS {c}" if c not in ("symbol", "date", "cutoff") else c
        for c in FEATURE_COLS
    )
    return f"SELECT {casts} FROM ({inner}) AS q ORDER BY symbol, cutoff"


def build_day(date: str, minute_path: Path, out_path: Path) -> dict:
    started = time.time()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / ".data.parquet.tmp"
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=6")
    con.execute("SET preserve_insertion_order=false")
    try:
        union = " UNION ALL ".join(
            f"SELECT * FROM ({build_sql(date, minute_path, c)})" for c in CUTOFFS
        )
        con.execute(
            f"COPY ({_cast_sql(union)}) TO '{tmp}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{tmp}')").fetchone()[0]
    finally:
        con.close()
    tmp.replace(out_path)
    meta = {
        "date": date,
        "rows": int(rows),
        "size_mb": round(out_path.stat().st_size / 1e6, 2),
        "elapsed_s": round(time.time() - started, 1),
    }
    return meta


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    args = ap.parse_args()

    if args.probe:
        date = args.probe
        src = MIN1_DIR / f"dt={date}" / "data.parquet"
        if not src.exists():
            raise SystemExit(f"{src} 不存在，先构建分钟表")
        meta = build_day(date, src, OUT / f"probe_{date}.parquet")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        con = duckdb.connect()
        print(con.sql(f"SELECT * FROM read_parquet('{OUT}/probe_{date}.parquet') LIMIT 3")
              .df().T.to_string())
        print(con.sql(f"""SELECT count(*) n,
            avg(CASE WHEN px IS NULL THEN 1 ELSE 0 END) px_null,
            avg(CASE WHEN bid1 IS NULL THEN 1 ELSE 0 END) bid_null,
            avg(CASE WHEN dir_autocorr IS NULL THEN 1 ELSE 0 END) ac_null,
            min(n_bars) min_bars, median(n_bars) med_bars, max(n_bars) max_bars
            FROM read_parquet('{OUT}/probe_{date}.parquet')""").df().to_string())
        con.close()
        return

    if not args.all:
        return
    days = sorted(p.name.split("=")[1] for p in MIN1_DIR.glob("dt=*"))
    days = [d for d in days if (not args.start or d >= args.start)
            and (not args.end or d <= args.end)]
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.time()
    done = 0
    for i, date in enumerate(days, 1):
        out = OUT / f"date={date}" / "data.parquet"
        if out.exists():
            done += 1
            continue
        src = MIN1_DIR / f"dt={date}" / "data.parquet"
        try:
            meta = build_day(date, src, out)
            done += 1
            log.info("%s: %d 只，%.0fs", date, meta["rows"], meta["elapsed_s"])
        except Exception as exc:  # pragma: no cover
            log.warning("%s 失败：%s", date, exc)
        if i % 20 == 0:
            print(f"[{i}/{len(days)}] 累计 {done}，{(time.time() - started) / 60:.1f} 分钟",
                  flush=True)
    print(f"完成 {done}/{len(days)}", flush=True)


if __name__ == "__main__":
    main()
