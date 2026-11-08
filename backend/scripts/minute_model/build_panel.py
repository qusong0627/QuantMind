"""构建盘中多时点训练面板：分钟特征 + 前一日日频上下文 + 4h/5h 标签 + 可交易性。

## 口径

- 样本：`(date, symbol, cutoff)`，cutoff ∈ {10:30, 11:30, 13:30, 14:30, 14:50}
- 特征：`data/minute_model/daily/date=D/data.parquet`（只用 ≤cutoff 的分钟 bar）
- 上下文：`6_ml_datasets/l1_l2_factors/dt=PREV`（**前一交易日**，PIT，含当日即前视）
- 标签：
  - `ret_4h` = 下一交易日**同一 cutoff** 的价格 / 当前 px - 1（240 个交易分钟，含隔夜）
  - `ret_5h` = 下一交易日同一 cutoff +60 交易分钟（10:30→11:30、11:30→13:30、13:30→14:30；
    14:30/14:50 只能到次日收盘，记 `ret_5h_capped=True`）
  - `rank_4h` = 同日同 cutoff 的截面分位（0-1）
- 可交易性：
  - `is_limit_up`：px/昨收 - 1 ≥ 涨停阈值 - 0.3%，且买一为空/量为 0（封板买不进）
  - `is_suspended`：分钟数 < 60 或全天成交量 0
  - `is_new`：60 个交易日前无日线（次新，前 60 日剔除）
  - `tradable = not (limit_up or suspended or new) and px not null`

输出：`data/minute_model/panel/date=YYYYMMDD/data.parquet`（约 2.5 万行/天）

用法：
  .venv/bin/python -m backend.scripts.minute_model.build_panel --probe 20240402
  .venv/bin/python -m backend.scripts.minute_model.build_panel --all --start 20240101 --end 20261231
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import duckdb

from backend.scripts.minute_model.build_intraday_features import CUTOFFS, OUT as FEAT_DIR

log = logging.getLogger("minute_model.panel")
QUANTDB = FEAT_DIR.parents[1] / "quantdb"
FACTORS = QUANTDB / "6_ml_datasets" / "l1_l2_factors"
DAILY = QUANTDB / "1_kline_data" / "daily_unadjusted"
OUT = FEAT_DIR.parent / "panel"

# 日频上下文（前一交易日，PIT）
CONTEXT_COLS = [
    "turn_1", "turn_5", "turn_20", "turn_z_20", "turn_ratio_1_5", "turn_std_20",
    "mom_ret_1d", "mom_ret_3d", "mom_ret_5d", "mom_ret_10d", "mom_ret_20d", "mom_ret_60d",
    "mom_ma_gap_5", "mom_ma_gap_20", "mom_rsi_6", "mom_macd_hist",
    "vol_std_5", "vol_std_20", "vol_atr_14", "vol_amp_1", "vol_amp_20",
    "amt_log", "amt_z_20", "amt_ratio_1_5", "amt_ratio_1_20", "amt_net_flow_5",
    "flow_net_ratio", "flow_super_net", "flow_large_net", "flow_large_ratio",
    "micro_liquidity_amihud_5", "micro_liquidity_turnover_decay", "micro_liquidity_concentration",
    "micro_vpin_100", "micro_vpin_kyle", "micro_trade_sign_autocorr",
    "fun_float_mv", "fun_mv_rank", "fun_pe", "fun_pb", "fun_bp",
    "ind_strength_20", "ind_dispersion_20", "ind_crowding_20", "ind_rotation_speed_20",
    "concept_hot_score", "concept_momentum_top3", "concept_rotation_score", "concept_crowding_max",
    "chip_profit_ratio_20", "chip_concentration_20",
    "style_beta_20", "style_idio_vol_20",
    "tech_bb_pos", "tech_close_to_high_20", "tech_max_drawdown_20",
    "mfi_14", "obv_slope_20",
]

# cutoff -> 5h 终点（"next cutoff" = 次日同 cutoff；"close" = 次日收盘）
FIVE_H_MAP = {"10:30": "11:30", "11:30": "13:30", "13:30": "14:30", "14:30": "close", "14:50": "close"}


def calendar() -> list[str]:
    days = []
    for p in DAILY.glob("dt=*"):
        n = p.name.split("=", 1)
        if len(n) == 2 and n[1].isdigit():
            days.append(n[1])
    return sorted(days)


def _next_prev(days: list[str]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    prv = {d: days[i - 1] for i, d in enumerate(days) if i > 0}
    # 60 个交易日前（用于次新过滤）
    d60 = {d: days[i - 60] for i, d in enumerate(days) if i >= 60}
    return nxt, prv, d60


def build_sql(date: str, next_date: str, prev_date: str, d60: str | None) -> str:
    feat = FEAT_DIR / f"date={date}" / "data.parquet"
    feat_next = FEAT_DIR / f"date={next_date}" / "data.parquet"
    factors = FACTORS / f"dt={prev_date}" / "data.parquet"
    daily = DAILY / f"dt={date}" / "data.parquet"
    daily_next = DAILY / f"dt={next_date}" / "data.parquet"
    ctx = ",\n           ".join(f"d.{c}" for c in CONTEXT_COLS)
    # 5h 终点：10:30→11:30、11:30→13:30、13:30→14:30 用次日特征；14:30/14:50 用次日收盘
    n2_join = f"""
        LEFT JOIN read_parquet('{feat_next}') n2
               ON n2.symbol = f.symbol
              AND n2.cutoff = CASE f.cutoff
                    WHEN '10:30' THEN '11:30' WHEN '11:30' THEN '13:30'
                    WHEN '13:30' THEN '14:30' ELSE '14:50' END
    """
    d60_join = (
        f"LEFT JOIN (SELECT DISTINCT symbol FROM read_parquet('{DAILY}/dt={d60}/data.parquet')) old "
        f"ON old.symbol = f.symbol"
        if d60
        else "LEFT JOIN (SELECT NULL AS symbol) old ON old.symbol = f.symbol"
    )
    return f"""
