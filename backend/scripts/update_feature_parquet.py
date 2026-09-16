#!/usr/bin/env python3
"""更新 model_features_{year}.parquet，从 QuantDB 本地 parquet 读取数据。

数据源: QuantDB 本地 parquet (daily_forward 前复权日线 + 估值 + 技术指标 + 行业/概念)
口径:   前复权 (daily_forward) — 因子计算需要连续价格序列，消除除权除息缺口。
        撮合/行情层使用 daily_unadjusted (不复权)，两者口径有意区分。

用法:
    python update_feature_parquet.py                    # 自动补充所有缺失日期
    python update_feature_parquet.py --since 2026-05-23  # 从指定日期开始
    python update_feature_parquet.py --rebuild           # 重建全部日期
    python update_feature_parquet.py --dry-run           # 仅检查，不写入
"""

import argparse
import os
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)

from backend.shared.feature_defs import (  # noqa: F401 — 唯一实现；回导出保外部 import 兼容
    DB_CONCEPT_COLS,
    DB_FUNDAMENTAL_COLS,
    DB_INDEX_COLS,
    FEATURE_COLS,
    _add_nan_features,
    _compute_features_core,
    _ema,
    _kdj,
    _macd,
    _rsi,
    compute_features_for_group,
)

# ── 路径配置 ──
if os.path.exists("/app") and not os.environ.get("QUANTMIND_HOST_MODE"):
    FEATURE_SNAPSHOT_DIR = Path("/app/db/feature_snapshots")
    QDB_DATA_DIR = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    FEATURE_SNAPSHOT_DIR = PROJECT_ROOT / "db" / "feature_snapshots"
    QDB_DATA_DIR = Path(os.environ.get("QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")))

# QuantDB 子目录
QDB_KLINE_DIR = QDB_DATA_DIR / "1_kline_data"
QDB_SECTOR_DIR = QDB_DATA_DIR / "2_base_sector"
QDB_FIN_DIR = QDB_DATA_DIR / "3_financial_data"
QDB_TECH_DIR = QDB_DATA_DIR / "5_technical_derived"

# 默认 lookback: 250 交易日 ≈ 1 年，确保 mom_ret_120d / ma120 / ma60 等有足够窗口
DEFAULT_LOOKBACK_DAYS = 250


def _parquet_path_for_year(year: int) -> Path:
    return FEATURE_SNAPSHOT_DIR / f"model_features_{year}.parquet"


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ═══════════════════════════════════════════════════════════════════════════
# 数据读取 — QuantDB 本地 parquet
# ═══════════════════════════════════════════════════════════════════════════

# 从 QuantDB 读取的列（用于特征计算 + 辅助列）
DB_OHLCV_COLS = [
    "symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor",
]
DB_TECHNICAL_COLS = [
    "return_1d", "return_5d", "return_20d", "ma5", "ma20", "ma60",
    "rsi_14", "kdj_k", "macd_hist", "vol_std_20", "vol_atr_14",
    "turnover_rate", "beta_20",
    "flow_net_amount", "volume_ma_5", "amount_ma_5",
]

ALL_DB_COLS = list(dict.fromkeys(
    DB_OHLCV_COLS + DB_FUNDAMENTAL_COLS + DB_INDEX_COLS + DB_CONCEPT_COLS + DB_TECHNICAL_COLS
))


