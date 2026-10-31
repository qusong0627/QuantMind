#!/usr/bin/env python3
"""
因子评估脚本 - 科学筛选有效因子
=================================

评估维度:
1. IC (Information Coefficient) - 因子值与未来收益的相关性
2. ICIR (IC Information Ratio) - IC 的稳定性 (均值/标准差)
3. Rank IC - Spearman 秩相关，对异常值更鲁棒
4. 因子衰减 - IC 随预测窗口延长的变化
5. 因子相关性 - 识别冗余因子
6. 因子单调性 - 分5组后收益是否单调递增

用法:
    python evaluate_factors.py                    # 评估全部因子
    python evaluate_factors.py --top 30           # 只看 Top 30
    python evaluate_factors.py --category momentum # 按类别筛选
    python evaluate_factors.py --export           # 导出推荐因子列表

输出:
    - 因子 IC/ICIR 排名表
    - 因子相关性热力图
    - 推荐因子列表 (去冗余后)
"""

import argparse
import os
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# 报告输出目录（旧 feature_snapshots 已废弃，统一落到 QuantDB releases 旁）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data" / "quantdb" / "reports"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ═══════════════════════════════════════════════════════════════════════════
# 因子分类
# ═══════════════════════════════════════════════════════════════════════════

FACTOR_CATEGORIES = {
    "momentum": ["mom_"],
    "volatility": ["vol_std", "vol_true", "vol_atr", "vol_parkinson", "vol_gk", "vol_rs"],
    "liquidity": ["liq_"],
    "flow": ["flow_"],
    "style": ["style_"],
    "kline": ["kline_", "prel_"],
    "tech": ["tech_"],
    "alpha": ["alpha_"],
    "fundamental": ["fund_", "pe_", "pb_", "roe", "bp", "ep_"],
    "industry": ["ind_"],
    "trend": ["trend_", "consecutive_"],
    "price_position": ["price_position", "dist_to_", "ret_rank"],
    # ── L2 微观结构因子（15 子类，全部 micro_ 前缀，按语义二分）──
    "l2_micro_liquidity": ["micro_liquidity_", "micro_amihud"],
    "l2_vpin": ["micro_vpin", "micro_pin", "micro_order_flow_toxicity", "micro_toxicity"],
    "l2_informed": ["micro_informed", "micro_liquidity_mining", "micro_order_imbalance_tox", "micro_vpin_bayesian"],
    "l2_spread": ["micro_qsp", "micro_esp", "micro_aqsp", "micro_aesp", "micro_spread", "micro_bid_ask"],
    "l2_realized_vol": ["vol_realized", "vol_rskew", "vol_rkurt", "vol_sjv", "vol_jump"],
    "l2_flow": ["flow_net", "flow_buy", "flow_sell", "flow_super", "flow_large", "flow_medium",
                "flow_small", "flow_imbalance", "flow_consistency", "flow_money", "flow_big",
                "flow_large_pct"],
    "l2_depth": ["micro_depth"],
    "l2_cancel": ["flow_cancel"],
    "l2_order": ["flow_order"],
    "l2_zone": ["micro_zone"],
    "l2_auction": ["micro_open", "micro_call", "micro_close_auction", "micro_lunch"],
    "l2_jump": ["micro_jump"],
    "l2_impact": ["micro_kyle", "micro_price_impact", "micro_impact"],
    "l2_trade_micro": ["micro_trade"],
    "l2_volume_dist": ["vol_turnover", "vol_price", "vol_up_down", "vol_large_trade",
                       "vol_tick", "vol_skew", "vol_kurt", "vol_gini", "vol_persistence"],
    "l2_adverse": ["micro_adverse", "micro_realized_spread", "micro_information_share"],
}


def get_factor_category(col: str) -> str:
    """根据列名前缀判断因子类别"""
    for cat, prefixes in FACTOR_CATEGORIES.items():
        for prefix in prefixes:
            if col.startswith(prefix):
                return cat
    return "other"


# ═══════════════════════════════════════════════════════════════════════════
# IC 计算
# ═══════════════════════════════════════════════════════════════════════════

def compute_ic_series(
    df: pd.DataFrame,
    factor_col: str,
    forward_col: str = "fwd_ret_5d",
    method: str = "spearman",
) -> pd.Series:
    """
    计算因子的每日 IC 序列

    Args:
        df: 包含 symbol, trade_date, factor_col, forward_col 的 DataFrame
        factor_col: 因子列名
        forward_col: 未来收益列名
        method: 'spearman' (Rank IC) 或 'pearson' (Normal IC)

    Returns:
        IC 时间序列 (index=trade_date)
    """
    # 按日期分组计算截面相关
    ic_series = df.groupby("trade_date").apply(
        lambda g: g[[factor_col, forward_col]].corr(method=method).iloc[0, 1]
        if len(g) > 10 and g[factor_col].std() > 1e-8 else np.nan
    )
    return ic_series.dropna()


