"""把 L2 原始 parquet 聚合为 QuantDB 统一格式的分钟表（1_kline_data/l2_min1）。

## 为什么要用 DuckDB（2026-11-07 教训）

单日原始数据是 snapshot 2520 万行 + trades 1.24 亿行 + orders 1.87 亿行（16 个
shard）。用 pandas 整天读入，单进程峰值就要 10 GB+；开 4 进程直接触发全局 OOM，
把 60G 主机连同生产栈一起打穿（内核 15:03:05 oom-kill，机器冻结）。

DuckDB 走列裁剪 + 哈希聚合 + 磁盘溢写，单进程内存上限可压到 6 GB，
且 8 线程并行扫 parquet。

## 单位与 QuantDB 口径

- 价格：元（L2 快照的万分之一元 / 1e4）
- 成交量：股
- 成交额：**万元**（L2 的元 / 1e4，与 daily_* 的 amount 一致）
- 时间：分钟收盘标记（09:31 表示 09:30:00~09:30:59；15:00 含收盘集合竞价）

## 清洗规则

1. **标的**：只留 `prod='stock'` 的沪深股票，剔除 B 股（SH 900xxx / SZ 200xxx）。
   ETF/可转债/基金/债券不进本表（后续如需要另开表）。
2. **时段**：09:15~09:25（集合竞价，归入 09:25 bar）、09:30~11:30、13:00~15:00。
   午休与 09:26~09:29 的盘前快照一律丢弃。
3. **成交有效性**：trades 只取 `is_valid=true` 且 `dir in ('B','S')`。
   `is_valid=false` 的行 price/amount 全为 0，是沪深 L2 里的撤单记录，不是成交。
4. **价格**：`成交价<=0 / NaN / Inf` 不参与 OHLC；整分钟无有效价则 OHLC 为 NULL。
5. **盘口**：申买/申卖价 `<=0` 视为无挂单（NULL）；档位量保留原始股数。
6. **累计量额**：分钟量额 = 当日累计差分，负差（数据重置）截断为 0。
7. **加权叫买/叫卖价**：Wind 的 `加权平均叫买价/叫卖价` 两列数值异常
   （曾见 10.24 / 11.01 而收盘 10.52），**弃用**，改由 10 档价格×量自行加权。
8. **撤单**：数据源 `is_cancel` 恒为 False（全表扫描确认），`cancel_n` 只能用
   `otype='1'` 计数，占比 <0.5%，**撤单类特征不可靠**，QC 报告里会标注。

## 输出

`{quantdb}/1_kline_data/l2_min1/dt=YYYYMMDD/data.parquet`，每股每分钟一行，
约 64 列（OHLCV + 10 档盘口 40 列 + 加权/总量 4 列 + 逐笔成交聚合 8 列 +
逐笔委托聚合 4 列），float32，zstd 压缩。

用法（宿主机仓库根，读 NAS 原始仓库）：
  .venv/bin/python -m backend.scripts.minute_model.build_min1_l2 --probe 20240401
  .venv/bin/python -m backend.scripts.minute_model.build_min1_l2 --all --workers 1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

import duckdb

from backend.scripts.minute_model.build_features import available_days

log = logging.getLogger("minute_model.min1")

LARGE_AMT = 1.0e6  # 大单：单笔成交额 >= 100 万元
XLARGE_AMT = 5.0e6  # 超大单：>= 500 万元
BIG_ORDER_AMT = 5.0e5  # 大额委托：价格 × 数量 >= 50 万元

# 分钟 -> bar 标签：连续竞价按「分钟收盘标记」右移 1 分钟，11:30/15:00 归并收尾。
# 注意 HHMM 不是十进制：10:59 的下一分钟是 11:00，13:59 的下一分钟是 14:00。
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

SESSION_FILTER = (
    "((minute BETWEEN 915 AND 925) OR (minute BETWEEN 930 AND 1130) "
    "OR (minute BETWEEN 1300 AND 1500))"
)
TRADE_SESSION_FILTER = (
    "((minute = 925) OR (minute BETWEEN 930 AND 1130) "
    "OR (minute BETWEEN 1300 AND 1500))"
)
STOCK_FILTER = (
    "prod = 'stock' AND market IN ('SH', 'SZ') "
    "AND code NOT LIKE '900%' AND code NOT LIKE '200%'"
)


def _quantdb_root() -> Path:
    """优先 QM_QUANTDB_DATA_DIR，其次容器路径 /data/quantdb，最后仓库 data/quantdb。"""
    env = os.environ.get("QM_QUANTDB_DATA_DIR")
    if env:
        return Path(env)
    if os.getenv("SERVICE_MODE") and Path("/data/quantdb").is_dir():
        return Path("/data/quantdb")
    repo = Path(__file__).resolve().parents[3] / "data" / "quantdb"
    return repo


OUT = _quantdb_root() / "1_kline_data" / "l2_min1"


def _price_filter(col: str) -> str:
    return f"isfinite({col}) AND {col} > 0"


def _snap_aggs() -> list[str]:
    aggs = [
        f'arg_min(px, time_ms) FILTER (WHERE {_price_filter("px")}) AS open',
        f'max(px) FILTER (WHERE {_price_filter("px")}) AS high',
        f'min(px) FILTER (WHERE {_price_filter("px")}) AS low',
        f'arg_max(px, time_ms) FILTER (WHERE {_price_filter("px")}) AS close',
        'arg_max(cum_volume, time_ms) AS cum_volume',
        'arg_max(cum_amount, time_ms) AS cum_amount',
    ]
    for i in range(1, 11):
        aggs.append(
            f'arg_max("申买价{i}", time_ms) FILTER (WHERE {_price_filter(f"申买价{i}")}) AS bid{i}'
        )
        aggs.append(
            f'arg_max("申卖价{i}", time_ms) FILTER (WHERE {_price_filter(f"申卖价{i}")}) AS ask{i}'
        )
        aggs.append(f'arg_max("申买量{i}", time_ms) AS bidv{i}')
        aggs.append(f'arg_max("申卖量{i}", time_ms) AS askv{i}')
    return aggs


def build_sql(root: Path, date: str) -> str:
    """单日聚合 SQL（返回输出列表达式，供 probe / COPY 复用）。"""
    snap = f"{root}/snapshot/date={date}/*.parquet"
    trades = f"{root}/trades/date={date}/*.parquet"
    orders = f"{root}/orders/date={date}/*.parquet"

    level_cols = ",\n           ".join(
        [f'"申买价{i}"' for i in range(1, 11)]
        + [f'"申卖价{i}"' for i in range(1, 11)]
        + [f'"申买量{i}"' for i in range(1, 11)]
        + [f'"申卖量{i}"' for i in range(1, 11)]
    )
    snap_aggs = ",\n           ".join(_snap_aggs())

    wavg_bid_num = " + ".join(
        f'coalesce(bid{i}, 0) * coalesce(bidv{i}, 0)' for i in range(1, 11)
    )
    wavg_ask_num = " + ".join(
        f'coalesce(ask{i}, 0) * coalesce(askv{i}, 0)' for i in range(1, 11)
    )
    bidv_sum = " + ".join(f"coalesce(bidv{i}, 0)" for i in range(1, 11))
    askv_sum = " + ".join(f"coalesce(askv{i}, 0)" for i in range(1, 11))

    return f"""