def _read_kline_forward(since: date, until: date) -> pd.DataFrame:
    """从 QuantDB daily_forward parquet 读取前复权 OHLCV。

    目录结构: 1_kline_data/daily_forward/dt=YYYYMMDD/*.parquet
    每个文件列: symbol, time, open, high, low, close, volume, amount, ...
    """
    kline_dir = QDB_KLINE_DIR / "daily_forward"
    if not kline_dir.exists():
        _log(f"  daily_forward 目录不存在: {kline_dir}")
        return pd.DataFrame()

    # 扫描日期分区目录
    parts = []
    for dt_dir in sorted(kline_dir.iterdir()):
        if not dt_dir.is_dir() or not dt_dir.name.startswith("dt="):
            continue
        dt_str = dt_dir.name[3:]  # "dt=20240304" → "20240304"
        try:
            dt = date(int(dt_str[:4]), int(dt_str[4:6]), int(dt_str[6:8]))
        except ValueError:
            continue
        if dt < since or dt > until:
            continue
        for pf in dt_dir.glob("*.parquet"):
            parts.append(pf)

    if not parts:
        return pd.DataFrame()

    _log(f"  读取 daily_forward: {len(parts)} 个分区文件")
    dfs = [pd.read_parquet(p, columns=["symbol", "time", "open", "high", "low", "close", "volume", "amount"])
           for p in parts]
    df = pd.concat(dfs, ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["time"]).dt.date
    df = df.drop(columns=["time"])
    # 确保数值类型
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _read_valuation(since: date, until: date) -> pd.DataFrame:
    """从 QuantDB valuation parquet 读取估值数据 (pe_ttm, pb, total_mv, float_mv 等).

    目录结构: 5_technical_derived/valuation/dt=YYYYMMDD/data.parquet
    """
    val_dir = QDB_TECH_DIR / "valuation"
    if not val_dir.exists():
        return pd.DataFrame()

    parts = []
    for dt_dir in sorted(val_dir.iterdir()):
        if not dt_dir.is_dir() or not dt_dir.name.startswith("dt="):
            continue
        dt_str = dt_dir.name[3:]
        try:
            dt = date(int(dt_str[:4]), int(dt_str[4:6]), int(dt_str[6:8]))
        except ValueError:
            continue
        if dt < since or dt > until:
            continue
        pf = dt_dir / "data.parquet"
        if pf.exists():
            parts.append(pf)

    if not parts:
        return pd.DataFrame()

    _log(f"  读取 valuation: {len(parts)} 个分区文件")
    cols = ["symbol", "time", "pe_ttm", "pb", "total_mv", "float_mv",
            "net_profit_ttm", "equity", "circulating_capital", "total_capital"]
    dfs = []
    for p in parts:
        try:
            available = pd.read_parquet(p).columns.tolist()
            use_cols = [c for c in cols if c in available]
            dfs.append(pd.read_parquet(p, columns=use_cols))
        except Exception:
            continue
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["time"]).dt.date
    df = df.drop(columns=["time"], errors="ignore")
    # 计算衍生列
    if "pe_ttm" in df.columns:
        df["ep_ttm"] = 1.0 / df["pe_ttm"].replace(0, np.nan)
    if "pb" in df.columns:
        df["bp"] = 1.0 / df["pb"].replace(0, np.nan)
    if "total_mv" in df.columns:
        df["ln_mv_total"] = np.log(df["total_mv"].clip(lower=1))
    if "equity" in df.columns and "net_profit_ttm" in df.columns:
        df["roe"] = (df["net_profit_ttm"] / df["equity"].clip(lower=1)).clip(-5, 5)
    return df


def _read_technical_indicators(since: date, until: date) -> pd.DataFrame:
    """从 QuantDB technical_indicators parquet 读取技术指标.

    目录结构: 5_technical_derived/technical_indicators/dt=YYYYMMDD/data.parquet
    """
    ti_dir = QDB_TECH_DIR / "technical_indicators"
    if not ti_dir.exists():
        return pd.DataFrame()

    parts = []
    for dt_dir in sorted(ti_dir.iterdir()):
        if not dt_dir.is_dir() or not dt_dir.name.startswith("dt="):
            continue
        dt_str = dt_dir.name[3:]
        try:
            dt = date(int(dt_str[:4]), int(dt_str[4:6]), int(dt_str[6:8]))
        except ValueError:
            continue
        if dt < since or dt > until:
            continue
        pf = dt_dir / "data.parquet"
        if pf.exists():
            parts.append(pf)

    if not parts:
        return pd.DataFrame()

    _log(f"  读取 technical_indicators: {len(parts)} 个分区文件")
    cols = ["symbol", "time", "return_1d", "return_5d", "return_20d",
            "ma5", "ma10", "ma20", "ma60", "rsi_6", "rsi_14",
            "kdj_k", "kdj_d", "kdj_j", "macd_dif", "macd_dea", "macd_hist",
            "vol_std_5", "vol_std_20", "vol_std_60", "vol_atr_14",
            "vol_to_ma5", "vol_to_ma20", "volume_ma_3", "amount_ma_5",
            "beta_20", "pct_change"]
    dfs = []
    for p in parts:
        try:
            available = pd.read_parquet(p).columns.tolist()
            use_cols = [c for c in cols if c in available]
            dfs.append(pd.read_parquet(p, columns=use_cols))
        except Exception:
            continue
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["time"]).dt.date
    df = df.drop(columns=["time"], errors="ignore")
    # 重命名 vol_to_ma5 → volume_ratio_5, vol_to_ma20 → volume_ratio_20
    if "vol_to_ma5" in df.columns:
        df["volume_ratio_5"] = df.pop("vol_to_ma5")
    if "vol_to_ma20" in df.columns:
        df["volume_ratio_20"] = df.pop("vol_to_ma20")
    return df