def compute_ic_stats(ic_series: pd.Series, name: str = "") -> dict:
    """计算 IC 统计指标"""
    if len(ic_series) < 5:
        return {
            "factor": name,
            "ic_mean": np.nan,
            "ic_std": np.nan,
            "icir": np.nan,
            "ic_positive_ratio": np.nan,
            "ic_abs_mean": np.nan,
            "sample_days": len(ic_series),
        }

    ic_mean = ic_series.mean()
    ic_std = ic_series.std()
    icir = ic_mean / ic_std if ic_std > 1e-8 else 0

    return {
        "factor": name,
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "icir": icir,
        "ic_positive_ratio": (ic_series > 0).mean(),  # IC > 0 的比例
        "ic_abs_mean": ic_series.abs().mean(),
        "sample_days": len(ic_series),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 因子分组收益 (分位数分析)
# ═══════════════════════════════════════════════════════════════════════════

def compute_quantile_returns(
    df: pd.DataFrame,
    factor_col: str,
    forward_col: str = "fwd_ret_5d",
    n_quantiles: int = 5,
) -> dict:
    """
    按因子值分 N 组，计算各组平均收益

    理想情况: 从 Q1 到 Q5 收益单调递增 (做多 Q5, 做空 Q1)

    Returns:
        {
            "q1_ret": 最低组平均收益,
            "q5_ret": 最高组平均收益,
            "long_short": Q5 - Q1 (多空收益),
            "monotonic": 是否单调 (bool),
        }
    """
    result = {}

    # 对每个日期分组
    daily_qret = []
    for dt, g in df.groupby("trade_date"):
        if len(g) < n_quantiles * 5:  # 样本太少跳过
            continue
        try:
            g = g.copy()
            g["quantile"] = pd.qcut(g[factor_col], n_quantiles, labels=False, duplicates="drop")
            qret = g.groupby("quantile")[forward_col].mean()
            daily_qret.append(qret)
        except Exception:
            continue

    if not daily_qret:
        return {"q1_ret": np.nan, "q5_ret": np.nan, "long_short": np.nan, "monotonic": False}

    avg_qret = pd.DataFrame(daily_qret).mean()

    q1_ret = avg_qret.iloc[0] if len(avg_qret) > 0 else np.nan
    q5_ret = avg_qret.iloc[-1] if len(avg_qret) > 0 else np.nan
    long_short = q5_ret - q1_ret if not (pd.isna(q1_ret) or pd.isna(q5_ret)) else np.nan

    # 检查单调性
    monotonic = True
    if len(avg_qret) >= 3:
        for i in range(1, len(avg_qret)):
            if avg_qret.iloc[i] < avg_qret.iloc[i - 1] - 0.001:  # 容忍小偏差
                monotonic = False
                break

    return {
        "q1_ret": q1_ret,
        "q5_ret": q5_ret,
        "long_short": long_short,
        "monotonic": monotonic,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 因子相关性分析
# ═══════════════════════════════════════════════════════════════════════════

def compute_factor_correlation(df: pd.DataFrame, factor_cols: list) -> pd.DataFrame:
    """计算因子间的截面相关性矩阵 (取时间平均)"""
    # 对每个日期计算相关矩阵，然后取平均
    daily_corrs = []
    for dt, g in df.groupby("trade_date"):
        if len(g) > 50:
            corr = g[factor_cols].corr()
            daily_corrs.append(corr)

    if not daily_corrs:
        return pd.DataFrame()

    avg_corr = pd.DataFrame(
        np.mean([c.values for c in daily_corrs], axis=0),
        index=factor_cols,
        columns=factor_cols,
    )
    return avg_corr


def find_redundant_factors(corr_matrix: pd.DataFrame, threshold: float = 0.8) -> list:
    """
    找出冗余因子 (相关性 > threshold)

    策略: 保留 IC 更高的因子，删除与其高度相关的其他因子
    """
    redundant = []
    processed = set()

    cols = corr_matrix.columns.tolist()

    for i, col1 in enumerate(cols):
        if col1 in processed:
            continue
        for col2 in cols[i + 1:]:
            if col2 in processed:
                continue
            corr = abs(corr_matrix.loc[col1, col2])
            if corr > threshold:
                # 标记 col2 为冗余 (假设 col1 排在前面是因为 IC 更高)
                redundant.append({
                    "redundant": col2,
                    "correlated_with": col1,
                    "correlation": corr,
                })
                processed.add(col2)

    return redundant


# ═══════════════════════════════════════════════════════════════════════════
# 因子衰减分析
# ═══════════════════════════════════════════════════════════════════════════

def compute_factor_decay(
    df: pd.DataFrame,
    factor_col: str,
    windows: list = [1, 3, 5, 10, 20],
) -> dict:
    """
    计算因子在不同预测窗口的 IC 衰减

    好的因子: 短期 IC 高，且衰减缓慢 (长期仍有预测力)
    差的因子: 短期 IC 高，但快速衰减到 0 (可能是噪声)
    """
    decay = {}
    for w in windows:
        fwd_col = f"fwd_ret_{w}d"
        if fwd_col not in df.columns:
            continue
        ic_series = compute_ic_series(df, factor_col, fwd_col)
        decay[f"ic_{w}d"] = ic_series.mean() if len(ic_series) > 0 else np.nan

    return decay


# ═══════════════════════════════════════════════════════════════════════════
# 向量化截面 IC（批量加速）+ L2 专属指标
# ═══════════════════════════════════════════════════════════════════════════

def compute_ic_series_vectorized(
    df: pd.DataFrame,
    factor_cols: list,
    forward_col: str = "fwd_ret_3d",
) -> pd.DataFrame:
    """
    向量化截面 rank-IC：按 trade_date groupby，一次性算所有因子的 spearman。

    比逐因子 groupby 快 10x+。返回 DataFrame: index=trade_date, columns=factor_cols。
    """
    # 截面 rank（按日期分组），消除异常值
    # groupby().rank() 会把分组键 trade_date 保留为索引
    ranked = df.groupby("trade_date")[factor_cols + [forward_col]].rank(pct=True)
    ranked["trade_date"] = df["trade_date"].values
    # 矩阵化：逐日一次性算所有因子与 target 的相关（避免逐因子循环）
    def _daily_corr(g):
        if len(g) < 10:
            return pd.Series(np.nan, index=factor_cols)
        target = g[forward_col]
        if target.std() < 1e-8:
            return pd.Series(np.nan, index=factor_cols)
        # 中心化后点积 / (std*std*n)
        fc = g[factor_cols].subtract(g[factor_cols].mean())
        tc = target - target.mean()
        denom = fc.std() * target.std() * len(g)
        with np.errstate(divide="ignore", invalid="ignore"):
            corr = fc.mul(tc, axis=0).sum() / denom
        return corr

    ic_df = ranked.groupby("trade_date").apply(_daily_corr)
    return ic_df


def compute_net_ic(ic_series: pd.Series, turnover: float, cost_bps: float = 6.0) -> float:
    """
    净 IC：扣双边手续费后的有效 IC。
    cost_bps=6 (双边千三，佣金万三×2 + 印花税千一卖单≈单边3bp)
    turnover: 因子多空组合日均换手率（0~1）
    """
    gross_ic = ic_series.mean()
    # 换手成本对 IC 的拖累近似 = turnover * cost / |ic|
    cost_drag = turnover * (cost_bps / 10000)
    net = gross_ic - cost_drag
    return net


def estimate_turnover(df: pd.DataFrame, factor_col: str, n_quantiles: int = 5) -> float:
    """
    估算因子多空组合日均换手率（轻量版）。
    用截面 rank 差的绝对值均值近似多空持仓变化——避免逐日 qcut 的 O(天×股票) 循环。
    top/bottom 20% 持仓换手 ≈ 2 × mean(|Δrank|) （经验近似）。
    """
    # 按 trade_date 算截面 rank（pct），再算相邻日 rank 差
    ranks = df.groupby("trade_date")[factor_col].rank(pct=True)
    tmp = pd.DataFrame({"td": df["trade_date"].values, "sym": df["symbol"].values,
                        "r": ranks.values})
    tmp = tmp.sort_values(["sym", "td"])
    tmp["dr"] = tmp.groupby("sym")["r"].diff().abs()
    # 多空两端（top20% + bottom20%）的换手 ≈ rank 变化的 2 倍均值
    mean_dr = tmp["dr"].mean()
    return float(min(mean_dr * 2, 1.0)) if not pd.isna(mean_dr) else 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 主评估流程
# ═══════════════════════════════════════════════════════════════════════════

def prepare_data(
    parquet_path: Path,
    sample_stocks: int = 500,
    date_range: tuple = None,
) -> pd.DataFrame:
    """
    加载数据并计算未来收益

    为了效率，随机抽取部分股票进行评估
    """
    _log(f"加载数据: {parquet_path}")
    df = pd.read_parquet(parquet_path)

    # 日期过滤
    if date_range:
        df = df[(df["trade_date"] >= date_range[0]) & (df["trade_date"] <= date_range[1])]

    _log(f"  原始数据: {len(df):,} 行, {df['symbol'].nunique()} 只股票")

    # 随机抽样股票 (减少计算量)
    all_symbols = df["symbol"].unique()
    if len(all_symbols) > sample_stocks:
        np.random.seed(42)
        sampled = np.random.choice(all_symbols, sample_stocks, replace=False)
        df = df[df["symbol"].isin(sampled)]
        _log(f"  抽样后: {len(df):,} 行, {len(sampled)} 只股票")

    # 计算未来收益
    df = df.sort_values(["symbol", "trade_date"])

    for w in [1, 3, 5, 10, 20]:
        df[f"fwd_ret_{w}d"] = df.groupby("symbol")["close"].transform(
            lambda x: x.shift(-w) / x - 1
        )

    # 过滤无效数据
    df = df.dropna(subset=["fwd_ret_5d"])

    _log(f"  有效数据: {len(df):,} 行")
    return df


# ═══════════════════════════════════════════════════════════════════════════
# L1+L2 数据加载（直读 QuantDB l1_l2_factors 分区，含全部 L2 高频因子列）
# ═══════════════════════════════════════════════════════════════════════════

L2_EXCLUDE_COLS = {
    "symbol", "date", "open", "high", "low", "close", "volume", "amount",
}


def prepare_l2_data(
    sample_stocks: int = 2000,
    date_range: tuple = None,
) -> pd.DataFrame:
    """
    直读 L1+L2 原始分区（data/quantdb/6_ml_datasets/l1_l2_factors/dt=*/data.parquet）。

    L1+L2 分区自带 OHLCV + date 列，无需 join K 线。
    为效率抽样股票（L2 信号截面性强，抽样不影响因子排序）。
    主看 T+3 fwd_ret（超短线定位），同时算 T+1/5/10/20 看衰减。
    """
    if os.path.exists("/app") and not os.environ.get("QUANTMIND_HOST_MODE"):
        l2_root = Path("/app/data/quantdb/6_ml_datasets/l1_l2_factors")
    else:
        l2_root = PROJECT_ROOT / "data" / "quantdb" / "6_ml_datasets" / "l1_l2_factors"

    if not l2_root.exists():
        _log(f"ERROR: L1+L2 分区目录不存在: {l2_root}")
        sys.exit(1)

    partitions = sorted(l2_root.glob("dt=*/data.parquet"))
    _log(f"发现 {len(partitions)} 个 L1+L2 分区: {partitions[0].parent.name} ~ {partitions[-1].parent.name}")

    if date_range:
        partitions = [
            p for p in partitions
            if date_range[0] <= p.parent.name.split("=")[1] <= date_range[1]
        ]
        _log(f"  日期过滤后: {len(partitions)} 分区")

    # 读取所有分区（只取需要的列：标识 + OHLCV + 因子）
    # 先读一个分区拿列名
    sample_cols = pd.read_parquet(partitions[0]).columns.tolist()
    needed = [c for c in sample_cols if c not in L2_EXCLUDE_COLS or c in ("symbol", "date", "close")]
    # 确保 close 在（算 fwd_ret 用）
    for must in ("symbol", "date", "close"):
        if must not in needed:
            needed.append(must)

    _log(f"读取 {len(partitions)} 分区，{len(needed)} 列...")
    dfs = []
    for i, p in enumerate(partitions):
        if (i + 1) % 200 == 0:
            _log(f"  读取进度: {i + 1}/{len(partitions)}")
        try:
            dfs.append(pd.read_parquet(p, columns=needed))
        except Exception as e:
            _log(f"  跳过分区 {p.parent.name}: {e}")
            continue

    df = pd.concat(dfs, ignore_index=True)
    df = df.rename(columns={"date": "trade_date"})
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    _log(f"  原始数据: {len(df):,} 行, {df['symbol'].nunique()} 只股票")

    # 抽样股票
    all_symbols = df["symbol"].unique()
    if len(all_symbols) > sample_stocks:
        np.random.seed(42)
        sampled = np.random.choice(all_symbols, sample_stocks, replace=False)
        df = df[df["symbol"].isin(sampled)]
        _log(f"  抽样后: {len(df):,} 行, {sample_stocks} 只股票")

    # 计算未来收益（超短线主看 T+3，衰减看 T+1/5/10/20）
    df = df.sort_values(["symbol", "trade_date"])
    for w in [1, 3, 5, 10, 20]:
        df[f"fwd_ret_{w}d"] = df.groupby("symbol")["close"].transform(
            lambda x: x.shift(-w) / x - 1
        )

    df = df.dropna(subset=["fwd_ret_3d"])
    _log(f"  有效数据: {len(df):,} 行")
    return df


def prepare_l1_data(
    sample_stocks: int = 500,
    date_range: tuple = None,
) -> pd.DataFrame:
    """直读 QuantDB l1_factors 分区（替代旧 model_features parquet 入口）。

    与 prepare_l2_data 同口径：分区自带 OHLCV，抽样股票后计算未来收益。
    """
    from backend.shared.feature_source import read_factor_source

    start = date_range[0] if date_range else None
    end = date_range[1] if date_range else None
    span = f"（{start}~{end}）" if date_range else ""
    _log(f"加载 QuantDB l1_factors 因子分区{span}...")
    df = read_factor_source("l1_factors", "CN", start=start, end=end)
    _log(f"  原始数据: {len(df):,} 行, {df['symbol'].nunique()} 只股票")

    all_symbols = df["symbol"].unique()
    if len(all_symbols) > sample_stocks:
        np.random.seed(42)
        sampled = np.random.choice(all_symbols, sample_stocks, replace=False)
        df = df[df["symbol"].isin(sampled)]
        _log(f"  抽样后: {len(df):,} 行, {len(sampled)} 只股票")

    df = df.sort_values(["symbol", "trade_date"])
    for w in [1, 3, 5, 10, 20]:
        df[f"fwd_ret_{w}d"] = df.groupby("symbol")["close"].transform(
            lambda x, _w=w: x.shift(-_w) / x - 1
        )

    df = df.dropna(subset=["fwd_ret_5d"])
    _log(f"  有效数据: {len(df):,} 行")
    return df


def get_l2_factor_columns(df: pd.DataFrame) -> list:
    """获取 L2 因子列（排除 OHLCV + 标识 + fwd_ret）"""
    factor_cols = []
    for col in df.columns:
        if col in L2_EXCLUDE_COLS or col.startswith("fwd_ret") or col == "trade_date":
            continue
        if df[col].dtype in ["float64", "float32", "int64"]:
            if df[col].std() > 1e-8:
                factor_cols.append(col)
    return factor_cols


def get_factor_columns(df: pd.DataFrame) -> list:
    """获取所有因子列 (排除基础数据和目标变量)"""
    exclude_prefixes = [
        "symbol", "trade_date", "open", "high", "low", "close",
        "volume", "amount", "adj_factor", "stock_name", "industry",
        "listing_market", "fwd_ret",
    ]

    factor_cols = []
    for col in df.columns:
        if any(col.startswith(p) or col == p for p in exclude_prefixes):
            continue
        if df[col].dtype in ["float64", "float32", "int64"]:
            # 排除常量列
            if df[col].std() > 1e-8:
                factor_cols.append(col)

    return factor_cols


def evaluate_all_factors(
    df: pd.DataFrame,
    factor_cols: list,
    forward_col: str = "fwd_ret_5d",
    top_n: int = None,
    category_filter: str = None,
) -> pd.DataFrame:
    """评估所有因子，返回 IC 统计表"""
    _log(f"评估 {len(factor_cols)} 个因子...")

    results = []
    for i, col in enumerate(factor_cols):
        if (i + 1) % 20 == 0:
            _log(f"  进度: {i + 1}/{len(factor_cols)}")

        # 基本 IC 统计
        ic_series = compute_ic_series(df, col, forward_col)
        stats = compute_ic_stats(ic_series, col)

        # 分组收益
        qret = compute_quantile_returns(df, col, forward_col)
        stats.update(qret)

        # 因子类别
        stats["category"] = get_factor_category(col)

        # Null 率
        stats["null_rate"] = df[col].isna().mean()

        results.append(stats)

    result_df = pd.DataFrame(results)

    # 按 ICIR 排序 (综合考虑 IC 大小和稳定性)
    result_df = result_df.sort_values("icir", ascending=False, na_position="last")

    # 类别过滤
    if category_filter:
        result_df = result_df[result_df["category"] == category_filter]

    # Top N
    if top_n:
        result_df = result_df.head(top_n)

    return result_df


def evaluate_l2_factors(
    df: pd.DataFrame,
    factor_cols: list,
    primary_horizon: int = 3,
) -> pd.DataFrame:
    """
    L2 专属批量评估：向量化截面 IC + 衰减序列 + 净 IC + 换手。

    主看 horizon=3（超短线定位）。返回按 |ICIR| 排序的因子表。
    """
    primary_col = f"fwd_ret_{primary_horizon}d"
    _log(f"L2 批量评估: {len(factor_cols)} 因子, 主 horizon=T+{primary_horizon}")

    # 1. 向量化算主 horizon 的 IC 序列（所有因子一次性）
    _log("  计算向量化截面 IC（主 horizon）...")
    ic_df = compute_ic_series_vectorized(df, factor_cols, primary_col)

    # 先用主 horizon ICIR 排序，取 top-60 算衰减（避免对全 216 因子算 5 个 horizon）
    main_stats_quick = []
    for col in factor_cols:
        s = ic_df[col].dropna()
        if len(s) > 5 and s.std() > 1e-8:
            main_stats_quick.append((col, s.mean() / s.std()))
        else:
            main_stats_quick.append((col, 0))
    main_stats_quick.sort(key=lambda x: abs(x[1]), reverse=True)
    top_for_decay = [c for c, _ in main_stats_quick[:60]]
    _log(f"  衰减序列只算 top-{len(top_for_decay)} 因子（按主 horizon |ICIR|）")

    # 2. 衰减序列（各 horizon 的均值 IC）——只对 top 因子
    decay_ic = {}
    for w in [1, 3, 5, 10, 20]:
        fw = f"fwd_ret_{w}d"
        if fw not in df.columns:
            continue
        if w == primary_horizon:
            # 主 horizon 已算过，复用
            decay_ic[f"ic_{w}d"] = ic_df.mean()
            continue
        _log(f"  衰减 IC T+{w} (top-{len(top_for_decay)})...")
        ic_w = compute_ic_series_vectorized(df, top_for_decay, fw)
        decay_ic[f"ic_{w}d"] = ic_w.mean()

    # 3. 逐因子汇总（quantile_returns 只对 top-50 算，避免全 216 因子逐日 qcut）
    _log("  汇总指标...")
    # top-50 by |ICIR| for expensive quantile analysis
    icir_order = main_stats_quick  # already sorted by |ICIR| desc
    top50_for_quantile = {c for c, _ in icir_order[:50]}

    results = []
    for i, col in enumerate(factor_cols):
        if (i + 1) % 50 == 0:
            _log(f"    进度: {i + 1}/{len(factor_cols)}")
        ic_series = ic_df[col].dropna()
        stats = compute_ic_stats(ic_series, col)

        # IC 序列 t 值（稳定性）
        if len(ic_series) > 5 and ic_series.std() > 1e-8:
            stats["ic_tstat"] = ic_series.mean() / (ic_series.std() / np.sqrt(len(ic_series)))
        else:
            stats["ic_tstat"] = np.nan

        # 衰减
        for w in [1, 3, 5, 10, 20]:
            stats[f"ic_{w}d"] = decay_ic.get(f"ic_{w}d", {}).get(col, np.nan)

        # 分组收益（单调性）——只对 top-50 算（逐日 qcut 太重）
        if col in top50_for_quantile:
            qret = compute_quantile_returns(df, col, primary_col)
            stats.update(qret)
        else:
            stats.update({"q1_ret": np.nan, "q5_ret": np.nan,
                          "long_short": np.nan, "monotonic": False})

        # 换手 + 净 IC（轻量版）
        turnover = estimate_turnover(df, col)
        stats["turnover"] = turnover
        stats["net_ic"] = compute_net_ic(ic_series, turnover)

        stats["category"] = get_factor_category(col)
        stats["null_rate"] = df[col].isna().mean()
        results.append(stats)

    result_df = pd.DataFrame(results)
    result_df = result_df.sort_values("icir", ascending=False, na_position="last")
    return result_df, ic_df


def generate_recommendations(
    ic_stats: pd.DataFrame,
    corr_matrix: pd.DataFrame = None,
    ic_threshold: float = 0.02,
    icir_threshold: float = 0.3,
    corr_threshold: float = 0.8,
) -> dict:
    """
    生成因子推荐列表

    筛选条件:
    1. |IC| > ic_threshold (至少 2% 的相关性)
    2. |ICIR| > icir_threshold (IC 稳定)
    3. 分组收益单调
    4. 去除冗余因子 (相关性 < corr_threshold)
    """
    # 基础筛选
    qualified = ic_stats[
        (ic_stats["ic_abs_mean"] > ic_threshold) &
        (ic_stats["icir"].abs() > icir_threshold) &
        (ic_stats["null_rate"] < 0.3)
    ].copy()

    _log(f"基础筛选: {len(qualified)} 个因子通过 IC/ICIR 门槛")

    # 单调性筛选
    monotonic = qualified[qualified["monotonic"] == True]
    _log(f"单调性筛选: {len(monotonic)} 个因子通过")

    # 去冗余
    if corr_matrix is not None and len(monotonic) > 0:
        # 只对通过的因子计算相关性
        common_cols = [c for c in monotonic["factor"] if c in corr_matrix.columns]
        sub_corr = corr_matrix.loc[common_cols, common_cols]
        redundant = find_redundant_factors(sub_corr, corr_threshold)
        redundant_names = {r["redundant"] for r in redundant}

        final = monotonic[~monotonic["factor"].isin(redundant_names)]
        _log(f"去冗余: {len(final)} 个因子 (移除 {len(redundant_names)} 个冗余)")
    else:
        final = monotonic
        redundant = []

    return {
        "recommended": final,
        "redundant": redundant,
        "stats": {
            "total_evaluated": len(ic_stats),
            "passed_ic": len(qualified),
            "passed_monotonic": len(monotonic),
            "final_count": len(final),
        },
    }


def generate_l2_recommendations(
    ic_stats: pd.DataFrame,
    df: pd.DataFrame,
    ic_df: pd.DataFrame = None,
    icir_threshold: float = 0.15,
    ic_positive_ratio_threshold: float = 0.55,
    corr_threshold: float = 0.6,
    top_n: int = 40,
) -> dict:
    """
    L2 专属推荐：阈值比 L1 严（ICIR>0.15, IC>0 占比>55%, 净IC>0, NaN<30%），
    去冗余用 IC 序列相关性（比原始因子值 spearman 快几个数量级，且更有意义——
    去的是"信号冗余"而非"数值冗余"）。最终截断到 top_n。
    ic_df: 主 horizon 的 IC 序列矩阵（index=trade_date, columns=factor），可选。
    """
    # 基础筛选
    qualified = ic_stats[
        (ic_stats["icir"].abs() > icir_threshold) &
        (ic_stats["ic_positive_ratio"] > ic_positive_ratio_threshold) &
        (ic_stats["net_ic"] > 0) &
        (ic_stats["null_rate"] < 0.3)
    ].copy()
    _log(f"L2 基础筛选: {len(qualified)}/{len(ic_stats)} 通过 ICIR/IC占比/净IC/NaN 门槛")

    # 按 |ICIR| 排序后去冗余
    qualified = qualified.reindex(
        qualified["icir"].abs().sort_values(ascending=False).index
    )
    factor_list = qualified["factor"].tolist()

    # 用 IC 序列相关性去冗余（快 + 有意义）
    final_names = []
    redundant = []
    if len(factor_list) > 1 and ic_df is not None:
        avail = [f for f in factor_list if f in ic_df.columns]
        _log(f"  计算 IC 序列 corr ({len(avail)}×{len(avail)})...")
        cand_corr = ic_df[avail].corr()  # IC 序列间 pearson，快
        _log(f"  贪心去冗余 (corr<{corr_threshold})...")
        factor_to_idx = {f: i for i, f in enumerate(avail)}
        selected = []
        remaining = list(range(len(avail)))
        while remaining and len(selected) < top_n:
            cur_idx = remaining.pop(0)
            selected.append(cur_idx)
            to_remove = []
            for r in remaining:
                if abs(cand_corr.iloc[cur_idx, r]) > corr_threshold:
                    to_remove.append(r)
                    redundant.append({
                        "redundant": avail[r],
                        "correlated_with": avail[cur_idx],
                        "correlation": abs(cand_corr.iloc[cur_idx, r]),
                    })
            for r in to_remove:
                remaining.remove(r)
        final_names = [avail[i] for i in selected]
    else:
        final_names = factor_list[:top_n]

    final = qualified[qualified["factor"].isin(final_names)].copy()
    _log(f"  最终推荐: {len(final)} 个 (移除 {len(redundant)} 个冗余)")

    return {
        "recommended": final,
        "redundant": redundant,
        "stats": {
            "total_evaluated": len(ic_stats),
            "passed_thresholds": len(qualified),
            "final_count": len(final),
        },
    }


def print_report(ic_stats: pd.DataFrame, recommendations: dict):
    """打印评估报告"""
    print("\n" + "=" * 80)
    print("因子评估报告")
    print("=" * 80)

    # Top 20 因子
    print("\n📊 Top 20 因子 (按 ICIR 排序)")
    print("-" * 80)
    top20 = ic_stats.head(20)
    print(f"{'因子':<30} {'类别':<12} {'IC':>8} {'ICIR':>8} {'IC>0':>8} {'多空':>10} {'单调':>6}")
    print("-" * 80)
    for _, row in top20.iterrows():
        mono = "✓" if row.get("monotonic") else "✗"
        ls = f"{row.get('long_short', 0):.4f}" if not pd.isna(row.get("long_short")) else "N/A"
        print(
            f"{row['factor']:<30} "
            f"{row.get('category', ''):<12} "
            f"{row.get('ic_mean', 0):>8.4f} "
            f"{row.get('icir', 0):>8.3f} "
            f"{row.get('ic_positive_ratio', 0):>7.1%} "
            f"{ls:>10} "
            f"{mono:>6}"
        )

    # 推荐因子
    rec = recommendations["recommended"]
    print(f"\n✅ 推荐因子列表 ({len(rec)} 个)")
    print("-" * 80)
    for cat in rec["category"].unique():
        cat_factors = rec[rec["category"] == cat]["factor"].tolist()
        if cat_factors:
            print(f"  {cat}: {', '.join(cat_factors[:10])}")
            if len(cat_factors) > 10:
                print(f"         ... (+{len(cat_factors) - 10} more)")

    # 冗余因子
    redundant = recommendations["redundant"]
    if redundant:
        print(f"\n⚠️ 冗余因子 ({len(redundant)} 个)")
        print("-" * 80)
        for r in redundant[:10]:
            print(f"  {r['redundant']} ↔ {r['correlated_with']} (corr={r['correlation']:.2f})")
        if len(redundant) > 10:
            print(f"  ... (+{len(redundant) - 10} more)")

    # 统计
    stats = recommendations["stats"]
    print(f"\n📈 筛选统计")
    print("-" * 80)
    print(f"  总因子数:     {stats['total_evaluated']}")
    print(f"  IC 通过:      {stats['passed_ic']}")
    print(f"  单调通过:     {stats['passed_monotonic']}")
    print(f"  最终推荐:     {stats['final_count']}")
    print("=" * 80)


def print_l2_report(ic_stats: pd.DataFrame, recommendations: dict, primary_horizon: int = 3):
    """L2 专属报告：强调超短线衰减 + 净 IC + 换手"""
    print("\n" + "=" * 90)
    print(f"L2 微观结构因子评估报告（超短线 T+{primary_horizon} 定位）")
    print("=" * 90)

    # Top 25 因子（按 |ICIR| 排序，含衰减+换手+净IC）
    print(f"\n📊 Top 25 L2 因子（主 horizon T+{primary_horizon}）")
    print("-" * 110)
    print(f"{'因子':<32} {'类别':<18} {'IC':>7} {'ICIR':>7} {'t值':>6} {'IC>0':>6} "
          f"{'净IC':>7} {'换手':>6} {'T1':>6} {'T3':>6} {'T5':>6} {'T10':>6} {'单调':>4}")
    print("-" * 110)
    top25 = ic_stats.head(25)
    for _, row in top25.iterrows():
        mono = "✓" if row.get("monotonic") else "✗"
        def _f(v, fmt=".4f"):
            return f"{v:{fmt}}" if not pd.isna(v) else "  N/A"
        print(
            f"{row['factor']:<32} "
            f"{row.get('category', ''):<18} "
            f"{_f(row.get('ic_mean', 0), '.4f'):>7} "
            f"{_f(row.get('icir', 0), '.3f'):>7} "
            f"{_f(row.get('ic_tstat', 0), '.2f'):>6} "
            f"{row.get('ic_positive_ratio', 0):>5.0%} "
            f"{_f(row.get('net_ic', 0), '.4f'):>7} "
            f"{_f(row.get('turnover', 0), '.2f'):>6} "
            f"{_f(row.get('ic_1d', 0), '.4f'):>6} "
            f"{_f(row.get('ic_3d', 0), '.4f'):>6} "
            f"{_f(row.get('ic_5d', 0), '.4f'):>6} "
            f"{_f(row.get('ic_10d', 0), '.4f'):>6} "
            f"{mono:>4}"
        )

    # 衰减分析：哪些因子 T+1/T+3 强、T+5/T+10 归零（真超短线）
    print(f"\n⚡ 超短线特征分析（T+1/T+3 强且 T+10 衰减 >50%）")
    print("-" * 90)
    short_term = ic_stats[
        (ic_stats["ic_1d"].abs() > 0.01) &
        (ic_stats["ic_10d"].abs() < ic_stats["ic_1d"].abs() * 0.5)
    ].head(15)
    if len(short_term) > 0:
        for _, row in short_term.iterrows():
            decay_ratio = row["ic_10d"] / row["ic_1d"] if row.get("ic_1d", 0) != 0 else 0
            print(f"  {row['factor']:<32} T1={row['ic_1d']:.4f} T3={row.get('ic_3d',0):.4f} "
                  f"T10={row['ic_10d']:.4f} (衰减 {decay_ratio:.0%})")
    else:
        print("  （无显著超短线衰减因子）")

    # 推荐因子
    rec = recommendations["recommended"]
    print(f"\n✅ 推荐因子列表 ({len(rec)} 个，去冗余后)")
    print("-" * 90)
    for cat in rec["category"].unique():
        cat_factors = rec[rec["category"] == cat]["factor"].tolist()
        if cat_factors:
            print(f"  {cat}: {', '.join(cat_factors[:8])}")
            if len(cat_factors) > 8:
                print(f"         ... (+{len(cat_factors) - 8} more)")

    # 统计
    stats = recommendations["stats"]
    print(f"\n📈 筛选统计")
    print("-" * 90)
    print(f"  总因子数:       {stats['total_evaluated']}")
    print(f"  通过阈值筛选:   {stats['passed_thresholds']}")
    print(f"  最终推荐(去冗余): {stats['final_count']}")
    print("=" * 90)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="因子评估脚本")
    parser.add_argument("--source", choices=["default", "l2"], default="default",
                        help="数据源: default=QuantDB l1_factors 分区, l2=L2原始分区(直读)")
    parser.add_argument("--top", type=int, default=50, help="显示 Top N 因子")
    parser.add_argument("--category", type=str, default=None, help="按类别筛选")
    parser.add_argument("--sample", type=int, default=2000, help="抽样股票数量")
    parser.add_argument("--export", action="store_true", help="导出推荐因子列表")
    parser.add_argument("--corr", action="store_true", help="计算因子相关性 (较慢)")
    parser.add_argument("--ic-threshold", type=float, default=0.02, help="IC 门槛")
    parser.add_argument("--icir-threshold", type=float, default=0.3, help="ICIR 门槛")
    parser.add_argument("--horizon", type=int, default=3, help="L2 主预测窗口(超短线=3)")
    parser.add_argument("--start", type=str, default=None, help="起始日期 YYYYMMDD")
    parser.add_argument("--end", type=str, default=None, help="结束日期 YYYYMMDD")
    args = parser.parse_args()

    date_range = None
    if args.start and args.end:
        date_range = (args.start, args.end)

    if args.source == "l2":
        # ── L2 微观结构因子评估 ──
        df = prepare_l2_data(sample_stocks=args.sample, date_range=date_range)
        factor_cols = get_l2_factor_columns(df)
        _log(f"发现 {len(factor_cols)} 个 L2 因子列")

        ic_stats, ic_df = evaluate_l2_factors(df, factor_cols, primary_horizon=args.horizon)

        recommendations = generate_l2_recommendations(
            ic_stats, df,
            ic_df=ic_df,
            icir_threshold=0.15,
            ic_positive_ratio_threshold=0.55,
            corr_threshold=0.6,
            top_n=40,
        )

        print_l2_report(ic_stats, recommendations, primary_horizon=args.horizon)

        if args.export:
            output_path = OUTPUT_DIR / "l2_factor_eval_report.csv"
            ic_stats.to_csv(output_path, index=False)
            _log(f"L2 因子评估已导出: {output_path}")
            rec_path = OUTPUT_DIR / "l2_recommended_factors.csv"
            recommendations["recommended"].to_csv(rec_path, index=False)
            _log(f"推荐因子已导出: {rec_path}")
        return

    # ── 默认: QuantDB l1_factors 因子评估 ──
    df = prepare_l1_data(sample_stocks=args.sample, date_range=date_range)
    factor_cols = get_factor_columns(df)
    _log(f"发现 {len(factor_cols)} 个因子列")

    ic_stats = evaluate_all_factors(
        df, factor_cols,
        forward_col="fwd_ret_5d",
        top_n=args.top,
        category_filter=args.category,
    )

    corr_matrix = None
    if args.corr and len(ic_stats) > 0:
        _log("计算因子相关性矩阵...")
        top_factors = ic_stats.head(50)["factor"].tolist()
        corr_matrix = compute_factor_correlation(df, top_factors)

    recommendations = generate_recommendations(
        ic_stats, corr_matrix,
        ic_threshold=args.ic_threshold,
        icir_threshold=args.icir_threshold,
    )

    print_report(ic_stats, recommendations)

    if args.export:
        output_path = OUTPUT_DIR / "recommended_factors.csv"
        recommendations["recommended"].to_csv(output_path, index=False)
        _log(f"推荐因子已导出: {output_path}")

        if corr_matrix is not None:
            corr_path = OUTPUT_DIR / "factor_correlation.csv"
            corr_matrix.to_csv(corr_path)
            _log(f"相关性矩阵已导出: {corr_path}")


if __name__ == "__main__":
    main()
