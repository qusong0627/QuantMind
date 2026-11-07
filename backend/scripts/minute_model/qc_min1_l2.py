"""L2 分钟表质检：逐日与 QuantDB 日线对数 + 结构检查。

检查项：
1. 结构：行数、股票数、每股分钟数（正常 241 根）、重复 (symbol,time)；
2. 单位：分钟量/额求和 vs 日线 volume/amount（应完全对齐，相对误差 ~0）；
3. 价格：最后一根 close vs 日线 close；
4. 覆盖：日线有、分钟缺（按市场拆分，BJ 本身不在 L2 范围）；
5. 脏数据：close 空、bid1 空、负 volume、high<low、零成交分钟。

用法：
  .venv/bin/python -m backend.scripts.minute_model.qc_min1_l2 --day 20240401
  .venv/bin/python -m backend.scripts.minute_model.qc_min1_l2 --all
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import duckdb

from backend.scripts.minute_model.build_min1_l2 import OUT

log = logging.getLogger("minute_model.qc")
DAILY = OUT.parent / "daily_unadjusted"
QC_DIR = OUT / "_qc"


def _day_sql(minute_path: Path, daily_path: Path) -> str:
    return f"""
WITH m AS (
    SELECT symbol, count(*) AS n_bars, count(DISTINCT time) AS n_times,
           sum(volume) AS vol, sum(amount) AS amt,
           arg_max(close, time) AS m_close,
           count(*) FILTER (WHERE close IS NULL) AS close_null,
           count(*) FILTER (WHERE bid1 IS NULL) AS bid1_null,
           count(*) FILTER (WHERE volume < 0) AS neg_vol,
           count(*) FILTER (WHERE high < low) AS bad_hl,
           count(*) FILTER (WHERE trade_n = 0) AS zero_trade,
           median(abs(bid1 / nullif(close, 0) - 1)) AS bid_dev_symbol,
           median(abs(wavg_bid / nullif(close, 0) - 1)) AS wavg_dev_symbol
    FROM read_parquet('{minute_path}')
    GROUP BY symbol
), d AS (
    SELECT symbol, volume AS d_vol, amount AS d_amt, close AS d_close
    FROM read_parquet('{daily_path}')
), j AS (
    SELECT m.*, d.d_vol, d.d_amt, d.d_close
    FROM m LEFT JOIN d USING (symbol)
)
SELECT
    (SELECT count(*) FROM read_parquet('{minute_path}')) AS rows,
    (SELECT count(DISTINCT symbol) FROM read_parquet('{minute_path}')) AS symbols,
    (SELECT count(*) - count(DISTINCT symbol || '|' || CAST(time AS VARCHAR))
       FROM read_parquet('{minute_path}')) AS dup_rows,
    count(*) FILTER (WHERE d_vol IS NOT NULL) AS in_daily,
    count(*) FILTER (WHERE d_vol IS NULL) AS not_in_daily,
    median(abs(vol - d_vol) / nullif(d_vol, 0)) AS vol_relerr_med,
    quantile_cont(abs(vol - d_vol) / nullif(d_vol, 0), 0.95) AS vol_relerr_p95,
    median(abs(amt - d_amt) / nullif(d_amt, 0)) AS amt_relerr_med,
    quantile_cont(abs(amt - d_amt) / nullif(d_amt, 0), 0.95) AS amt_relerr_p95,
    median(abs(m_close - d_close) / nullif(d_close, 0)) AS px_relerr_med,
    quantile_cont(abs(m_close - d_close) / nullif(d_close, 0), 0.95) AS px_relerr_p95,
    median(bid_dev_symbol) AS bid_dev_med,
    quantile_cont(bid_dev_symbol, 0.99) AS bid_dev_p99,
    median(wavg_dev_symbol) AS wavg_dev_med,
    median(n_bars) AS bars_med,
    min(n_bars) AS bars_min,
    sum(close_null) AS close_null,
    sum(bid1_null) AS bid1_null,
    sum(neg_vol) AS neg_vol,
    sum(bad_hl) AS bad_hl,
    sum(zero_trade) AS zero_trade