def _read_instrument_detail() -> pd.DataFrame:
    """从 instrument_detail.parquet 读取行业编码、ST 标识、复权因子等静态信息。

    返回: DataFrame[symbol, is_st, industry, adj_factor, ind_code_l1, listing_market]
    """
    ind_path = QDB_SECTOR_DIR / "instrument_detail" / "instrument_list.parquet"
    if not ind_path.exists():
        ind_path = QDB_SECTOR_DIR / "instrument_detail" / "instrument_detail.parquet"
    if not ind_path.exists():
        _log(f"  instrument_detail.parquet 不存在: {ind_path}")
        return pd.DataFrame()

    _log("  读取 instrument_detail")
    use_cols = ["Symbol", "IsSTGP", "rs_hycode_sim", "rs_hyname",
                "ZAF", "tdx_dycode", "tdx_dyname", "BelongHS300", "BelongRZRQ"]
    available = pd.read_parquet(ind_path).columns.tolist()
    read_cols = [c for c in use_cols if c in available]
    df = pd.read_parquet(ind_path, columns=read_cols)

    # 统一 symbol 格式: "000001.SZ" (已是后缀格式)
    df = df.rename(columns={"Symbol": "symbol"})
    df["symbol"] = df["symbol"].astype(str).str.strip()

    # is_st: IsSTGP 是字符串 "0"/"1"
    if "IsSTGP" in df.columns:
        df["is_st"] = pd.to_numeric(df["IsSTGP"], errors="coerce").fillna(0).astype(int)
    else:
        df["is_st"] = 0

    # industry: 使用 rs_hyname (行业名称)
    if "rs_hyname" in df.columns:
        df["industry"] = df["rs_hyname"].astype(str).str.strip()
        df.loc[df["industry"].isin(["", "nan", "None"]), "industry"] = np.nan
    else:
        df["industry"] = np.nan

    # adj_factor: daily_forward 已是前复权，factor=1.0
    # (instrument_detail.ZAF 是涨跌幅不是复权因子，不可用作 adj_factor)
    if "adj_factor" not in df.columns:
        df["adj_factor"] = 1.0
    else:
        df["adj_factor"] = 1.0  # 前复权数据 factor 恒 1.0

    # listing_market: 从 tdx_dycode 推断
    if "tdx_dycode" in df.columns:
        df["listing_market"] = df["tdx_dycode"].astype(str).apply(
            lambda x: "SH" if x in ("1", "7") else ("SZ" if x in ("2", "8") else "BJ")
        )
    else:
        df["listing_market"] = "Unknown"

    # 指数成分标记
    if "BelongHS300" in df.columns:
        df["idx_hs300"] = pd.to_numeric(df["BelongHS300"], errors="coerce").fillna(0).astype(int)
    else:
        df["idx_hs300"] = 0
    if "BelongRZRQ" in df.columns:
        df["idx_margin"] = pd.to_numeric(df["BelongRZRQ"], errors="coerce").fillna(0).astype(int)
    else:
        df["idx_margin"] = 0

    # ind_code_l1: rs_hycode_sim → CatBoost 整数编码
    if "rs_hycode_sim" in df.columns:
        df["ind_code_l1"] = pd.Categorical(df["rs_hycode_sim"]).codes.astype(np.float32)
        df.loc[df["rs_hycode_sim"].isna() | (df["rs_hycode_sim"] == ""), "ind_code_l1"] = -1
    else:
        df["ind_code_l1"] = -1.0

    df["ind_code_l2"] = -1.0
    df["idx_all"] = 1  # 所有 A 股
    df["idx_zz1000"] = 0
    df["idx_chinext"] = 0

    return df[["symbol", "is_st", "industry", "adj_factor", "listing_market",
               "idx_all", "idx_hs300", "idx_zz1000", "idx_chinext", "idx_margin",
               "ind_code_l1", "ind_code_l2"]]