WITH f AS (
    SELECT * FROM read_parquet('{feat}')
), n AS (
    SELECT * FROM read_parquet('{feat_next}')
), base AS (
    SELECT
        f.symbol, f.date, f.cutoff,
        {", ".join("f." + c for c in _feature_cols())},
        {ctx},
        d.close AS prev_close,
        dn.close AS next_close,
        n.px AS px_next_same,
        n2.px AS px_next_5h,
        old.symbol IS NOT NULL AS has_60d_history,
        (f.px / nullif(d.close, 0) - 1.0) AS ret_today_sofar
    FROM f
    LEFT JOIN read_parquet('{factors}') d ON d.symbol = f.symbol
    LEFT JOIN read_parquet('{daily_next}') dn ON dn.symbol = f.symbol
    LEFT JOIN read_parquet('{feat_next}') n ON n.symbol = f.symbol AND n.cutoff = f.cutoff
    {n2_join}
    {d60_join}
), labels AS (
    SELECT *,
        px_next_same / nullif(px, 0) - 1.0 AS ret_4h,
        CASE
            WHEN cutoff IN ('14:30', '14:50') THEN next_close / nullif(px, 0) - 1.0
            ELSE px_next_5h / nullif(px, 0) - 1.0
        END AS ret_5h,
        (cutoff IN ('14:30', '14:50')) AS ret_5h_capped,
        CASE
            WHEN px IS NULL OR prev_close IS NULL THEN FALSE
            WHEN px / prev_close - 1.0 >=
                 (CASE WHEN symbol LIKE '30%' OR symbol LIKE '68%' THEN 0.195 ELSE 0.095 END) - 0.003
                 -- 涨停封板：涨幅到阈值且卖一无货（买盘排队买不进）
                 AND (ask1 IS NULL OR ask_depth_1 = 0) THEN TRUE
            ELSE FALSE
        END AS is_limit_up,
        (n_bars < 60 OR cum_volume = 0) AS is_suspended,
        (has_60d_history IS DISTINCT FROM TRUE) AS is_new
    FROM base
)
SELECT *,
    ret_4h IS NOT NULL AND ret_4h BETWEEN -0.35 AND 0.35 AS label_ok,
    (NOT is_limit_up AND NOT is_suspended AND NOT is_new AND px IS NOT NULL) AS tradable,
    percent_rank() OVER (PARTITION BY date, cutoff ORDER BY ret_4h) AS rank_4h