FROM j
"""


def _missing_sql(minute_path: Path, daily_path: Path) -> str:
    return f"""
WITH d AS (SELECT symbol FROM read_parquet('{daily_path}')),
     m AS (SELECT DISTINCT symbol FROM read_parquet('{minute_path}'))
SELECT d.symbol FROM d ANTI JOIN m USING (symbol)
"""


def check_day(date: str) -> dict | None:
    minute_path = OUT / f"dt={date}" / "data.parquet"
    daily_path = DAILY / f"dt={date}" / "data.parquet"
    if not minute_path.exists():
        return None
    if not daily_path.exists():
        return {"date": date, "status": "no_daily"}
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=8")
    try:
        row = con.execute(_day_sql(minute_path, daily_path)).fetchone()
        cols = [d[0] for d in con.description]
        stats = dict(zip(cols, row))
        missing = [r[0] for r in con.execute(_missing_sql(minute_path, daily_path)).fetchall()]
    finally:
        con.close()
    by_market: dict[str, int] = {}
    for sym in missing:
        suffix = sym.rsplit(".", 1)[-1] if "." in sym else "?"
        by_market[suffix] = by_market.get(suffix, 0) + 1
    stats["date"] = date
    stats["missing_daily_only"] = len(missing)
    stats["missing_by_market"] = by_market
    stats["missing_sample"] = sorted(missing)[:20]
    for key in ("vol_relerr_med", "vol_relerr_p95", "amt_relerr_med",
                "amt_relerr_p95", "px_relerr_med", "px_relerr_p95",
                "bid_dev_med", "bid_dev_p99", "wavg_dev_med"):
        stats[key] = None if stats[key] is None else round(float(stats[key]), 10)
    stats["bars_med"] = int(stats["bars_med"]) if stats["bars_med"] is not None else None
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    args = ap.parse_args()

    QC_DIR.mkdir(parents=True, exist_ok=True)
    if args.day:
        stats = check_day(args.day)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return
    if not args.all:
        return

    days = sorted(p.name.split("=")[1] for p in OUT.glob("dt=*"))
    days = [d for d in days if (not args.start or d >= args.start)
            and (not args.end or d <= args.end)]
    started = time.time()
    ok = 0
    for i, date in enumerate(days, 1):
        try:
            stats = check_day(date)
        except Exception as exc:  # pragma: no cover
            log.warning("%s 质检失败：%s", date, exc)
            stats = {"date": date, "status": f"error:{exc}"}
        if stats:
            (QC_DIR / f"dt={date}.json").write_text(
                json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            ok += 1
        if i % 20 == 0 or i == len(days):
            print(f"[{i}/{len(days)}] {date} ok={ok} "
                  f"（{(time.time() - started) / 60:.1f} 分钟）", flush=True)

    # 汇总：各指标分布
    reports = []
    for p in sorted(QC_DIR.glob("dt=*.json")):
        reports.append(json.loads(p.read_text(encoding="utf-8")))
    good = [r for r in reports if r.get("rows")]
    if good:
        import statistics as st

        summary = {
            "days": len(good),
            "vol_relerr_p95_max": max(r["vol_relerr_p95"] or 0 for r in good),
            "px_relerr_p95_max": max(r["px_relerr_p95"] or 0 for r in good),
            "bid_dev_med_max": max(r["bid_dev_med"] or 0 for r in good),
            "wavg_dev_med_max": max(r["wavg_dev_med"] or 0 for r in good),
            "bars_med_min": min(r["bars_med"] or 0 for r in good),
            "missing_daily_only_med": st.median(r["missing_daily_only"] for r in good),
            "bad_hl_days": sum(1 for r in good if (r["bad_hl"] or 0) > 0),
            "neg_vol_days": sum(1 for r in good if (r["neg_vol"] or 0) > 0),
        }
        (QC_DIR / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