def _read_sector_concepts() -> pd.DataFrame:
    """从 sector_members.parquet 读取概念标签，转为 0/1 列。

    返回: DataFrame[symbol, concept_ai, concept_chip, ...]
    """
    sm_path = QDB_SECTOR_DIR / "sector_concept" / "sector_members.parquet"
    if not sm_path.exists():
        _log(f"  sector_members.parquet 不存在: {sm_path}")
        return pd.DataFrame()

    _log("  读取 sector_concept")
    df = pd.read_parquet(sm_path)

    # 概念名称 → 列名映射
    CONCEPT_MAP = {
        "人工智能": "concept_ai", "AI": "concept_ai",
        "芯片": "concept_chip", "半导体": "concept_chip",
        "新能源": "concept_new_energy",
        "光伏": "concept_pv",
        "军工": "concept_military", "国防": "concept_military",
        "医药": "concept_medical", "医疗": "concept_medical",
        "金融科技": "concept_fintech", "互金": "concept_fintech",
        "消费": "concept_consumption",
        "国企": "concept_state_owned", "央企": "concept_state_owned",
        "锂电": "concept_lithium", "锂电池": "concept_lithium",
    }

    # 过滤概念类型
    concept_df = df[df.get("SectorType", "").astype(str).str.contains("概念", na=False)] if "SectorType" in df.columns else df

    # 构建映射
    sym_col = "Symbol" if "Symbol" in concept_df.columns else "symbol"
    concept_df = concept_df.rename(columns={sym_col: "symbol", "SectorName": "concept_name"})
    concept_df["symbol"] = concept_df["symbol"].astype(str).str.strip()
    concept_df["col_name"] = concept_df["concept_name"].map(CONCEPT_MAP)

    valid = concept_df[concept_df["col_name"].notna()]
    if valid.empty:
        # 返回空 DataFrame 带所有 concept 列
        return pd.DataFrame(columns=["symbol"] + list(CONCEPT_MAP.values()))

    # pivot: symbol × concept_col → 0/1
    pivot = valid.groupby(["symbol", "col_name"]).size().reset_index(name="_cnt")
    pivot = pivot.pivot(index="symbol", columns="col_name", values="_cnt").fillna(0).astype(int)
    pivot.columns = list(pivot.columns)  # flatten

    # 确保所有 concept 列都存在
    for col in CONCEPT_MAP.values():
        if col not in pivot.columns:
            pivot[col] = 0

    pivot = pivot.reset_index()
    return pivot


def fetch_data_from_quantdb(since: date, until: date, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> pd.DataFrame:
    """从 QuantDB 本地 parquet 读取全部数据（含 lookback 窗口）。

    合并 daily_forward (OHLCV) + valuation (估值) + technical_indicators (技术指标)
         + instrument_detail (行业/ST/复权因子) + sector_concepts (概念标签)
    """
    data_since = since - timedelta(days=lookback_days)

    # 1. 前复权 OHLCV
    kline = _read_kline_forward(data_since, until)
    if kline.empty:
        _log("  daily_forward 无数据")
        return pd.DataFrame()
    _log(f"  OHLCV: {len(kline):,} 行, {kline['symbol'].nunique()} 只股票")

    # 2. 估值数据
    val = _read_valuation(data_since, until)
    if not val.empty:
        _log(f"  估值: {len(val):,} 行")

    # 3. 技术指标
    ti = _read_technical_indicators(data_since, until)
    if not ti.empty:
        _log(f"  技术指标: {len(ti):,} 行")

    # 4. 静态信息 (行业/ST/复权因子/指数成分)
    inst = _read_instrument_detail()
    if not inst.empty:
        _log(f"  instrument_detail: {len(inst)} 只股票")

    # 5. 概念标签
    concepts = _read_sector_concepts()
    if not concepts.empty:
        _log(f"  概念标签: {len(concepts)} 只股票")

    # ── 合并 ──
    df = kline

    # 合并估值 (按 symbol + trade_date)
    if not val.empty:
        val_cols = [c for c in val.columns if c not in df.columns]
        if val_cols:
            df = df.merge(val[["symbol", "trade_date"] + val_cols],
                          on=["symbol", "trade_date"], how="left")

    # 合并技术指标 (按 symbol + trade_date)
    if not ti.empty:
        ti_cols = [c for c in ti.columns if c not in df.columns]
        if ti_cols:
            df = df.merge(ti[["symbol", "trade_date"] + ti_cols],
                          on=["symbol", "trade_date"], how="left")

    # 合并静态信息 (按 symbol, 广播到所有日期)
    if not inst.empty:
        inst_cols = [c for c in inst.columns if c not in df.columns]
        if inst_cols:
            df = df.merge(inst[["symbol"] + inst_cols], on="symbol", how="left")

    # 合并概念标签 (按 symbol, 广播到所有日期)
    if not concepts.empty:
        concept_cols = [c for c in concepts.columns if c not in df.columns]
        if concept_cols:
            df = df.merge(concepts[["symbol"] + concept_cols], on="symbol", how="left")

    # 填充缺失的 concept / index 列为 0
    for col in DB_CONCEPT_COLS + DB_INDEX_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
        else:
            df[col] = 0

    # 填充缺失的 is_st
    if "is_st" in df.columns:
        df["is_st"] = pd.to_numeric(df["is_st"], errors="coerce").fillna(0).astype(int)
    else:
        df["is_st"] = 0

    # 填充缺失的 adj_factor
    if "adj_factor" not in df.columns:
        df["adj_factor"] = 1.0
    else:
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0)

    # 排序
    df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return df