FROM labels
"""


def _feature_cols() -> list[str]:
    """分钟特征列（从特征表推断，排除键与日期）。"""
    return FEATURE_COLS_CACHE


def _detect_feature_cols(sample: Path) -> list[str]:
    import pyarrow.parquet as pq

    names = pq.ParquetFile(sample).schema_arrow.names
    return [c for c in names if c not in ("symbol", "date", "cutoff")]


FEATURE_COLS_CACHE: list[str] = []


def build_day(date: str, next_date: str, prev_date: str, d60: str | None) -> dict:
    started = time.time()
    out_path = OUT / f"date={date}" / "data.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / ".data.parquet.tmp"
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=8")
    con.execute("SET preserve_insertion_order=false")
    try:
        con.execute(
            f"COPY ({build_sql(date, next_date, prev_date, d60)}) TO '{tmp}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 131072)"
        )
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{tmp}')").fetchone()[0]
    finally:
        con.close()
    tmp.replace(out_path)
    return {
        "date": date,
        "rows": int(rows),
        "size_mb": round(out_path.stat().st_size / 1e6, 1),
        "elapsed_s": round(time.time() - started, 1),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    args = ap.parse_args()

    days = calendar()
    nxt, prv, d60 = _next_prev(days)
    feat_days = sorted(
        p.name.split("=", 1)[1] for p in FEAT_DIR.glob("date=*")
        if (p / "data.parquet").exists()
    )
    global FEATURE_COLS_CACHE
    sample = FEAT_DIR / f"date={feat_days[0]}" / "data.parquet"
    FEATURE_COLS_CACHE = _detect_feature_cols(sample)
    log.info("特征日 %d 天，特征列 %d 个，上下文 %d 个",
             len(feat_days), len(FEATURE_COLS_CACHE), len(CONTEXT_COLS))

    if args.probe:
        date = args.probe
        if date not in feat_days:
            raise SystemExit(f"{date} 没有特征文件")
        meta = build_day(date, nxt[date], prv[date], d60.get(date))
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        con = duckdb.connect()
        p = OUT / f"date={date}" / "data.parquet"
        print(con.sql(f"SELECT cutoff, count(*) n, avg(ret_4h) ret, avg(tradable::INT) tr, "
                      f"avg(is_limit_up::INT) lu, avg(is_new::INT) newly FROM read_parquet('{p}') "
                      f"GROUP BY 1 ORDER BY 1").df().to_string())
        con.close()
        return

    if not args.all:
        return
    targets = [d for d in feat_days if d in nxt
               and (not args.start or d >= args.start) and (not args.end or d <= args.end)]
    started = time.time()
    done = 0
    for d in targets:
        out = OUT / f"date={d}" / "data.parquet"
        if out.exists():
            done += 1
            continue
        try:
            meta = build_day(d, nxt[d], prv[d], d60.get(d))
            done += 1
            log.info("%s: %d 行，%.0fs", d, meta["rows"], meta["elapsed_s"])
        except Exception as exc:  # pragma: no cover
            log.exception("%s 失败", d)
        if done % 20 == 0:
            print(f"[{done}/{len(targets)}] {(time.time() - started) / 60:.1f} 分钟", flush=True)
    print(f"完成 {done}/{len(targets)}", flush=True)


if __name__ == "__main__":
    main()
