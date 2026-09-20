#!/usr/bin/env python3
"""从 QuantDB parquet 直接生成特征快照（按年份拆分）。

替代 update_feature_parquet.py 的 PG→计算模式，
直接读取 QuantDB 已有的 daily_forward + features_daily + l1_factors + l2_factors 数据，
JOIN 后按年份写入 model_features_YYYY.parquet + metadata.json。

用法:
    python generate_feature_snapshots.py                    # 生成所有年份
    python generate_feature_snapshots.py --year 2026        # 只生成指定年份
    python generate_feature_snapshots.py --dry-run          # 仅检查不写入
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import numpy as np

# 容器内 vs 主机
if os.path.exists("/app") and not os.environ.get("QUANTMIND_HOST_MODE"):
    QDB_DIR = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
    SNAPSHOT_DIR = Path("/app/db/feature_snapshots")
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    QDB_DIR = Path(os.environ.get("QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")))
    SNAPSHOT_DIR = PROJECT_ROOT / "db" / "feature_snapshots"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _normalize_symbol(series: "pd.Series") -> "pd.Series":
    """统一股票代码为 6 位数字：000001.SZ / SZ000001 / 1 → 000001。"""
    s = series.astype(str).str.strip().str.upper()
    s = s.str.split(".").str[0]
    s = s.str.replace(r"^(SH|SZ|BJ)", "", regex=True)
    return s.str.zfill(6)


# features_daily.return_Nd 是【未来 N 日收益】(return_1d[T] == pct_change[T+1])，
# 直接当特征会造成标签泄漏，故一律丢弃，改用 l1_factors 的 mom_ret_Nd（过去收益）。
_LEAKY_RETURN_COLS: tuple[str, ...] = (
    "return_1d", "return_3d", "return_5d", "return_10d", "return_20d", "return_60d",
)


def _add_gtja_factors(df: pd.DataFrame) -> pd.DataFrame:
    """加入去重后的 GTJA Alpha191 高价值因子。

    基于 IC 分析与现有特征去重, 保留 13 个独立且有预测力的因子(多空双方向):
      - gtja_158: (high-low)/close 振幅
      - gtja_070/095: STD(amount, 6/20) 成交额波动
      - gtja_042: -RANK(STD(high,10)) * CORR(high,vol,10)
      - gtja_083/016/099/090/032/062: 量价相关性/协方差的截面排名 (做空信号)
      - gtja_150: (close+high+low)/3 * volume 典型价格×量
      - gtja_176: CORR(rank(RSV12), rank(vol), 6)
      - gtja_036: RANK(SUM(CORR(rank(vol),rank(vwap),6),2))
    需按股票分组时序计算, df 需含 symbol/trade_date/open/high/low/close/volume/amount。
    """
    need_cols = {"symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"}
    if not need_cols.issubset(df.columns):
        _log("  警告: 缺 OHLCV 列, 跳过 GTJA 因子")
        return df

    g = df.sort_values(["symbol", "trade_date"]).copy()
    sym = g["symbol"]
    c = g["close"]
    h = g["high"]
    lo = g["low"]
    v = g["volume"]
    amt = g["amount"].fillna((h + lo + c) / 3 * v)
    vwap = (amt / v.replace(0, np.nan)).fillna((h + lo + c) / 3)
    td = g["trade_date"]

    def cross_rank(s):
        return s.groupby(td).rank(pct=True)

    def roll_corr(a, b, w):
        return a.groupby(sym).transform(
            lambda x: x.rolling(w, min_periods=max(3, int(w * 0.5))).corr(b.loc[x.index])
        )

    def roll_cov(a, b, w):
        return a.groupby(sym).transform(
            lambda x: x.rolling(w, min_periods=max(3, int(w * 0.5))).cov(b.loc[x.index])
        )

    def roll_std(s, w):
        return s.groupby(sym).transform(
            lambda x: x.rolling(w, min_periods=max(3, int(w * 0.5))).std()
        )

    def roll_sum(s, w):
        return s.groupby(sym).transform(
            lambda x: x.rolling(w, min_periods=max(1, int(w * 0.5))).sum()
        )

    def roll_max(s, w):
        return s.groupby(sym).transform(
            lambda x: x.rolling(w, min_periods=max(3, int(w * 0.5))).max()
        )

    def roll_min(s, w):
        return s.groupby(sym).transform(
            lambda x: x.rolling(w, min_periods=max(3, int(w * 0.5))).min()
        )

    def roll_mean(s, w):
        return s.groupby(sym).transform(
            lambda x: x.rolling(w, min_periods=max(3, int(w * 0.5))).mean()
        )

    # 截面 rank 序列
    r_vol = cross_rank(v)
    r_high = cross_rank(h)
    r_close = cross_rank(c)
    r_vwap = cross_rank(vwap)
    r_low = cross_rank(lo)

    out = {}

    # gtja_016: -TSMAX(RANK(CORR(rank(vol),rank(vwap),5)),5)
    corr_rv = roll_corr(r_vol, r_vwap, 5)
    out["gtja_016"] = -roll_max(corr_rv.rank(pct=True), 5)

    # gtja_032: -SUM(RANK(CORR(rank(high),rank(vol),3)),3)
    corr_hv = roll_corr(r_high, r_vol, 3)
    out["gtja_032"] = -roll_sum(corr_hv.rank(pct=True), 3)

    # gtja_062: -CORR(high, rank(vol), 5)
    out["gtja_062"] = -roll_corr(h, r_vol, 5)

    # gtja_083: -RANK(COV(rank(high), rank(vol), 5))
    out["gtja_083"] = -roll_cov(r_high, r_vol, 5).rank(pct=True)

    # gtja_090: -RANK(CORR(rank(vwap), rank(vol), 5))
    out["gtja_090"] = -roll_corr(r_vwap, r_vol, 5).rank(pct=True)

    # gtja_099: -RANK(COV(rank(close), rank(vol), 5))
    out["gtja_099"] = -roll_cov(r_close, r_vol, 5).rank(pct=True)

    # gtja_036: RANK(SUM(CORR(rank(vol),rank(vwap),6),2))
    corr_vw6 = roll_corr(r_vol, r_vwap, 6)
    out["gtja_036"] = roll_sum(corr_vw6, 2).rank(pct=True)

    # gtja_070: STD(AMOUNT, 6)
    out["gtja_070"] = roll_std(amt, 6)

    # gtja_095: STD(AMOUNT, 20)
    out["gtja_095"] = roll_std(amt, 20)

    # gtja_150: (close+high+low)/3 * volume
    out["gtja_150"] = (c + h + lo) / 3 * v

    # gtja_176: CORR(rank(KDJ_RSV_12), rank(vol), 6)
    hh12 = roll_max(h, 12)
    ll12 = roll_min(lo, 12)
    rsv12 = (c - ll12) / (hh12 - ll12).replace(0, np.nan)
    out["gtja_176"] = roll_corr(rsv12.rank(pct=True), r_vol, 6)

    # gtja_042: -RANK(STD(high,10)) * CORR(high,vol,10)
    out["gtja_042"] = -roll_std(h, 10).rank(pct=True) * roll_corr(h, v, 10)

    # gtja_158: (high-low)/close
    out["gtja_158"] = (h - lo) / c.clip(lower=1e-8)

    for k, val in out.items():
        g[k] = val

    _log(f"  GTJA Alpha191 新增因子: {len(out)} 个")
    return g

def _build_snapshot(year: int, dry_run: bool = False) -> dict | None:
    """从 QuantDB parquet 生成指定年份的特征快照。"""
    import duckdb

    con = duckdb.connect()

    qdb = str(QDB_DIR)
    year_start = int(f"{year}0101")
    year_end = int(f"{year}1231")

    # ── Step 1: daily_forward (OHLCV) + features_daily (基础指标) ──
    # OHLCV 用后复权 (daily_backward)：l1_factors 的 mom_ret_Nd 也是后复权口径，
    # 混用前复权会让价格序列与因子/标签错配（实测相关性仅 0.08）。
    _log(f"读取 QuantDB daily_backward + features_daily ({year})...")
    query_base = f"""
    SELECT
        k.symbol,
        k.time AS trade_date,
        k.open, k.high, k.low, k.close, k.volume, k.amount,
        f.ma5, f.ma10, f.ma20, f.ma60,
        f.ma_gap_5, f.ma_gap_10, f.ma_gap_20,
        f.rsi_6, f.rsi_14,
        f.kdj_k, f.kdj_d, f.kdj_j,
        f.macd_dif, f.macd_dea, f.macd_hist,
        f.vol_std_5, f.vol_std_20, f.vol_std_60, f.vol_atr_14,
        f.vol_to_ma5, f.vol_to_ma20,
        f.volume_ma_3, f.amount_ma_5, f.volume_trend_3d,
        f.pct_change, f.beta_20,
        f.total_mv, f.float_mv, f.pe_ttm, f.pe_static, f.pb, f.ps_ttm,
        -- features_daily.dividend_rate 全程为「每10股派息/不复权close」小数口径
        -- （20260814 实测 601138: 0.98/66.19=0.014806），而 valuation 同日起切换为
        -- 「每10股派息/10/close×100」百分数口径（0.148）。
        -- 统一 ×10 归一为百分数口径，与 valuation 对齐
        f.dividend_rate * 10 AS dividend_rate,
        f.total_capital, f.circulating_capital, f.net_profit_ttm, f.revenue_ttm, f.equity,
        f.annual_net_profit
    -- ⚠ features_daily 自 20260914 起由 50 列**永久**扩为 78 列（新增 is_st /
    -- industry_name / in_hs300 / list_date / pb_mrq 等 30 列），本 glob 跨该 schema 边界。
    -- 这里**故意不加** union_by_name：
    --   不加 → DuckDB 取首个文件的 schema，本查询枚举的老列全都在，结果正确；
    --           若日后有人往 SELECT 里加 f.is_st，会直接 Binder 报错（响亮的失败）。
    --   加了 → 那 30 列在老分区**全 NULL**、新分区全非 NULL，正好是个「年代指示器」，
    --           一旦被选入就是静默泄露（树模型必拿它做一刀切）。
    -- 所以「补上 union_by_name」不是修复。要新列请显式点名，并先想清楚 NULL 段怎么处理。
    -- 回归测试见 backend/tests/test_factor_partition.py::test_duckdb_glob_with*。
    FROM read_parquet('{qdb}/1_kline_data/daily_backward/**/*.parquet', hive_partitioning=true) k
    JOIN read_parquet('{qdb}/6_ml_datasets/features_daily/**/*.parquet', hive_partitioning=true) f
      ON k.symbol = f.symbol AND k.dt = f.dt
    WHERE k.dt BETWEEN {year_start} AND {year_end}
    """

    df = con.execute(query_base).fetchdf()
    _log(f"  base JOIN: {len(df):,} rows")

    if df.empty:
        _log(f"  {year} 年无基础数据，跳过")
        con.close()
        return None

    # 所有数据源的主键都是带后缀格式 (000001.SZ)，统一归一化为 6 位代码，
    # 保证后续 l1/l2 merge 的 JOIN 键格式一致
    df["symbol"] = _normalize_symbol(df["symbol"])

    # ── Step 2: l1_factors (动量/波动率/流动性/风格/行业/筹码/概念) ──
    # l1 目录混合两种格式，两者都要读并合并：
    #   旧格式 l1_factors_YYYYMMDD.parquet — 主键 wind_code (600036.SH)，日期在文件名
    #   新格式 dt=YYYYMMDD/data.parquet     — 主键 symbol，日期在 hive 分区
    _log(f"读取 QuantDB l1_factors ({year})...")
    l1_path = f"{qdb}/6_ml_datasets/l1_factors"
    l1_parts = []

    try:
        query_l1_old = f"""
        SELECT
            regexp_extract(filename, 'l1_factors_(\\d{{8}})\\.parquet', 1) AS _dt,
            * EXCLUDE (filename)
        FROM read_parquet('{l1_path}/l1_factors_{year}*.parquet', filename=true)
        """
        part = con.execute(query_l1_old).fetchdf()
        if not part.empty:
            _log(f"  l1 旧格式: {len(part):,} rows")
            l1_parts.append(part)
    except Exception as exc:
        _log(f"  l1 旧格式读取跳过: {exc}")

    try:
        query_l1_hive = f"""
        SELECT CAST(dt AS VARCHAR) AS _dt, * EXCLUDE (dt)
        FROM read_parquet('{l1_path}/dt=*/data.parquet', hive_partitioning=true)
        WHERE dt BETWEEN {year_start} AND {year_end}
        """
        part = con.execute(query_l1_hive).fetchdf()
        if not part.empty:
            _log(f"  l1 hive 格式: {len(part):,} rows")
            l1_parts.append(part)
    except Exception as exc:
        _log(f"  l1 hive 格式读取跳过: {exc}")

    df_l1 = None
    if l1_parts:
        df_l1 = pd.concat(l1_parts, axis=0, ignore_index=True)

        # 统一主键：wind_code (600036.SH) / symbol → 6 位代码
        if "symbol" not in df_l1.columns and "wind_code" in df_l1.columns:
            df_l1["symbol"] = df_l1["wind_code"]
        elif "wind_code" in df_l1.columns:
            df_l1["symbol"] = df_l1["symbol"].fillna(df_l1["wind_code"])
        df_l1["symbol"] = _normalize_symbol(df_l1["symbol"])

        df_l1["trade_date"] = pd.to_datetime(df_l1["_dt"], format="%Y%m%d", errors="coerce")
        df_l1 = df_l1[df_l1["trade_date"].notna()].copy()

        drop_cols = [
            c for c in ("_dt", "wind_code", "time", "release_id", "published_at")
            if c in df_l1.columns
        ]
        df_l1 = df_l1.drop(columns=drop_cols, errors="ignore")

        # 同日同股去重（两种格式可能重叠），保留后出现的 hive 版本
        df_l1 = df_l1.drop_duplicates(subset=["symbol", "trade_date"], keep="last")

        # 单位归一：l1 的 vol_std_* 是小数（0.0339=3.39%），technical_indicators 的
        # vol_std_5/20/60 是百分数（3.39=3.39%）。同族不同量纲会让树模型学到伪分界，
        # 统一 ×100 对齐百分数口径（vol_atr_14 两边都是元，不动）
        for col in df_l1.columns:
            if col.startswith("vol_std_") and col in df_l1.columns:
                df_l1[col] = pd.to_numeric(df_l1[col], errors="coerce") * 100.0

    if df_l1 is not None and not df_l1.empty:
        _log(f"  l1_factors 合并后: {len(df_l1):,} rows, {len(df_l1.columns)} cols")
        df = df.merge(df_l1, on=["symbol", "trade_date"], how="left", suffixes=("", "_l1"))
        dup_cols = [c for c in df.columns if c.endswith("_l1")]
        if dup_cols:
            df = df.drop(columns=dup_cols, errors="ignore")
    else:
        _log("  l1_factors: 无数据或为空")

    # ── Step 3: l2_factors (微观结构/高频波动率/资金流) ──
    _log(f"读取 QuantDB l2_factors ({year})...")
    l2_path = f"{qdb}/6_ml_datasets/l2_factors"
    try:
        # union_by_name: 2026-03-02 前老分区无 OHLC 行情列，之后的分区有，
        # 跨分区 glob 会因 schema 不一致报错（unified 按位置对齐会串列）。
        query_l2 = f"""
        SELECT * EXCLUDE (time, release_id, published_at)
        FROM read_parquet('{l2_path}/**/*.parquet', hive_partitioning=true, union_by_name=true)
        WHERE dt BETWEEN {year_start} AND {year_end}
        """
        df_l2 = con.execute(query_l2).fetchdf()
    except Exception as e:
        _log(f"  l2_factors 读取失败: {e}")
        df_l2 = None

    if df_l2 is not None and not df_l2.empty:
        # 去除元数据列
        meta_cols = {"time", "release_id", "published_at"}
        drop_cols = [c for c in df_l2.columns if c in meta_cols]
        if drop_cols:
            df_l2 = df_l2.drop(columns=drop_cols, errors="ignore")

        df_l2["symbol"] = _normalize_symbol(df_l2["symbol"])

        if "trade_date" not in df_l2.columns and "dt" in df_l2.columns:
            # hive 分区的 dt 是 int (YYYYMMDD)，转为 datetime
            df_l2["trade_date"] = pd.to_datetime(df_l2["dt"].astype(str), format="%Y%m%d")
            df_l2 = df_l2.drop(columns=["dt"], errors="ignore")

        if df_l2 is not None:
            _log(f"  l2_factors: {len(df_l2):,} rows, {len(df_l2.columns)} cols")
            df = df.merge(df_l2, on=["symbol", "trade_date"], how="left", suffixes=("", "_l2"))
            dup_cols = [c for c in df.columns if c.endswith("_l2")]
            if dup_cols:
                df = df.drop(columns=dup_cols, errors="ignore")
    else:
        _log("  l2_factors: 无数据或为空")

    con.close()

    if df.empty:
        _log(f"  {year} 年 JOIN 后无数据，跳过")
        return None

    # ── Step 4: 丢弃泄漏列 + 去重 ──
    leaky_present = [c for c in _LEAKY_RETURN_COLS if c in df.columns]
    if leaky_present:
        df = df.drop(columns=leaky_present, errors="ignore")
        _log(f"  丢弃未来函数列: {leaky_present}")

    # 去重：l1/l2 与 base 重叠的列保留第一个出现（base 优先）
    dup_cols = df.columns[df.columns.duplicated()].tolist()
    if dup_cols:
        _log(f"  去重列: {dup_cols}")
        df = df.loc[:, ~df.columns.duplicated(keep="first")]

    # mom_ret_Nd 必须来自 l1_factors（过去收益），缺失说明 l1 未覆盖该年份
    for horizon in (1, 5, 20):
        col = f"mom_ret_{horizon}d"
        if col not in df.columns:
            _log(f"  警告: {col} 缺失，该年份无法用于 horizon={horizon} 训练")

    # 清理异常收益：l1 存在 +2670% 之类的脏值，会主导标签与训练损失。
    # A 股单日涨跌幅上限 20%（含 ST/新股放宽），超出 ±50% 一律视为脏数据置 NaN。
    for horizon in (1, 3, 5, 10, 20, 60, 120):
        col = f"mom_ret_{horizon}d"
        if col not in df.columns:
            continue
        limit = 0.5 * max(1, horizon) ** 0.5
        bad = df[col].abs() > limit
        n_bad = int(bad.sum())
        if n_bad:
            df.loc[bad, col] = np.nan
            _log(f"  清理 {col} 异常值: {n_bad} 行 (|ret| > {limit:.2f})")

    # 排序
    df = df.sort_values(["trade_date", "symbol"]).reset_index(drop=True)

    # ── Step 4.5: 加入 GTJA Alpha191 去重后因子 ──
    df = _add_gtja_factors(df)

    row_count = len(df)
    sym_count = int(df["symbol"].nunique())
    min_date = str(df["trade_date"].min())[:10]
    max_date = str(df["trade_date"].max())[:10]
    feature_cols = [c for c in df.columns if c not in ("symbol", "trade_date")]
    _log(f"  {year}: {row_count:,} 行, {sym_count} 只股票, {len(feature_cols)} 特征, {min_date}~{max_date}")

    if dry_run:
        return {"year": year, "row_count": row_count, "symbol_count": sym_count,
                "feature_count": len(feature_cols), "dry_run": True}

    # ── Step 5: 写入 parquet ──
    out_path = SNAPSHOT_DIR / f"model_features_{year}.parquet"
    df.to_parquet(str(out_path), index=False, engine="pyarrow")
    size_mb = out_path.stat().st_size / 1024 / 1024
    _log(f"  已写入: {out_path} ({size_mb:.1f}MB)")

    # ── Step 6: 写入 metadata.json ──
    meta = {
        "year": year,
        "output_start_date": min_date,
        "output_end_date": max_date,
        "trading_days": int(df["trade_date"].nunique()),
        "row_count": row_count,
        "symbol_count": sym_count,
        "implemented_feature_count": len(feature_cols),
        "feature_columns": feature_cols,
        "source": "quantdb (daily_forward + features_daily + l1_factors + l2_factors)",
        "generated_at": datetime.now().isoformat(),
    }
    meta_path = out_path.with_suffix(".metadata.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"  已写入 metadata: {meta_path}")

    return meta


def rollback_polluted(year: int | None = None) -> dict:
    """回滚被 update_feature_parquet.py 污染的快照（symbol 格式不一致的行）。

    过滤掉 symbol 含 '.' 后缀的行（如 600036.SH），恢复为干净的 6 位数字格式。
    """
    import duckdb

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    json_files = sorted(SNAPSHOT_DIR.glob("model_features_*.metadata.json"))
    if not json_files:
        return {"status": "skipped", "reason": "no metadata files found"}

    targets = []
    if year:
        targets = [SNAPSHOT_DIR / f"model_features_{year}.parquet"]
    else:
        targets = [
            SNAPSHOT_DIR / jf.name.replace(".metadata.json", ".parquet")
            for jf in json_files
        ]

    results = {"cleaned": 0, "files": []}
    con = duckdb.connect()

    for pq_path in targets:
        if not pq_path.exists():
            continue

        meta_path = pq_path.with_suffix(".metadata.json")
        before = con.execute(f"SELECT count(*), count(distinct symbol) FROM read_parquet('{pq_path}')").fetchone()
        after = con.execute(f"SELECT count(*), count(distinct symbol) FROM read_parquet('{pq_path}') WHERE symbol NOT LIKE '%.%'").fetchone()

        if before[0] == after[0]:
            _log(f"  {pq_path.name}: 无污染 ({before[0]:,} 行)")
            continue

        _log(f"  {pq_path.name}: 发现污染 — 总行 {before[0]:,} (含 {before[1]:,} 只) → 干净 {after[0]:,} ({after[1]:,} 只)")

        clean_df = con.execute(f"SELECT * FROM read_parquet('{pq_path}') WHERE symbol NOT LIKE '%.%'").fetchdf()
        clean_df.to_parquet(str(pq_path), index=False, engine="pyarrow")

        # 更新 metadata
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

        meta["row_count"] = len(clean_df)
        meta["symbol_count"] = int(clean_df["symbol"].nunique())
        if "trade_date" in clean_df.columns:
            td = pd.to_datetime(clean_df["trade_date"])
            meta["output_start_date"] = str(td.min())[:10]
            meta["output_end_date"] = str(td.max())[:10]
            meta["trading_days"] = int(td.dt.date.nunique())
        meta["rollback_at"] = datetime.now().isoformat()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        _log(f"  已回滚: {len(clean_df):,} 行, {meta['symbol_count']} 只")
        results["cleaned"] += 1
        results["files"].append({"file": pq_path.name, "before_rows": before[0], "after_rows": len(clean_df)})

    con.close()
    return results


def main():
    parser = argparse.ArgumentParser(description="从 QuantDB 生成特征快照")
    parser.add_argument("--year", type=int, default=0, help="指定年份 (默认: 全部)")
    parser.add_argument("--dry-run", action="store_true", help="仅检查不写入")
    parser.add_argument("--rollback", action="store_true", help="回滚被污染的快照（删除 symbol 格式不一致的行）")
    args = parser.parse_args()

    if args.rollback:
        result = rollback_polluted(year=args.year or None)
        _log(f"回滚完成: {json.dumps(result, ensure_ascii=False, default=str)}")
        return

    if not QDB_DIR.exists():
        _log(f"ERROR: QuantDB 数据目录不存在: {QDB_DIR}")
        sys.exit(1)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # 确定年份范围
    import duckdb
    con = duckdb.connect()
    qdb = str(QDB_DIR)
    r = con.execute(f"""
        SELECT min(dt), max(dt) FROM read_parquet('{qdb}/6_ml_datasets/features_daily/**/*.parquet', hive_partitioning=true)
    """).fetchone()
    con.close()

    min_year = int(str(r[0])[:4])
    max_year = int(str(r[1])[:4])
    _log(f"QuantDB features_daily 覆盖: {min_year}~{max_year}")

    if args.year:
        years = [args.year]
    else:
        years = list(range(min_year, max_year + 1))

    _log(f"将生成 {len(years)} 个年份: {years[0]}~{years[-1]}")

    results = []
    for y in years:
        meta = _build_snapshot(y, dry_run=args.dry_run)
        if meta:
            results.append(meta)

    _log(f"完成! 生成 {len(results)} 个快照")
    for r in results:
        _log(f"  {r['year']}: {r.get('row_count', '?'):,} 行, {r.get('symbol_count', '?')} 只股票, {r.get('feature_count', r.get('implemented_feature_count', '?'))} 特征")


if __name__ == "__main__":
    main()