# ═══════════════════════════════════════════════════════════════════════════
# 特征计算
# ═══════════════════════════════════════════════════════════════════════════



def _normalize_industry(series: pd.Series) -> pd.Series:
    """标准化行业名称，去除 CSRC 代码前缀等变体。"""
    s = series.astype(str).str.strip()
    # 去掉 CSRC 代码前缀 (C39计算机 → 计算机, A01农业 → 农业)
    s = s.str.replace(r'^[A-Z]?\d{1,3}', '', regex=True)
    # 去掉尾部罗马数字
    s = s.str.replace(r'[ⅠⅡⅢIV]+$', '', regex=True)
    # 去掉多余空格
    s = s.str.strip()
    # 映射空字符串回 NaN
    s = s.replace({'': np.nan, 'nan': np.nan, 'None': np.nan, 'NoneType': np.nan})
    return s


def _compute_industry_codes(df: pd.DataFrame) -> pd.DataFrame:
    """从 instrument_detail.parquet 填充 ind_code_l1 / ind_code_l2 行业编码。

    instrument_detail.parquet 包含 rs_hycode_sim (CSRC 行业编码)，
    将其映射为 CatBoost 可用的整数类别编码，缺失行业填 -1。

    注意: instrument_detail 的 Symbol 已是后缀格式 (如 "600036.SH")，
    无需 zfill + 加后缀，直接 merge 即可。
    """
    from pathlib import Path as _Path

    ind_path = QDB_SECTOR_DIR / "instrument_detail" / "instrument_list.parquet"
    if not ind_path.exists():
        ind_path = QDB_SECTOR_DIR / "instrument_detail" / "instrument_detail.parquet"
    if not ind_path.exists():
        _log("    instrument_detail.parquet 不存在，行业编码填充为 -1")
        df["ind_code_l1"] = -1.0
        df["ind_code_l2"] = -1.0
        return df

    ind_df = pd.read_parquet(ind_path, columns=["Symbol", "rs_hycode_sim"])
    sym_col = "Symbol" if "Symbol" in ind_df.columns else "symbol"
    ind_map = ind_df.rename(columns={sym_col: "symbol", "rs_hycode_sim": "ind_code_l1"})
    # Symbol 已是 "600036.SH" 格式，直接用，不要 zfill(6)
    ind_map["symbol"] = ind_map["symbol"].astype(str).str.strip()
    ind_map["ind_code_l1"] = pd.Categorical(ind_map["ind_code_l1"]).codes.astype(np.float32)
    ind_map["ind_code_l2"] = -1.0

    # 删除 df 中已有的 ind_code_l1/l2 (可能来自 fetch_data_from_quantdb 的静态信息)
    for col in ["ind_code_l1", "ind_code_l2"]:
        if col in df.columns:
            df = df.drop(columns=[col])

    df = df.merge(ind_map[["symbol", "ind_code_l1", "ind_code_l2"]], on="symbol", how="left")
    df["ind_code_l1"] = df["ind_code_l1"].fillna(-1)
    df["ind_code_l2"] = df["ind_code_l2"].fillna(-1)
    _log(f"    行业编码填充完成: {ind_map['ind_code_l1'].nunique()} 个 L1 行业")
    return df