WITH s AS (
    SELECT wd_code, time_ms, bar,
           "成交价" / 1e4 AS px,
           "当日累计成交量" AS cum_volume,
           "当日成交额" AS cum_amount,
           {level_cols}
    FROM (
        SELECT *, {BAR_EXPR} AS bar
        FROM read_parquet('{snap}')
        WHERE {STOCK_FILTER} AND {SESSION_FILTER}
    )
    WHERE bar IS NOT NULL
), b AS (
    SELECT wd_code, bar,
           {snap_aggs}
    FROM s
    GROUP BY 1, 2
), bars AS (
    SELECT wd_code, bar,
           open, high, low, close,
           greatest(cum_volume - coalesce(
               lag(cum_volume) OVER (PARTITION BY wd_code ORDER BY bar), 0), 0) AS volume,
           greatest(cum_amount - coalesce(
               lag(cum_amount) OVER (PARTITION BY wd_code ORDER BY bar), 0), 0) / 1e4 AS amount,
           {", ".join(f"bid{i}, ask{i}, bidv{i}, askv{i}" for i in range(1, 11))},
           ({wavg_bid_num}) / nullif({bidv_sum}, 0) AS wavg_bid,
           ({wavg_ask_num}) / nullif({askv_sum}, 0) AS wavg_ask,
           ({bidv_sum}) AS total_bidv,
           ({askv_sum}) AS total_askv
    FROM b
), tr AS (
    SELECT wd_code, bar,
           sum(qty) FILTER (WHERE dir = 'B') AS buy_volume,
           sum(qty) FILTER (WHERE dir = 'S') AS sell_volume,
           sum(amount_yuan) FILTER (WHERE dir = 'B') / 1e4 AS buy_amount,
           sum(amount_yuan) FILTER (WHERE dir = 'S') / 1e4 AS sell_amount,
           sum(amount_yuan) FILTER (WHERE dir = 'B' AND amount_yuan >= {LARGE_AMT}) / 1e4
               AS large_buy_amount,
           sum(amount_yuan) FILTER (WHERE dir = 'S' AND amount_yuan >= {LARGE_AMT}) / 1e4
               AS large_sell_amount,
           sum(amount_yuan) FILTER (WHERE dir = 'B' AND amount_yuan >= {XLARGE_AMT}) / 1e4
               AS xlarge_buy_amount,
           sum(amount_yuan) FILTER (WHERE dir = 'S' AND amount_yuan >= {XLARGE_AMT}) / 1e4
               AS xlarge_sell_amount,
           count(*) AS trade_n
    FROM (
        SELECT wd_code, {BAR_EXPR} AS bar, qty, amount_yuan, dir
        FROM read_parquet('{trades}')
        WHERE {STOCK_FILTER} AND {TRADE_SESSION_FILTER}
          AND is_valid AND dir IN ('B', 'S') AND qty > 0
    )
    WHERE bar IS NOT NULL
    GROUP BY 1, 2
), od AS (
    SELECT wd_code, bar,
           count(*) AS order_n,
           count(*) FILTER (WHERE is_cancel OR otype = '1') AS cancel_n,
           count(*) FILTER (WHERE price_yuan * qty >= {BIG_ORDER_AMT}) AS big_order_n
    FROM (
        SELECT wd_code, {BAR_EXPR} AS bar, price_yuan, qty, is_cancel, otype
        FROM read_parquet('{orders}')
        WHERE {STOCK_FILTER} AND {SESSION_FILTER}
    )
    WHERE bar IS NOT NULL
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


FINAL_COLS = [
    "symbol", "time", "open", "high", "low", "close", "volume", "amount",
] + [f"{side}{i}" for side in ("bid", "ask") for i in range(1, 11)] + [
    f"{side}v{i}" for side in ("bid", "ask") for i in range(1, 11)
] + [
    "wavg_bid", "wavg_ask", "total_bidv", "total_askv",
    "buy_volume", "sell_volume", "buy_amount", "sell_amount",
    "large_buy_amount", "large_sell_amount",
    "xlarge_buy_amount", "xlarge_sell_amount",
    "trade_n", "order_n", "cancel_n", "big_order_n",
]


def final_sql(root: Path, date: str) -> str:
    base = build_sql(root, date)
    casts = ",\n       ".join(
        f"CAST({c} AS FLOAT) AS {c}" for c in FINAL_COLS[2:]
    )
    return f"""
SELECT wd_code AS symbol,
       CAST(strptime('{date} ' || lpad(CAST(bar AS VARCHAR), 4, '0'), '%Y%m%d %H%M')
            AS TIMESTAMP) AS time,
       {casts}
FROM (
{base}
) AS j
ORDER BY wd_code, bar
"""


def _connect(memory_limit: str, threads: int, tmp_dir: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET threads={threads}")
    con.execute(f"SET temp_directory='{tmp_dir}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET enable_progress_bar=false")
    return con


def _mem_available_gb() -> float:
    """读取 /proc/meminfo 的 MemAvailable（GB）。"""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 999.0


def _wait_for_memory(min_gb: float, tag: str, max_wait_s: int = 1800) -> None:
    """开工前等内存：低于阈值就睡，最多等 max_wait_s 秒。

    2026-11-07 全局 OOM 的教训：构建进程的内存必须由自己看住，不能指望 OOM killer
    在打穿主机之前挑对人。
    """
    started = time.time()
    while True:
        avail = _mem_available_gb()
        if avail >= min_gb:
            return
        if time.time() - started > max_wait_s:
            log.warning("%s: 内存 %.1fGB < %.1fGB，等待超时，继续执行", tag, avail, min_gb)
            return
        log.info("%s: 内存 %.1fGB < %.1fGB，等待 60s", tag, avail, min_gb)
        time.sleep(60)


def build_day(
    date: str,
    root: Path,
    out_path: Path,
    memory_limit: str,
    threads: int,
    min_free_gb: float = 0.0,
) -> dict:
    started = time.time()
    if min_free_gb > 0:
        _wait_for_memory(min_free_gb, date)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_path.parent / ".duckdb_tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = out_path.parent / ".data.parquet.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    con = _connect(memory_limit, threads, tmp_dir)
    try:
        con.execute(
            f"COPY ({final_sql(root, date)}) TO '{tmp_path}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 131072)"
        )
        stats = con.execute(
            f"SELECT count(*) AS rows, count(DISTINCT symbol) AS symbols, "
            f"count(DISTINCT time) AS bars, "
            f"avg(CASE WHEN volume IS NULL THEN 0 ELSE 1 END) AS vol_cov "
            f"FROM read_parquet('{tmp_path}')"
        ).fetchone()
    finally:
        con.close()

    rows, symbols, bars, vol_cov = stats
    tmp_path.replace(out_path)
    meta = {
        "date": date,
        "source": str(root),
        "rows": int(rows),
        "symbols": int(symbols),
        "bars": int(bars),
        "volume_coverage": round(float(vol_cov or 0.0), 4),
        "size_mb": round(out_path.stat().st_size / 1e6, 1),
        "elapsed_s": round(time.time() - started, 1),
    }
    (out_path.parent / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    try:
        tmp_dir.rmdir()
    except OSError:
        pass
    return meta


def _worker(payload: tuple[str, str, str, str, int, float]) -> tuple[str, str]:
    date, root, out, memory_limit, threads, min_free_gb = payload
    path = Path(out) / f"dt={date}" / "data.parquet"
    if path.exists():
        return date, "skip"
    try:
        meta = build_day(
            date, Path(root), path, memory_limit, threads, min_free_gb
        )
    except Exception as exc:  # pragma: no cover
        log.exception("%s 构建失败", date)
        return date, f"fail:{exc}"
    log.info(
        "%s: %d 行 × %d 只 × %d 分钟，%.0f MB，%.0fs",
        date, meta["rows"], meta["symbols"], meta["bars"], meta["size_mb"], meta["elapsed_s"],
    )
    return date, "ok"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", default="", help="单日探针，产物写到 _probe/")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--memory-limit", default="6GB", help="每进程 DuckDB 内存上限")
    ap.add_argument("--threads", type=int, default=8, help="每进程 DuckDB 线程数")
    ap.add_argument("--min-free-gb", type=float, default=14.0,
                    help="开工前要求的最小可用内存（GB），不足则等待")
    args = ap.parse_args()

    days = available_days()
    if not days:
        raise SystemExit("未找到任何 L2 parquet 仓库日期，检查 NAS 挂载")
    print(f"可用交易日 {len(days)} 天：{min(days)} ~ {max(days)}", flush=True)

    if args.probe:
        date = args.probe
        if date not in days:
            raise SystemExit(f"{date} 不在可用日期内")
        out_dir = OUT / "_probe"
        (out_dir / ".duckdb_tmp").mkdir(parents=True, exist_ok=True)
        meta = build_day(date, days[date], out_dir / f"dt={date}" / "data.parquet",
                         args.memory_limit, args.threads, args.min_free_gb)
        print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
        con = _connect(args.memory_limit, args.threads, out_dir / ".duckdb_tmp")
        df = con.sql(
            f"SELECT * FROM read_parquet('{out_dir}/dt={date}/data.parquet') LIMIT 5"
        ).df()
        print(df.to_string(), flush=True)
        nan = con.sql(
            f"""SELECT count(*) AS n,
                       avg(CASE WHEN close IS NULL THEN 1 ELSE 0 END) AS close_null,
                       avg(CASE WHEN bid1 IS NULL THEN 1 ELSE 0 END) AS bid1_null,
                       avg(CASE WHEN trade_n IS NULL THEN 1 ELSE 0 END) AS trade_null
                FROM read_parquet('{out_dir}/dt={date}/data.parquet')"""
        ).df()
        print(nan.to_string(), flush=True)
        con.close()
        return

    if not args.all:
        return

    targets = [
        d for d in sorted(days)
        if (not args.start or d >= args.start) and (not args.end or d <= args.end)
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    payloads = [
        (d, str(days[d]), str(OUT), args.memory_limit, args.threads, args.min_free_gb)
        for d in targets
    ]

    started = time.time()
    done = 0
    if args.workers > 1:
        from multiprocessing import Pool

        with Pool(args.workers) as pool:
            for date, status in pool.imap_unordered(_worker, payloads, chunksize=1):
                done += 1
                print(f"[{done}/{len(targets)}] {date} {status} "
                      f"（累计 {(time.time() - started) / 60:.1f} 分钟）", flush=True)
    else:
        for payload in payloads:
            date, status = _worker(payload)
            done += 1
            print(f"[{done}/{len(targets)}] {date} {status} "
                  f"（累计 {(time.time() - started) / 60:.1f} 分钟）", flush=True)
    print(f"完成 {done}/{len(targets)}，用时 {(time.time() - started) / 60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    main()
