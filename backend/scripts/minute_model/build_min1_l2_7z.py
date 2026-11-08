"""把 2017-2022 的 L2 原始 7z 包落成 QuantDB 分钟表（l2_min1），口径与 parquet 版完全一致。

## 数据形态

`/media/zbox/nas-NSF/L2/{年}/{月|YYYYMM}/{YYYYMMDD}.7z`，每天一个 **solid 单块**压缩包
（约 600-700 MB，解压后 ~7.8 GB、10,412 个 CSV、约 3,000 只股票 × 4 个文件）：

    20170103/000001.SZ/行情.csv     快照（10 档盘口 + 累计量额）
    20170103/000001.SZ/逐笔成交.csv  逐笔成交（价格万分之一元、量股、BS标志）
    20170103/000001.SZ/逐笔委托.csv  逐笔委托
    20170103/000001.SZ/委托队列.csv  委托队列（本表不用）

CSV 是 **GBK** 编码，但数据行全是 ASCII——跳过头行、按位置读列即可，不需要转码。

## 覆盖（2026-11-07 清点，两个源合并）

| 年 | 天数 | 来源 |
|---|---:|---|
| 2017 | 244 | NSF |
| 2018 | 243 | NSF |
| 2019 | 244 | NSF |
| 2020 | 239 | NSF |
| 2021 | 244 | NSF |
| 2022 | **242（全年）** | 1-8 月 toshiba Leve2 + 8-12 月 NSF |
| 合计 | **1,449 天** | 2023 年 7z 为空，用 parquet 仓库的 169 天 |

## 实测成本

2017-01-03（7.8GB 解压、2,837 只）：解压 6.2s + 解析 5.5s + 聚合 ~12s = **24s**；
2022-01-04（27.1GB 解压、4,986 只）预计 **60-90s**。全量约 **18-20 小时**（单进程）。

用法（宿主机仓库根）：
  .venv/bin/python -m backend.scripts.minute_model.build_min1_l2_7z --probe 20170103
  .venv/bin/python -m backend.scripts.minute_model.build_min1_l2_7z --all --workers 2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import duckdb

from backend.scripts.minute_model.build_min1_l2 import (
    BIG_ORDER_AMT,
    FINAL_COLS,
    LARGE_AMT,
    OUT,
    XLARGE_AMT,
    _connect,
    _mem_available_gb,
    _wait_for_memory,
)

log = logging.getLogger("minute_model.min1_7z")

ARCHIVE_ROOTS = (
    Path("/media/zbox/nas-NSF/L2"),
    Path("/media/zbox/nas-toshiba/Leve2"),
)
STAGE_ROOT = Path("/media/zbox/data/quantmind/tmp/l2_7z")

# 时间：HHMMSSmmm -> minute(HHMM)；bar 与 parquet 版同一口径
MINUTE_EXPR = "((CAST(c3 AS BIGINT) // 10000000) * 100 + ((CAST(c3 AS BIGINT) // 100000) % 100))"
BAR_EXPR = """
CASE
  WHEN minute <= 925 THEN 925
  WHEN minute BETWEEN 930 AND 1129 THEN
      CASE WHEN minute % 100 = 59 THEN (minute // 100 + 1) * 100 ELSE minute + 1 END
  WHEN minute = 1130 THEN 1130
  WHEN minute BETWEEN 1300 AND 1459 THEN
      CASE WHEN minute % 100 = 59 THEN (minute // 100 + 1) * 100 ELSE minute + 1 END
  WHEN minute = 1500 THEN 1500
END
"""
SESSION = (
    "((minute BETWEEN 915 AND 925) OR (minute BETWEEN 930 AND 1130) "
    "OR (minute BETWEEN 1300 AND 1500))"
)
# 只留股票：0/3 开头（深市）、6 开头（沪市），剔除 2/4/5/8/9（B 股/基金/债/北交所）
STOCK = (
    "(c0 LIKE '0%' OR c0 LIKE '3%' OR c0 LIKE '6%') "
    "AND c0 NOT LIKE '2%' AND c0 NOT LIKE '4%' AND c0 NOT LIKE '5%' "
    "AND c0 NOT LIKE '8%' AND c0 NOT LIKE '9%'"
)
SNAP_COLS = {f"c{i}": "VARCHAR" for i in range(67)}
TRADE_COLS = {f"c{i}": "VARCHAR" for i in range(13)}
ORDER_COLS = {f"c{i}": "VARCHAR" for i in range(11)}


def available_days(roots: tuple[Path, ...] = ARCHIVE_ROOTS) -> dict[str, Path]:
    """日期 -> 7z 路径。兼容 {年}/{月}/{日}.7z 与 {年}/{YYYYMM}/ 两种目录形态。

    两个源合并（后者覆盖前者）：NSF 的 2017-2021 + 2022 年 8-12 月，
    toshiba Leve2 的 2022 年 1-8 月——合起来 2022 全年 242 天齐全。
    """
    days: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.glob("*/*/*.7z"):
            stem = p.stem
            if len(stem) == 8 and stem.isdigit():
                days[stem] = p
    return days


def _csv(name: str, base: Path, cols: dict[str, str], extra: str = "") -> str:
    # auto_detect=false 必须开：停牌股会留下只有表头的空 CSV，
    # DuckDB 嗅探空文件会直接报 "Error when sniffing"（2026-11-08 实测 350 天因此失败）。
    # null_padding=true 必须开：部分年份的行少一个尾随逗号（委托表 10 vs 11 列）。
    return (
        f"read_csv('{base}/*/{name}.csv', header=false, skip=1, "
        f"auto_detect=false, null_padding=true, encoding='latin-1', columns={cols}{extra})"
    )


def build_sql(date: str, base: Path) -> str:
    snap = _csv("行情", base, SNAP_COLS)
    trades = _csv("逐笔成交", base, TRADE_COLS)
    orders = _csv("逐笔委托", base, ORDER_COLS)

    levels = ",\n           ".join(
        [f"CAST(c{i} AS DOUBLE) AS ask{i-16}" for i in range(17, 27)]
        + [f"CAST(c{i} AS DOUBLE) AS askv{i-26}" for i in range(27, 37)]
        + [f"CAST(c{i} AS DOUBLE) AS bid{i-36}" for i in range(37, 47)]
        + [f"CAST(c{i} AS DOUBLE) AS bidv{i-46}" for i in range(47, 57)]
    )
    snap_aggs = [
        "arg_min(px, time_ms) FILTER (WHERE isfinite(px) AND px > 0) AS open",
        "max(px) FILTER (WHERE isfinite(px) AND px > 0) AS high",
        "min(px) FILTER (WHERE isfinite(px) AND px > 0) AS low",
        "arg_max(px, time_ms) FILTER (WHERE isfinite(px) AND px > 0) AS close",
        "arg_max(cum_volume, time_ms) AS cum_volume",
        "arg_max(cum_amount, time_ms) AS cum_amount",
    ]
    for i in range(1, 11):
        snap_aggs.append(
            f"arg_max(bid{i}, time_ms) FILTER (WHERE isfinite(bid{i}) AND bid{i} > 0) "
            f"/ 1e4 AS bid{i}"
        )
        snap_aggs.append(
            f"arg_max(ask{i}, time_ms) FILTER (WHERE isfinite(ask{i}) AND ask{i} > 0) "
            f"/ 1e4 AS ask{i}"
        )
        snap_aggs.append(f"arg_max(bidv{i}, time_ms) AS bidv{i}")
        snap_aggs.append(f"arg_max(askv{i}, time_ms) AS askv{i}")
    snap_aggs_sql = ",\n           ".join(snap_aggs)

    bidv_sum = " + ".join(f"coalesce(bidv{i}, 0)" for i in range(1, 11))
    askv_sum = " + ".join(f"coalesce(askv{i}, 0)" for i in range(1, 11))
    wavg_bid_num = " + ".join(
        f"coalesce(bid{i}, 0) * coalesce(bidv{i}, 0)" for i in range(1, 11)
    )
    wavg_ask_num = " + ".join(
        f"coalesce(ask{i}, 0) * coalesce(askv{i}, 0)" for i in range(1, 11)
    )

    return f"""
WITH s0 AS (
    SELECT c0 AS wd_code, {MINUTE_EXPR} AS minute, CAST(c3 AS BIGINT) AS time_ms,
           CAST(c4 AS DOUBLE) AS px_raw, CAST(c11 AS DOUBLE) AS cum_volume,
           CAST(c12 AS DOUBLE) AS cum_amount,
           {levels}
    FROM {snap}
    WHERE {STOCK}
), s AS (
    SELECT wd_code, time_ms, px_raw / 1e4 AS px, cum_volume, cum_amount,
           {", ".join(f"bid{i}, ask{i}, bidv{i}, askv{i}" for i in range(1, 11))},
           {BAR_EXPR} AS bar
    FROM s0
    WHERE {SESSION}
), b AS (
    SELECT wd_code, bar, {snap_aggs_sql}
    FROM s WHERE bar IS NOT NULL
    GROUP BY 1, 2
), bars AS (
    SELECT wd_code, bar, open, high, low, close,
           greatest(cum_volume - coalesce(lag(cum_volume) OVER w, 0), 0) AS volume,
           greatest(cum_amount - coalesce(lag(cum_amount) OVER w, 0), 0) / 1e4 AS amount,
           {", ".join(f"bid{i}, ask{i}, bidv{i}, askv{i}" for i in range(1, 11))},
           ({wavg_bid_num}) / nullif({bidv_sum}, 0) AS wavg_bid,
           ({wavg_ask_num}) / nullif({askv_sum}, 0) AS wavg_ask,
           ({bidv_sum}) AS total_bidv,
           ({askv_sum}) AS total_askv
    FROM b
    WINDOW w AS (PARTITION BY wd_code ORDER BY bar)
), tr AS (
    SELECT wd_code, bar,
           sum(qty) FILTER (WHERE dir = 'B') AS buy_volume,
           sum(qty) FILTER (WHERE dir = 'S') AS sell_volume,
           sum(amt) FILTER (WHERE dir = 'B') / 1e4 AS buy_amount,
           sum(amt) FILTER (WHERE dir = 'S') / 1e4 AS sell_amount,
           sum(amt) FILTER (WHERE dir = 'B' AND amt >= {LARGE_AMT}) / 1e4 AS large_buy_amount,
           sum(amt) FILTER (WHERE dir = 'S' AND amt >= {LARGE_AMT}) / 1e4 AS large_sell_amount,
           sum(amt) FILTER (WHERE dir = 'B' AND amt >= {XLARGE_AMT}) / 1e4 AS xlarge_buy_amount,
           sum(amt) FILTER (WHERE dir = 'S' AND amt >= {XLARGE_AMT}) / 1e4 AS xlarge_sell_amount,
           count(*) AS trade_n
    FROM (
        SELECT c0 AS wd_code, {MINUTE_EXPR} AS minute,
               CAST(c9 AS DOUBLE) AS qty, c7 AS dir,
               CAST(c9 AS DOUBLE) * CAST(c8 AS DOUBLE) / 1e4 AS amt,
               {BAR_EXPR} AS bar
        FROM {trades}
        WHERE {STOCK} AND c7 IN ('B', 'S') AND CAST(c8 AS DOUBLE) > 0
          AND CAST(c9 AS DOUBLE) > 0
    )
    WHERE bar IS NOT NULL AND {SESSION}
    GROUP BY 1, 2
), od AS (
    SELECT wd_code, bar,
           count(*) AS order_n,
           count(*) FILTER (WHERE otype = '1') AS cancel_n,
           count(*) FILTER (WHERE px_raw / 1e4 * qty >= {BIG_ORDER_AMT}) AS big_order_n
    FROM (
        SELECT c0 AS wd_code, {MINUTE_EXPR} AS minute,
               CAST(c8 AS DOUBLE) AS px_raw, CAST(c9 AS DOUBLE) AS qty,
               c6 AS otype, {BAR_EXPR} AS bar
        FROM {orders}
        WHERE {STOCK}
    )
    WHERE bar IS NOT NULL AND {SESSION}
    GROUP BY 1, 2
), joined AS (
    SELECT coalesce(bars.wd_code, tr.wd_code, od.wd_code) AS wd_code,
           coalesce(bars.bar, tr.bar, od.bar) AS bar,
           bars.open, bars.high, bars.low, bars.close, bars.volume, bars.amount,
           {", ".join(f"bars.bid{i}, bars.ask{i}, bars.bidv{i}, bars.askv{i}" for i in range(1, 11))},
           bars.wavg_bid, bars.wavg_ask, bars.total_bidv, bars.total_askv,
           coalesce(tr.buy_volume, 0) AS buy_volume,
           coalesce(tr.sell_volume, 0) AS sell_volume,
           coalesce(tr.buy_amount, 0) AS buy_amount,
           coalesce(tr.sell_amount, 0) AS sell_amount,
           coalesce(tr.large_buy_amount, 0) AS large_buy_amount,
           coalesce(tr.large_sell_amount, 0) AS large_sell_amount,
           coalesce(tr.xlarge_buy_amount, 0) AS xlarge_buy_amount,
           coalesce(tr.xlarge_sell_amount, 0) AS xlarge_sell_amount,
           coalesce(tr.trade_n, 0) AS trade_n,
           coalesce(od.order_n, 0) AS order_n,
           coalesce(od.cancel_n, 0) AS cancel_n,
           coalesce(od.big_order_n, 0) AS big_order_n
    FROM bars
    FULL OUTER JOIN tr ON bars.wd_code = tr.wd_code AND bars.bar = tr.bar
    FULL OUTER JOIN od ON coalesce(bars.wd_code, tr.wd_code) = od.wd_code
                      AND coalesce(bars.bar, tr.bar) = od.bar
)
SELECT * FROM joined
"""


def final_sql(date: str, base: Path) -> str:
    base_sql = build_sql(date, base)
    casts = ",\n       ".join(f"CAST({c} AS FLOAT) AS {c}" for c in FINAL_COLS[2:])
    return f"""
SELECT wd_code AS symbol,
       CAST(strptime('{date} ' || lpad(CAST(bar AS VARCHAR), 4, '0'), '%Y%m%d %H%M')
            AS TIMESTAMP) AS time,
       {casts}
FROM (
{base_sql}
) AS j
ORDER BY wd_code, bar
"""


def extract(archive: Path, dest: Path) -> float:
    started = time.time()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["7z", "x", str(archive), f"-o{dest}", "-mmt=20", "-bso0", "-bsp0"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return time.time() - started


def build_day(
    date: str,
    archive: Path,
    out_path: Path,
    memory_limit: str,
    threads: int,
    min_free_gb: float = 0.0,
    keep: bool = False,
) -> dict:
    started = time.time()
    if min_free_gb > 0:
        _wait_for_memory(min_free_gb, date)
    dest = STAGE_ROOT / date
    t_extract = extract(archive, dest)
    # 7z 解压后是 {dest}/{date}/{symbol}/xxx.csv
    base = dest / date
    if not base.is_dir():
        candidates = [p for p in dest.iterdir() if p.is_dir()]
        if not candidates:
            raise RuntimeError(f"{date}: 解压后找不到数据目录")
        base = candidates[0]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_path.parent / ".duckdb_tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = out_path.parent / ".data.parquet.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    con = _connect(memory_limit, threads, tmp_dir)
    try:
        con.execute(
            f"COPY ({final_sql(date, base)}) TO '{tmp_path}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 131072)"
        )
        stats = con.execute(
            f"SELECT count(*) AS rows, count(DISTINCT symbol) AS symbols, "
            f"count(DISTINCT time) AS bars, "
            f"median(abs(bid1 / nullif(close, 0) - 1)) AS bid_dev, "
            f"median(abs(wavg_bid / nullif(close, 0) - 1)) AS wavg_dev "
            f"FROM read_parquet('{tmp_path}')"
        ).fetchone()
        rows, symbols, bars, bid_dev, wavg_dev = stats
        if bid_dev is not None and bid_dev > 0.2:
            raise ValueError(f"{date}: 盘口量纲异常（bid1 偏差 {bid_dev:.3f}），拒绝落库")
        if wavg_dev is not None and wavg_dev > 0.2:
            raise ValueError(f"{date}: 加权价量纲异常（{wavg_dev:.3f}），拒绝落库")
    finally:
        con.close()
        if not keep:
            shutil.rmtree(dest, ignore_errors=True)

    tmp_path.replace(out_path)
    meta = {
        "date": date,
        "source": str(archive),
        "rows": int(rows),
        "symbols": int(symbols),
        "bars": int(bars),
        "size_mb": round(out_path.stat().st_size / 1e6, 1),
        "extract_s": round(t_extract, 1),
        "elapsed_s": round(time.time() - started, 1),
    }
    (out_path.parent / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return meta


def _worker(payload):
    date, archive, out, mem, threads, min_free, keep = payload
    path = Path(out) / f"dt={date}" / "data.parquet"
    if path.exists():
        return date, "skip"
    try:
        meta = build_day(date, Path(archive), path, mem, threads, min_free, keep)
    except Exception as exc:  # pragma: no cover
        log.exception("%s 失败", date)
        return date, f"fail:{exc}"
    log.info(
        "%s: %d 行 × %d 只，%.0f MB，解压 %.1fs / 合计 %.0fs",
        date, meta["rows"], meta["symbols"], meta["size_mb"],
        meta["extract_s"], meta["elapsed_s"],
    )
    return date, "ok"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--memory-limit", default="5GB")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--min-free-gb", type=float, default=14.0)
    ap.add_argument("--keep-extracted", action="store_true")
    args = ap.parse_args()

    days = available_days()
    if not days:
        raise SystemExit(f"未找到 7z 归档：{ARCHIVE_ROOTS}")
    print(f"7z 可用交易日 {len(days)} 天：{min(days)} ~ {max(days)}", flush=True)
    STAGE_ROOT.mkdir(parents=True, exist_ok=True)

    if args.probe:
        date = args.probe
        if date not in days:
            raise SystemExit(f"{date} 不在 7z 归档内")
        out = OUT / "_probe_7z" / f"dt={date}" / "data.parquet"
        meta = build_day(date, days[date], out, args.memory_limit, args.threads,
                         args.min_free_gb, keep=True)
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        con = duckdb.connect()
        print(con.sql(f"SELECT * FROM read_parquet('{out}') LIMIT 3").df().T.to_string())
        con.close()
        return

    if not args.all:
        return
    targets = [d for d in sorted(days)
               if (not args.start or d >= args.start) and (not args.end or d <= args.end)]
    payloads = [(d, str(days[d]), str(OUT), args.memory_limit, args.threads,
                 args.min_free_gb, args.keep_extracted) for d in targets]
    started = time.time()
    done = 0
    if args.workers > 1:
        from multiprocessing import Pool

        with Pool(args.workers) as pool:
            for date, status in pool.imap_unordered(_worker, payloads, chunksize=1):
                done += 1
                print(f"[{done}/{len(targets)}] {date} {status} "
                      f"（{(time.time() - started) / 60:.1f} 分钟）", flush=True)
    else:
        for payload in payloads:
            date, status = _worker(payload)
            done += 1
            print(f"[{done}/{len(targets)}] {date} {status} "
                  f"（{(time.time() - started) / 60:.1f} 分钟）", flush=True)
    print(f"完成 {done}/{len(targets)}，用时 {(time.time() - started) / 60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    main()