def _compute_industry_features(all_feat: pd.DataFrame) -> pd.DataFrame:
    """跨股票计算行业因子（需要在全部股票特征计算完成后执行）。"""
    # 过滤无行业的行
    valid_ind = all_feat["industry"].notna() & (all_feat["industry"] != "")
    if valid_ind.sum() < 100:
        _log("    行业数据不足，跳过行业因子计算")
        return all_feat

    # 1. 行业日聚合指标
    ind_daily = all_feat[valid_ind].groupby(["industry", "trade_date"]).agg(
        ind_close=("close", "median"),
        ind_flow=("flow_net_amount", "mean"),
        ind_volume=("volume", "sum"),
        ind_amount=("amount", "sum"),
        ind_turnover=("liq_turnover_os", "mean"),
        ind_mv=("ln_mv_total", "mean"),
        ind_ret_1d_stock=("mom_ret_1d", "median"),
    ).reset_index()

    ind_daily = ind_daily.sort_values(["industry", "trade_date"])

    # 行业收益率 (1d, 5d, 10d, 20d)
    ind_daily["ind_ret_1d"] = ind_daily.groupby("industry")["ind_close"].pct_change()
    ind_daily["ind_ret_5d"] = ind_daily.groupby("industry")["ind_close"].pct_change(5)
    ind_daily["ind_ret_10d"] = ind_daily.groupby("industry")["ind_close"].pct_change(10)
    ind_daily["ind_ret_20d"] = ind_daily.groupby("industry")["ind_close"].pct_change(20)

    # 行业波动率 (20日)
    ind_ret = ind_daily.groupby("industry")["ind_close"].pct_change()
    ind_daily["ind_vol_20"] = ind_ret.rolling(20, min_periods=5).std()

    # 行业强度 (20日/60日)
    ind_std_20 = ind_ret.rolling(20, min_periods=5).std().clip(lower=1e-8)
    ind_daily["ind_strength_20"] = ind_daily["ind_ret_20d"] / ind_std_20
    ind_std_60 = ind_ret.rolling(60, min_periods=10).std().clip(lower=1e-8)
    ind_daily["ind_strength_60"] = ind_daily.groupby("industry")["ind_close"].pct_change(60) / ind_std_60

    # 行业换手率/成交额 (20日均值)
    ind_daily["ind_turnover_20"] = ind_daily.groupby("industry")["ind_turnover"].rolling(20, min_periods=5).mean().values
    ind_daily["ind_amount_20"] = ind_daily.groupby("industry")["ind_amount"].rolling(20, min_periods=5).mean().values

    # 行业内动量排名（截面排名）
    ind_daily["ind_momentum_rank_20"] = ind_daily.groupby("trade_date")["ind_ret_20d"].rank(pct=True)

    # 行业离散度 (20日个股收益标准差)
    stock_disp = all_feat[valid_ind].groupby(["industry", "trade_date"])["mom_ret_1d"].std().reset_index()
    stock_disp.columns = ["industry", "trade_date", "ind_dispersion_20"]
    ind_daily = ind_daily.merge(stock_disp, on=["industry", "trade_date"], how="left")

    # 行业涨跌家数
    stock_breadth = all_feat[valid_ind].groupby(["industry", "trade_date"]).agg(
        ind_up_breadth_20=("mom_ret_1d", lambda x: (x > 0).sum()),
        ind_down_breadth_20=("mom_ret_1d", lambda x: (x < 0).sum()),
    ).reset_index()
    ind_daily = ind_daily.merge(stock_breadth, on=["industry", "trade_date"], how="left")

    # 行业相对指标 (行业/全市场)
    mkt_daily = all_feat.groupby("trade_date").agg(
        mkt_volume=("volume", "sum"),
        mkt_amount=("amount", "sum"),
    ).reset_index()
    ind_daily = ind_daily.merge(mkt_daily, on="trade_date", how="left")
    ind_daily["ind_relative_volume_20"] = (ind_daily["ind_volume"] / ind_daily["mkt_volume"].clip(lower=1)).clip(0, 1)
    ind_daily["ind_relative_volatility_20"] = (ind_daily["ind_vol_20"] / ind_daily.groupby("trade_date")["ind_vol_20"].transform("median").clip(lower=1e-8)).clip(0, 5)
    ind_daily["ind_relative_flow_20"] = (ind_daily["ind_flow"] / ind_daily["mkt_amount"].clip(lower=1)).clip(0, 1)

    # 行业市值/价值排名
    ind_daily["ind_value_rank"] = ind_daily.groupby("trade_date")["ind_mv"].rank(pct=True)
    ind_daily["ind_size_rank"] = ind_daily.groupby("trade_date")["ind_volume"].rank(pct=True)

    # 清理临时列
    drop_cols = [c for c in ["ind_volume", "ind_amount", "ind_turnover", "ind_mv",
                              "ind_ret_1d_stock", "mkt_volume", "mkt_amount"] if c in ind_daily.columns]
    ind_daily = ind_daily.drop(columns=drop_cols)

    # Merge 回主表
    merge_cols = [c for c in ind_daily.columns if c not in ["industry", "trade_date"]]
    # 先删除主表中已有的占位列
    for col in merge_cols:
        if col in all_feat.columns:
            all_feat = all_feat.drop(columns=[col])

    all_feat = all_feat.merge(
        ind_daily,
        on=["industry", "trade_date"],
        how="left",
    )

    _log(f"    行业因子计算完成: {ind_daily['industry'].nunique()} 个行业, {len(merge_cols)} 个因子")
    return all_feat


def compute_all_features(df: pd.DataFrame, target_dates: set) -> pd.DataFrame:
    """为所有股票计算特征，只返回 target_dates 中的数据。"""
    # 标准化行业名称
    if "industry" in df.columns:
        df["industry"] = _normalize_industry(df["industry"])

    _log(f"  计算特征（{df['symbol'].nunique()} 只股票）...")
    results = []
    total = df["symbol"].nunique()
    done = 0

    for _sym, group in df.groupby("symbol"):
        feat = compute_features_for_group(group)
        results.append(feat)
        done += 1
        if done % 1000 == 0:
            _log(f"    进度: {done}/{total}")

    all_feat = pd.concat(results, ignore_index=True)

    # 跨股票计算行业因子
    _log("  计算行业因子...")
    all_feat = _compute_industry_features(all_feat)

    # 填充行业编码 (CatBoost cat_features)
    _log("  填充行业编码...")
    all_feat = _compute_industry_codes(all_feat)

    all_feat = all_feat[all_feat["trade_date"].isin(target_dates)].copy()
    return all_feat


# ═══════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="更新 feature parquet")
    parser.add_argument("--since", default="", help="起始日期 (默认: parquet 最后日期+1)")
    parser.add_argument("--until", default="", help="截止日期 (默认: 今天)")
    parser.add_argument("--dry-run", action="store_true", help="仅检查不写入")
    parser.add_argument("--rebuild", action="store_true", help="重建全部特征")
    parser.add_argument("--year", type=int, default=0, help="指定年份 (默认: 当前年份)")
    args = parser.parse_args()

    year = args.year or date.today().year
    PARQUET_PATH = _parquet_path_for_year(year)

    if not PARQUET_PATH.exists():
        _log(f"parquet 文件不存在，将创建: {PARQUET_PATH}")
        # 创建空 parquet 以便后续逻辑正常工作
        empty_df = pd.DataFrame({"trade_date": pd.Series(dtype="object"), "symbol": pd.Series(dtype="str")})
        empty_df.to_parquet(str(PARQUET_PATH), index=False, engine="pyarrow")

    # 读取现有 parquet
    _log(f"读取现有 parquet: {PARQUET_PATH}")
    existing = pd.read_parquet(PARQUET_PATH, engine="pyarrow")
    existing["trade_date"] = pd.to_datetime(existing["trade_date"]).dt.date
    max_date = existing["trade_date"].max() if len(existing) else date(year, 1, 1) - timedelta(days=1)
    _log(f"  现有数据: {len(existing):,} 行, {existing['symbol'].nunique() if len(existing) else 0} 只股票")
    if len(existing):
        _log(f"  日期范围: {existing['trade_date'].min()} ~ {max_date}")

    # 确定日期范围
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    since = date.fromisoformat(args.since) if args.since else (max_date + timedelta(days=1) if len(existing) else year_start)
    until = date.fromisoformat(args.until) if args.until else min(date.today(), year_end)

    if args.rebuild:
        since = year_start
        _log(f"  REBUILD 模式: 重建 {year} 年 {since} ~ {until}")

    _log(f"  需要补充: {since} ~ {until}")

    if since > until and not args.rebuild:
        _log("无需更新（parquet 已是最新）")
        return

    if args.dry_run:
        _log("DRY RUN 模式，不写入")
        return

    # 从 QuantDB 读取数据
    _log(f"从 QuantDB 本地 parquet 读取数据（含 {DEFAULT_LOOKBACK_DAYS} 天 lookback）...")
    db_df = fetch_data_from_quantdb(since, until, lookback_days=DEFAULT_LOOKBACK_DAYS)

    if db_df.empty:
        _log("DB 中没有新数据")
        return

    _log(f"  读取到 {len(db_df):,} 行, {db_df['symbol'].nunique()} 只股票")

    # 计算特征
    target_dates = set()
    d = since
    while d <= until:
        target_dates.add(d)
        d += timedelta(days=1)

    new_data = compute_all_features(db_df, target_dates)
    _log(f"  计算完成: {len(new_data):,} 行")

    if new_data.empty:
        _log("没有有效数据")
        return

    # 确定输出列（parquet 已有列 + 新增列，去重）
    existing_cols = set(existing.columns)
    _new_cols = set(new_data.columns)
    all_cols = list(dict.fromkeys(list(existing.columns) + [c for c in new_data.columns if c not in existing_cols]))

    # 类型感知的填充：string/object 列用 None/空字符串，数值列用 NaN（不是 0）
    # 防止 industry 等字符串列被 fill_value=0 污染导致 pyarrow 写 parquet 失败
    import numpy as np
    for col in all_cols:
        if col in new_data.columns:
            continue
        # new_data 缺这一列，需要补
        if col in existing.columns:
            dtype = existing[col].dtype
            if dtype is np.dtype('O') or pd.api.types.is_string_dtype(dtype):
                new_data[col] = None
            elif pd.api.types.is_integer_dtype(dtype):
                new_data[col] = pd.NA  # 用 nullable Int
            else:
                new_data[col] = np.nan
        else:
            new_data[col] = np.nan
    new_data = new_data[all_cols]

    # 合并
    if args.rebuild:
        combined = new_data
    else:
        overlap_dates = set(new_data["trade_date"].unique()) & set(existing["trade_date"].unique())
        if overlap_dates:
            _log(f"  发现重叠日期 {len(overlap_dates)} 天，将覆盖")
            existing = existing[~existing["trade_date"].isin(overlap_dates)]
        # 对齐列（已有数据缺少的新列：同样按类型填充）
        for c in all_cols:
            if c not in existing.columns:
                if c in new_data.columns:
                    dtype = new_data[c].dtype
                    if dtype is np.dtype('O') or pd.api.types.is_string_dtype(dtype):
                        existing[c] = None
                    elif pd.api.types.is_integer_dtype(dtype):
                        existing[c] = pd.NA
                    else:
                        existing[c] = np.nan
                else:
                    existing[c] = np.nan
        existing = existing[all_cols]
        combined = pd.concat([existing, new_data], ignore_index=True)

    combined = combined.sort_values(["trade_date", "symbol"]).reset_index(drop=True)

    _log(f"合并后: {len(combined):,} 行, {len(combined.columns)} 列")
    _log(f"日期: {combined['trade_date'].min()} ~ {combined['trade_date'].max()}")

    # 写入 parquet
    combined.to_parquet(str(PARQUET_PATH), index=False, engine="pyarrow")
    _log(f"已写入: {PARQUET_PATH} ({PARQUET_PATH.stat().st_size / 1024 / 1024:.1f}MB)")

    # 验证
    verify = pd.read_parquet(PARQUET_PATH, engine="pyarrow")
    verify["trade_date"] = pd.to_datetime(verify["trade_date"]).dt.date
    _log(f"验证: {len(verify):,} 行, {len(verify.columns)} 列, 最新日期 {verify['trade_date'].max()}")

    # 检查覆盖率
    latest = verify[verify["trade_date"] == verify["trade_date"].max()]
    _log(f"最新日期 {len(latest)} 只股票:")
    for col_group, cols in [
        ("OHLCV", ["open", "high", "low", "close", "volume"]),
        ("动量", ["mom_ret_1d", "mom_ret_5d", "mom_rsi_14"]),
        ("波动率", ["vol_std_20", "vol_atr_14"]),
        ("流动性", ["liq_volume", "liq_amihud_20"]),
        ("资金流", ["flow_net_amount", "flow_vpin"]),
        ("基本面", ["pe_ttm", "pb", "ln_mv_total"]),
        ("指数", ["idx_hs300", "idx_zz1000", "idx_chinext"]),
        ("概念", ["concept_ai", "concept_chip", "concept_new_energy"]),
    ]:
        coverage = []
        for col in cols:
            if col in latest.columns:
                non_null = latest[col].notna().sum()
                coverage.append(f"{col}={non_null}")
        _log(f"  [{col_group}] {', '.join(coverage)}")

    _log("完成!")

    # 生成 metadata.json
    try:
        import json as _json
        meta_path = PARQUET_PATH.with_suffix(".metadata.json")
        feature_cols = [c for c in combined.columns if c not in ("trade_date", "symbol", "instrument")]
        meta = {
            "year": year,
            "calc_start_date": str(since - timedelta(days=DEFAULT_LOOKBACK_DAYS)),
            "output_start_date": str(combined["trade_date"].min()),
            "output_end_date": str(combined["trade_date"].max()),
            "lookback_days": DEFAULT_LOOKBACK_DAYS,
            "trading_days": int(combined["trade_date"].nunique()),
            "row_count": len(combined),
            "symbol_count": int(combined["symbol"].nunique()),
            "implemented_feature_count": len(feature_cols),
            "feature_columns": feature_cols,
            "source": "quantdb",
            "data_source": "quantdb_local_parquet",
            "adjust": "qfq",
        }
        meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(f"已写入 metadata: {meta_path}")
    except Exception as exc:
        _log(f"metadata.json 写入失败: {exc}")


if __name__ == "__main__":
    main()
