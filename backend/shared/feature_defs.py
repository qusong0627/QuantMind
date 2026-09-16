"""批量特征定义唯一实现（T-P6-07）——自 update_feature_parquet.py 纯迁移，逐字节保真。

背景：同一套 OHLCV 因子口径此前散落多处（批次脚本 / 多市场脚本 / 各推理侧候选实现），
训练-推理-实时三处口径漂移风险；本模块收敛为唯一实现。任何算法修订必须先跑金样回归
（backend/tests/test_feature_defs_parity.py，真实快照逐值哈希）。

输入契约：单标的 DataFrame（open/high/low/close/volume/amount/trade_date/adj_factor 必需——
批量管线由 fetch 预填 adj_factor=1.0；main_flow/total_mv/float_mv/turnover_rate/is_st 等可选），
按 trade_date 升序；
输出：追加 FEATURE_COLS 全列；不足窗口的滚动特征按各函数 min_periods 语义产出 NaN（不假填）。

注意：本文件为**纯迁移**（2026-09-17，T-P6-07），未做任何重构——改核心算法即改口径。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ═══ 以下为 update_feature_parquet.py:462-1174 的逐字节迁移 ═══

# ── 特征计算输入列契约（原 update_feature_parquet.py:62-73 纯迁移）───────────
DB_FUNDAMENTAL_COLS = [
    "pe_ttm", "pb", "roe", "bp", "ep_ttm", "ln_mv_total", "float_mv", "total_mv",
    "industry", "is_st", "listing_market",
]
DB_INDEX_COLS = [
    "idx_all", "idx_hs300", "idx_zz1000", "idx_chinext", "idx_margin",
]
DB_CONCEPT_COLS = [
    "concept_ai", "concept_chip", "concept_new_energy", "concept_pv",
    "concept_military", "concept_medical", "concept_fintech",
    "concept_consumption", "concept_state_owned", "concept_lithium",
]

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9) -> tuple:
    low_n = low.rolling(n, min_periods=1).min()
    high_n = high.rolling(n, min_periods=1).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def _macd(close: pd.Series) -> tuple:
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    hist = dif - dea
    return dif, dea, hist


# All feature columns added by _compute_features_core.
# Used to add NaN columns when all rows are suspended.
FEATURE_COLS = [
    # 动量
    "mom_ret_1d", "mom_ret_5d", "mom_ret_10d", "mom_ret_20d",
    "mom_ma_gap_5", "mom_ma_gap_20", "mom_macd_hist", "mom_rsi_14", "mom_kdj_k",
    "mom_breakout_20d",
    # 波动率
    "vol_std_20", "vol_atr_14", "vol_parkinson_20", "vol_gk_20", "vol_rs_20",
    "vol_downside_20", "vol_realized_rv", "vol_jump_zadj",
    # 流动性
    "liq_volume", "liq_amount", "liq_turnover_os", "liq_volume_ma_20",
    "liq_volume_ratio_5", "liq_amount_ma_20", "liq_amount_ratio_5",
    "liq_mfi_14", "liq_amihud_20", "liq_amihud_60", "liq_accdist_20",
    # 资金流
    "flow_net_amount", "flow_net_amount_ratio", "flow_large_net_amount",
    "flow_vpin", "flow_vpin_ma_5", "flow_vpin_ma_20",
    # 风格
    "style_ln_mv_total", "style_ln_mv_float", "style_beta_20", "style_beta_60",
    "style_idio_vol_20", "style_residual_ret_20",
    # 行业
    "ind_ret_1d", "ind_ret_5d", "ind_ret_10d", "ind_ret_20d",
    "ind_strength_20", "ind_strength_60", "ind_momentum_rank_20",
    "ind_vol_20", "ind_turnover_20", "ind_amount_20",
    "ind_dispersion_20", "ind_up_breadth_20", "ind_down_breadth_20",
    "ind_relative_volume_20", "ind_relative_volatility_20", "ind_relative_flow_20",
    "ind_value_rank", "ind_size_rank",
    # 新增动量
    "mom_ret_3d", "mom_ret_60d", "mom_ret_120d",
    "mom_ma_gap_10", "mom_ma_gap_60", "mom_ma_gap_120",
    "mom_ema_gap_12", "mom_ema_gap_26", "mom_roc_12",
    # 新增波动率
    "vol_std_10", "vol_atr_20", "vol_true_range", "vol_parkinson_10",
    "vol_gk_10", "vol_rs_10", "vol_upside_20", "vol_realized_rrv",
    "vol_realized_rskew", "vol_realized_rkurt", "vol_jump_rjv_ratio", "vol_jump_sjv_ratio",
    # 新增流动性
    "liq_turnover_tl", "liq_volume_ma_5", "liq_volume_ma_10",
    "liq_volume_ratio_20", "liq_amount_ma_5", "liq_amount_ma_10",
    "liq_amount_ratio_20", "liq_obv_20", "liq_obv_60",
    # 新增资金流
    "flow_vpin_delta_5", "flow_net_order_count", "flow_net_order_ratio", "flow_pressure_index",
    # 新增风格
    "style_beta_120", "style_idio_vol_60", "style_bp", "style_ep_ttm",
    # 辅助
    "factor", "pctchange",
    # DB 基本面 (numeric only)
    "pe_ttm", "pb", "roe", "bp", "ep_ttm", "ln_mv_total", "float_mv", "total_mv",
    # 指数成分 / 概念
    "idx_all", "idx_hs300", "idx_zz1000", "idx_chinext", "idx_margin",
    "concept_ai", "concept_chip", "concept_new_energy", "concept_pv",
    "concept_military", "concept_medical", "concept_fintech",
    "concept_consumption", "concept_state_owned", "concept_lithium",
    # DB 技术指标
    "return_1d", "return_5d", "return_20d", "ma5", "ma20", "ma60",
    "rsi_14", "rsi_6", "kdj_k", "kdj_d", "kdj_j",
    "macd_hist", "macd_dif", "macd_dea",
    "ma_gap_5", "ma_gap_20",
    "vol_std_5", "vol_std_60", "vol_atr_14",
    "volume_ratio_5", "volume_ratio_20", "volume_ma_5", "volume_ma_3", "amount_ma_5",
    "turnover_rate", "beta_20",
    "mom_macd_dif", "mom_macd_dea", "mom_rsi_6", "mom_kdj_d", "mom_kdj_j",
    # Alpha158 K线
    "kline_kmid", "kline_klen", "kline_kmid2", "kline_kup", "kline_kup2",
    "kline_klow", "kline_klow2", "kline_ksft", "kline_ksft2",
    # Alpha158 价格相对
    "prel_open0", "prel_high0", "prel_low0", "prel_vwap0",
    # 价格位置
    "price_position_20", "price_position_60",
    "dist_to_high_20", "dist_to_low_20", "ret_rank_20",
    # 波动率调整动量
    "mom_sharpe_5", "mom_sharpe_20", "mom_sharpe_60", "mom_risk_adj_20",
    # 量价配合
    "pv_corr_20", "pv_corr_10", "up_volume_ratio_20", "pv_divergence_20",
    # 趋势质量
    "trend_r2_20", "trend_slope_20", "consecutive_updown_5",
    # 时序滞后
    "ret_1d_lag1", "ret_1d_lag2",
    # ═══ 第六梯队新增因子 ═══
    # 波动率曲面
    "vol_smile_20", "vol_term_structure", "vol_of_vol",
    # 技术形态
    "tech_bollinger_position", "tech_williams_r_14", "tech_cci_20",
    # Alpha101 风格
    "alpha_decay_ret_10", "alpha_corr_cv_20", "alpha_tsrank_ret_20", "alpha_tsrank_volume_20",
    # Alpha360 补充
    "alpha_high_20d_ratio", "alpha_low_20d_ratio", "alpha_close_open_gap",
    # 基本面补充
    "fund_pe_percentile", "fund_pb_percentile",
    # 行业编码 (CatBoost cat_features, 从 instrument_detail 填充)
    "ind_code_l1", "ind_code_l2",
    # 分类列 (keep as-is, no NaN fill needed)
    # "industry", "is_st", "listing_market",
]


def _add_nan_features(g: pd.DataFrame) -> pd.DataFrame:
    """Add all feature columns as NaN (for fully-suspended stocks)."""
    missing = [c for c in FEATURE_COLS if c not in g.columns]
    if missing:
        nan_df = pd.DataFrame(np.nan, index=g.index, columns=missing)
        g = pd.concat([g, nan_df], axis=1)
    # Ensure is_st is int (not string) to avoid mixed-type parquet errors
    if "is_st" in g.columns:
        g["is_st"] = pd.to_numeric(g["is_st"], errors="coerce").fillna(0).astype(int)
    return g


def compute_features_for_group(g: pd.DataFrame) -> pd.DataFrame:
    """为单只股票计算全部特征。输入需按 trade_date 排序。"""
    g = g.sort_values("trade_date").copy()

    # 过滤停牌/零价格行：close<=0 或 volume=0 视为停牌，不参与特征计算
    suspended = (g["close"] <= 0) | (g["volume"] == 0)
    if suspended.any():
        # 停牌行的特征保持 NaN，只对有效行计算
        valid = g[~suspended].copy()
        if len(valid) < 2:
            return _add_nan_features(g)  # 数据太少，特征全部填 NaN
        feat = _compute_features_core(valid)
        result = g.copy()
        # 特征回写前的类型对齐：部分市场以空串填充 is_st/listing_market
        # （str 列），而特征计算会输出数值列，直接回写会触发 arrow-string
        # 类型错误。仅当 feat 侧为数值且 result 侧为字符串时规整。
        for col in set(feat.columns) & set(result.columns):
            if (
                col in ("is_st", "listing_market")
                and pd.api.types.is_numeric_dtype(feat[col])
                and not pd.api.types.is_numeric_dtype(result[col])
            ):
                result[col] = (
                    pd.to_numeric(result[col], errors="coerce").fillna(0).astype(int)
                )
        # Add missing feature columns in one batch to avoid fragmentation
        missing = [c for c in feat.columns if c not in result.columns]
        if missing:
            nan_df = pd.DataFrame(np.nan, index=result.index, columns=missing)
            result = pd.concat([result, nan_df], axis=1)
        for col in feat.columns:
            result.loc[feat.index, col] = feat[col].values
        return result

    return _compute_features_core(g)


def _compute_features_core(g: pd.DataFrame) -> pd.DataFrame:
    """实际特征计算逻辑（假设输入数据无停牌）。"""
    c = g["close"]
    h = g["high"]
    lo = g["low"]
    v = g["volume"]
    amt = g["amount"]
    ret = c.pct_change().clip(lower=-1.0, upper=10.0)  # 限制收益率范围，防止 inf
    ln_c = np.log(c.clip(lower=1e-8))
    log_ret = ln_c.diff()
    # 替换 inf 为 NaN
    ret = ret.replace([np.inf, -np.inf], np.nan)
    log_ret = log_ret.replace([np.inf, -np.inf], np.nan)

    # ═══ 原有 51 个特征 ═══

    # ── 动量 ──
    g["mom_ret_1d"] = ret
    g["mom_ret_5d"] = c.pct_change(5)
    g["mom_ret_10d"] = c.pct_change(10)
    g["mom_ret_20d"] = c.pct_change(20)
    g["mom_ma_gap_5"] = (c / c.rolling(5, min_periods=1).mean()) - 1
    g["mom_ma_gap_20"] = (c / c.rolling(20, min_periods=1).mean()) - 1
    g["mom_macd_hist"] = _macd(c)[2]
    g["mom_rsi_14"] = _rsi(c, 14)
    g["mom_kdj_k"] = _kdj(h, lo, c)[0]
    g["mom_breakout_20d"] = (c / c.rolling(20, min_periods=1).max()) - 1

    # ── 波动率 ──
    g["vol_std_20"] = log_ret.rolling(20, min_periods=5).std()
    tr = pd.concat([h - lo, (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    g["vol_atr_14"] = tr.rolling(14, min_periods=1).mean()
    hl_ratio = np.log(h / lo.clip(lower=1e-8))
    g["vol_parkinson_20"] = np.sqrt((hl_ratio ** 2).rolling(20, min_periods=5).mean() / (4 * np.log(2)))
    g["vol_gk_20"] = np.sqrt(
        (0.5 * (ln_c.diff() ** 2) - (2 * np.log(2) - 1) * (log_ret ** 2))
        .rolling(20, min_periods=5).mean().clip(lower=0)
    )
    g["vol_rs_20"] = np.sqrt((log_ret.clip(lower=0) ** 2).rolling(20, min_periods=5).mean())
    neg_ret = log_ret.clip(upper=0)
    g["vol_downside_20"] = neg_ret.rolling(20, min_periods=5).std()
    g["vol_realized_rv"] = np.sqrt((log_ret ** 2).rolling(20, min_periods=5).mean() * 252)
    rv = log_ret.rolling(20, min_periods=5).std()
    bv = (log_ret.abs() * log_ret.shift().abs()).rolling(20, min_periods=5).mean()
    bv = bv.clip(lower=1e-12)
    g["vol_jump_zadj"] = ((rv ** 2) / bv).clip(upper=10).fillna(0)

    # ── 流动性 ──
    g["liq_volume"] = v
    g["liq_amount"] = amt
    g["liq_turnover_os"] = v / v.rolling(250, min_periods=20).mean().clip(lower=1)
    g["liq_volume_ma_20"] = v.rolling(20, min_periods=1).mean()
    g["liq_volume_ratio_5"] = v / v.rolling(5, min_periods=1).mean().clip(lower=1) - 1
    g["liq_amount_ma_20"] = amt.rolling(20, min_periods=1).mean()
    g["liq_amount_ratio_5"] = amt / amt.rolling(5, min_periods=1).mean().clip(lower=1) - 1
    tp = (h + lo + c) / 3
    mf = tp * v
    pos_mf = mf * (tp > tp.shift()).astype(float)
    neg_mf = mf * (tp <= tp.shift()).astype(float)
    mfr = pos_mf.rolling(14, min_periods=1).sum() / neg_mf.rolling(14, min_periods=1).sum().replace(0, np.nan)
    g["liq_mfi_14"] = 100 - (100 / (1 + mfr))
    abs_ret = ret.abs()
    g["liq_amihud_20"] = (abs_ret / amt.clip(lower=1)).rolling(20, min_periods=1).mean()
    g["liq_amihud_60"] = (abs_ret / amt.clip(lower=1)).rolling(60, min_periods=5).mean()
    clv = ((c - lo) - (h - c)) / (h - lo).replace(0, np.nan)
    clv = clv.fillna(0)
    g["liq_accdist_20"] = (clv * v).rolling(20, min_periods=1).sum()

    # ── 资金流 ──
    direction = np.sign(c.diff())
    g["flow_net_amount"] = (amt * direction).rolling(5, min_periods=1).sum()
    g["flow_net_amount_ratio"] = g["flow_net_amount"] / amt.rolling(20, min_periods=1).sum().clip(lower=1)
    # 大单净流入: 使用 DB 的 main_flow 列，或 fallback 用高金额交易日近似
    if "main_flow" in g.columns:
        g["flow_large_net_amount"] = pd.to_numeric(g["main_flow"], errors="coerce").fillna(0)
    else:
        # Fallback: 大单定义 - 单笔成交额 > 20日均值的 3 倍
        amt_threshold = amt.rolling(20, min_periods=5).mean() * 3
        is_large = amt > amt_threshold
        g["flow_large_net_amount"] = (amt * direction * is_large).rolling(5, min_periods=1).sum()
    buy_vol = v * (c > c.shift()).astype(float)
    sell_vol = v * (c <= c.shift()).astype(float)
    g["flow_vpin"] = (buy_vol - sell_vol).abs().rolling(20, min_periods=5).sum() / v.rolling(20, min_periods=5).sum().clip(lower=1)
    g["flow_vpin_ma_5"] = g["flow_vpin"].rolling(5, min_periods=1).mean()
    g["flow_vpin_ma_20"] = g["flow_vpin"].rolling(20, min_periods=1).mean()

    # ── 风格因子 ──
    # 总市值: 使用 DB 的 total_mv，fallback 用成交额近似
    if "total_mv" in g.columns:
        total_mv = pd.to_numeric(g["total_mv"], errors="coerce")
        g["style_ln_mv_total"] = np.where(
            total_mv.notna() & (total_mv > 0),
            np.log(total_mv.clip(lower=1)),
            np.log(amt.clip(lower=1))
        )
    else:
        g["style_ln_mv_total"] = np.log(amt.clip(lower=1))
    # 流通市值: 使用 DB 的 float_mv，fallback 用 total_mv * 0.9
    if "float_mv" in g.columns:
        float_mv = pd.to_numeric(g["float_mv"], errors="coerce")
        g["style_ln_mv_float"] = np.where(
            float_mv.notna() & (float_mv > 0),
            np.log(float_mv.clip(lower=1)),
            g["style_ln_mv_total"] * 0.9
        )
    else:
        g["style_ln_mv_float"] = g["style_ln_mv_total"] * 0.9
    # beta = cov(ret, market) / var(market); proxy: rolling mean / rolling std
    ret_ma20 = ret.rolling(20, min_periods=5).mean()
    ret_std20 = ret.rolling(20, min_periods=5).std().clip(lower=1e-12)
    g["style_beta_20"] = ret_ma20 / ret_std20
    ret_ma60 = ret.rolling(60, min_periods=10).mean()
    ret_std60 = ret.rolling(60, min_periods=10).std().clip(lower=1e-12)
    g["style_beta_60"] = ret_ma60 / ret_std60
    g["style_idio_vol_20"] = log_ret.rolling(20, min_periods=5).std()
    g["style_residual_ret_20"] = ret.rolling(20, min_periods=5).mean()

    # ── 行业因子（占位，实际计算在 compute_all_features 中跨股票聚合）──
    g["ind_ret_1d"] = np.nan
    g["ind_ret_20d"] = np.nan
    g["ind_strength_20"] = np.nan
    g["ind_momentum_rank_20"] = np.nan

    # ═══ 新增动量特征（纯 OHLCV 计算，不依赖后续变量） ═══
    g["mom_ret_3d"] = c.pct_change(3)
    g["mom_ret_60d"] = c.pct_change(60)
    g["mom_ret_120d"] = c.pct_change(120)
    g["mom_ma_gap_10"] = (c / c.rolling(10, min_periods=1).mean()) - 1
    g["mom_ma_gap_60"] = (c / c.rolling(60, min_periods=1).mean()) - 1
    g["mom_ma_gap_120"] = (c / c.rolling(120, min_periods=1).mean()) - 1
    ema12 = _ema(c, 12)
    ema26 = _ema(c, 26)
    g["mom_ema_gap_12"] = (c / ema12) - 1
    g["mom_ema_gap_26"] = (c / ema26) - 1
    g["mom_roc_12"] = c.pct_change(12)

    # ═══ 新增波动率特征 ═══
    g["vol_std_10"] = log_ret.rolling(10, min_periods=3).std()
    g["vol_atr_20"] = tr.rolling(20, min_periods=1).mean()
    g["vol_true_range"] = tr  # raw true range
    g["vol_parkinson_10"] = np.sqrt((hl_ratio ** 2).rolling(10, min_periods=3).mean() / (4 * np.log(2)))
    g["vol_gk_10"] = np.sqrt(
        (0.5 * (ln_c.diff() ** 2) - (2 * np.log(2) - 1) * (log_ret ** 2))
        .rolling(10, min_periods=3).mean().clip(lower=0)
    )
    g["vol_rs_10"] = np.sqrt((log_ret.clip(lower=0) ** 2).rolling(10, min_periods=3).mean())
    pos_ret = log_ret.clip(lower=0)
    g["vol_upside_20"] = pos_ret.rolling(20, min_periods=5).std()
    # realized relative vol (rv / bv ratio)
    rv_20 = log_ret.rolling(20, min_periods=5).std()
    bv_20 = (log_ret.abs() * log_ret.shift().abs()).rolling(20, min_periods=5).mean().clip(lower=1e-12)
    g["vol_realized_rrv"] = (rv_20 / bv_20).clip(upper=10).fillna(0)
    # realized skewness & kurtosis (vectorized approximation)
    lr_mean = log_ret.rolling(20, min_periods=10).mean()
    lr_std = log_ret.rolling(20, min_periods=10).std().clip(lower=1e-12)
    lr_z = (log_ret - lr_mean) / lr_std
    g["vol_realized_rskew"] = (lr_z ** 3).rolling(20, min_periods=10).mean()
    g["vol_realized_rkurt"] = (lr_z ** 4).rolling(20, min_periods=10).mean() - 3
    # jump ratios
    rv_sq = rv_20 ** 2
    bv_val = bv_20
    g["vol_jump_rjv_ratio"] = (rv_sq / bv_val).clip(upper=10).fillna(0)
    bipower_var = (log_ret.abs() * log_ret.shift().abs()).rolling(20, min_periods=5).mean()
    jump_var = (rv_sq - bipower_var).clip(lower=0)
    g["vol_jump_sjv_ratio"] = (jump_var / rv_sq.replace(0, np.nan)).fillna(0).clip(upper=1)

    # ═══ 新增流动性特征 ═══
    # turnover_rate: 换手率 = volume / circulating_capital，QuantDB 不直接提供
    if "turnover_rate" in g.columns and g["turnover_rate"].notna().any():
        g["turnover_rate"] = pd.to_numeric(g["turnover_rate"], errors="coerce").fillna(
            v / v.rolling(250, min_periods=20).mean().clip(lower=1)
        )
    else:
        # Fallback: 用 volume / 250日均量 近似换手率
        g["turnover_rate"] = v / v.rolling(250, min_periods=20).mean().clip(lower=1)
    g["liq_turnover_tl"] = g["turnover_rate"]  # alias
    g["liq_volume_ma_5"] = v.rolling(5, min_periods=1).mean()
    g["liq_volume_ma_10"] = v.rolling(10, min_periods=1).mean()
    g["liq_volume_ratio_20"] = v / v.rolling(20, min_periods=1).mean().clip(lower=1) - 1
    g["liq_amount_ma_5"] = amt.rolling(5, min_periods=1).mean()
    g["liq_amount_ma_10"] = amt.rolling(10, min_periods=1).mean()
    g["liq_amount_ratio_20"] = amt / amt.rolling(20, min_periods=1).mean().clip(lower=1) - 1
    # OBV (On-Balance Volume)
    obv_direction = np.sign(c.diff())
    obv_raw = (v * obv_direction).cumsum()
    g["liq_obv_20"] = obv_raw - obv_raw.rolling(20, min_periods=1).mean()
    g["liq_obv_60"] = obv_raw - obv_raw.rolling(60, min_periods=1).mean()

    # ═══ 新增资金流特征 ═══
    g["flow_vpin_delta_5"] = g["flow_vpin"].diff(5)
    # approximate order count from volume pattern
    g["flow_net_order_count"] = (v * direction).rolling(5, min_periods=1).sum()
    g["flow_net_order_ratio"] = g["flow_net_order_count"] / v.rolling(20, min_periods=1).sum().clip(lower=1)
    # pressure index: cumulative money flow direction
    g["flow_pressure_index"] = (amt * direction).rolling(20, min_periods=1).sum() / amt.rolling(20, min_periods=1).sum().clip(lower=1)

    # ═══ 新增风格因子 ═══
    ret_ma120 = ret.rolling(120, min_periods=20).mean()
    ret_std120 = ret.rolling(120, min_periods=20).std().clip(lower=1e-12)
    g["style_beta_120"] = ret_ma120 / ret_std120
    g["style_idio_vol_60"] = log_ret.rolling(60, min_periods=10).std()
    # style_bp: 账面市值比，优先用 bp，fallback 用 1/pb
    if "bp" in g.columns:
        bp_val = pd.to_numeric(g["bp"], errors="coerce")
        pb_val = pd.to_numeric(g["pb"], errors="coerce") if "pb" in g.columns else pd.Series(np.nan, index=g.index)
        g["style_bp"] = bp_val.fillna(1.0 / pb_val.replace(0, np.nan))
    else:
        pb_val = pd.to_numeric(g["pb"], errors="coerce") if "pb" in g.columns else pd.Series(np.nan, index=g.index)
        g["style_bp"] = 1.0 / pb_val.replace(0, np.nan)
    # style_ep_ttm: 盈利收益率，优先用 ep_ttm，fallback 用 1/pe_ttm
    if "ep_ttm" in g.columns:
        ep_val = pd.to_numeric(g["ep_ttm"], errors="coerce")
        pe_val = pd.to_numeric(g["pe_ttm"], errors="coerce") if "pe_ttm" in g.columns else pd.Series(np.nan, index=g.index)
        g["style_ep_ttm"] = ep_val.fillna(1.0 / pe_val.replace(0, np.nan))
    else:
        pe_val = pd.to_numeric(g["pe_ttm"], errors="coerce") if "pe_ttm" in g.columns else pd.Series(np.nan, index=g.index)
        g["style_ep_ttm"] = 1.0 / pe_val.replace(0, np.nan)

    # ── 辅助列 ──
    g["factor"] = g["adj_factor"]
    g["pctchange"] = ret

    # ═══ 新增特征：从 DB 直接使用（已有值保留，NULL 填 0） ═══

    # 基本面
    for col in DB_FUNDAMENTAL_COLS:
        if col in g.columns:
            if col in ("industry", "is_st", "listing_market"):
                # 分类列保持原样
                pass
            else:
                g[col] = pd.to_numeric(g[col], errors="coerce").fillna(0)
        else:
            g[col] = 0

    # 指数成分 / 概念标签（0/1 标记）
    for col in DB_INDEX_COLS + DB_CONCEPT_COLS:
        if col in g.columns:
            g[col] = pd.to_numeric(g[col], errors="coerce").fillna(0).astype(int)
        else:
            g[col] = 0

    # 技术指标（DB 已计算的，优先用 DB 值，NULL 用 OHLCV 重算）
    if "return_1d" in g.columns:
        g["return_1d"] = pd.to_numeric(g["return_1d"], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(ret)
    else:
        g["return_1d"] = ret

    if "return_5d" in g.columns:
        g["return_5d"] = pd.to_numeric(g["return_5d"], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(c.pct_change(5).clip(lower=-1.0, upper=10.0).replace([np.inf, -np.inf], np.nan))
    else:
        g["return_5d"] = c.pct_change(5).clip(lower=-1.0, upper=10.0).replace([np.inf, -np.inf], np.nan)

    if "return_20d" in g.columns:
        g["return_20d"] = pd.to_numeric(g["return_20d"], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(c.pct_change(20).clip(lower=-1.0, upper=10.0).replace([np.inf, -np.inf], np.nan))
    else:
        g["return_20d"] = c.pct_change(20).clip(lower=-1.0, upper=10.0).replace([np.inf, -np.inf], np.nan)

    # 移动平均
    if "ma5" not in g.columns or g["ma5"].isna().all():
        g["ma5"] = c.rolling(5, min_periods=1).mean()
    else:
        g["ma5"] = pd.to_numeric(g["ma5"], errors="coerce").fillna(c.rolling(5, min_periods=1).mean())

    if "ma20" not in g.columns or g["ma20"].isna().all():
        g["ma20"] = c.rolling(20, min_periods=1).mean()
    else:
        g["ma20"] = pd.to_numeric(g["ma20"], errors="coerce").fillna(c.rolling(20, min_periods=1).mean())

    if "ma60" not in g.columns or g["ma60"].isna().all():
        g["ma60"] = c.rolling(60, min_periods=1).mean()
    else:
        g["ma60"] = pd.to_numeric(g["ma60"], errors="coerce").fillna(c.rolling(60, min_periods=1).mean())

    # 均线偏离
    g["ma_gap_5"] = (c / g["ma5"]) - 1
    g["ma_gap_20"] = (c / g["ma20"]) - 1

    # RSI
    if "rsi_14" not in g.columns or g["rsi_14"].isna().all():
        g["rsi_14"] = _rsi(c, 14)
    else:
        g["rsi_14"] = pd.to_numeric(g["rsi_14"], errors="coerce").fillna(_rsi(c, 14))

    g["rsi_6"] = _rsi(c, 6)

    # KDJ
    kdj_k, kdj_d, kdj_j = _kdj(h, lo, c)
    if "kdj_k" not in g.columns or g["kdj_k"].isna().all():
        g["kdj_k"] = kdj_k
    else:
        g["kdj_k"] = pd.to_numeric(g["kdj_k"], errors="coerce").fillna(kdj_k)
    g["kdj_d"] = kdj_d
    g["kdj_j"] = kdj_j

    # MACD
    macd_dif, macd_dea, macd_hist = _macd(c)
    if "macd_hist" not in g.columns or g["macd_hist"].isna().all():
        g["macd_hist"] = macd_hist
    else:
        g["macd_hist"] = pd.to_numeric(g["macd_hist"], errors="coerce").fillna(macd_hist)
    g["macd_dif"] = macd_dif
    g["macd_dea"] = macd_dea

    # 动量别名（catalog key 匹配）
    g["mom_macd_dif"] = macd_dif
    g["mom_macd_dea"] = macd_dea
    g["mom_rsi_6"] = g["rsi_6"]
    g["mom_kdj_d"] = kdj_d
    g["mom_kdj_j"] = kdj_j

    # 波动率补充
    g["vol_std_5"] = log_ret.rolling(5, min_periods=2).std()
    g["vol_std_60"] = log_ret.rolling(60, min_periods=10).std()
    if "vol_std_20" in g.columns:
        g["vol_std_20"] = pd.to_numeric(g["vol_std_20"], errors="coerce").fillna(log_ret.rolling(20, min_periods=5).std())
    if "vol_atr_14" in g.columns:
        g["vol_atr_14"] = pd.to_numeric(g["vol_atr_14"], errors="coerce").fillna(tr.rolling(14, min_periods=1).mean())

    # 成交量比率
    g["volume_ratio_5"] = v / v.rolling(5, min_periods=1).mean().clip(lower=1)
    g["volume_ratio_20"] = v / v.rolling(20, min_periods=1).mean().clip(lower=1)
    g["volume_ma_5"] = v.rolling(5, min_periods=1).mean()
    g["volume_ma_3"] = v.rolling(3, min_periods=1).mean()
    g["amount_ma_5"] = amt.rolling(5, min_periods=1).mean()

    # 换手率
    if "turnover_rate" in g.columns:
        g["turnover_rate"] = pd.to_numeric(g["turnover_rate"], errors="coerce").fillna(
            v / v.rolling(250, min_periods=20).mean().clip(lower=1)
        )
    else:
        g["turnover_rate"] = v / v.rolling(250, min_periods=20).mean().clip(lower=1)

    # Beta
    if "beta_20" in g.columns:
        g["beta_20"] = pd.to_numeric(g["beta_20"], errors="coerce").fillna(
            ret_ma20 / ret_std20
        )

    # 市值相关
    if "ln_mv_total" in g.columns:
        g["ln_mv_total"] = pd.to_numeric(g["ln_mv_total"], errors="coerce").fillna(np.log(amt.clip(lower=1)))

    # bp / ep_ttm
    if "bp" in g.columns:
        g["bp"] = pd.to_numeric(g["bp"], errors="coerce").fillna(0)
    if "ep_ttm" in g.columns:
        g["ep_ttm"] = pd.to_numeric(g["ep_ttm"], errors="coerce").fillna(0)

    # is_st
    if "is_st" in g.columns:
        g["is_st"] = pd.to_numeric(g["is_st"], errors="coerce").fillna(0).astype(int)

    # ═══ Alpha158 K 线形态因子 (9 个) ═══
    # 来源: Qlib Alpha158 — 仅需 OHLCV，无窗口依赖
    o = g["open"]
    denom = (h - lo).replace(0, np.nan)
    g["kline_kmid"] = (c - o) / o.clip(lower=1e-8)                              # 实体比
    g["kline_klen"] = (h - lo) / o.clip(lower=1e-8)                             # 振幅比
    g["kline_kmid2"] = (c - o) / denom                                          # 实体占振幅比
    g["kline_kup"] = (h - pd.concat([o, c], axis=1).max(axis=1)) / o.clip(lower=1e-8)   # 上影线比
    g["kline_kup2"] = (h - pd.concat([o, c], axis=1).max(axis=1)) / denom              # 上影线占振幅比
    g["kline_klow"] = (pd.concat([o, c], axis=1).min(axis=1) - lo) / o.clip(lower=1e-8) # 下影线比
    g["kline_klow2"] = (pd.concat([o, c], axis=1).min(axis=1) - lo) / denom             # 下影线占振幅比
    g["kline_ksft"] = (2 * c - h - lo) / o.clip(lower=1e-8)                     # 重心偏移
    g["kline_ksft2"] = (2 * c - h - lo) / denom                                  # 重心偏移归一化

    # ═══ Alpha158 价格相对因子 (4 个) ═══
    g["prel_open0"] = o / c.clip(lower=1e-8)                                    # 开盘/收盘
    g["prel_high0"] = h / c.clip(lower=1e-8)                                    # 最高/收盘
    g["prel_low0"] = lo / c.clip(lower=1e-8)                                    # 最低/收盘
    vwap = amt / v.clip(lower=1)                                                 # VWAP = 成交额/成交量
    vwap = vwap.replace([np.inf, -np.inf], np.nan).fillna((h + lo + c) / 3)     # fallback: 典型价格
    g["prel_vwap0"] = vwap / c.clip(lower=1e-8)                                 # VWAP/收盘

    # ═══ 第一梯队: 价格位置因子 (5 个) ═══
    # 价格在N日区间的位置 (0=最低, 1=最高)
    low_20 = lo.rolling(20, min_periods=1).min()
    high_20 = h.rolling(20, min_periods=1).max()
    g["price_position_20"] = (c - low_20) / (high_20 - low_20).clip(lower=1e-8)

    low_60 = lo.rolling(60, min_periods=1).min()
    high_60 = h.rolling(60, min_periods=1).max()
    g["price_position_60"] = (c - low_60) / (high_60 - low_60).clip(lower=1e-8)

    # 距离N日新高的回撤幅度
    g["dist_to_high_20"] = c / high_20.clip(lower=1e-8) - 1
    g["dist_to_low_20"] = c / low_20.clip(lower=1e-8) - 1

    # 20日内收益率排名 (0~1) — 向量化近似
    ret_1d = c.pct_change()
    ret_min_20 = ret_1d.rolling(20, min_periods=5).min()
    ret_max_20 = ret_1d.rolling(20, min_periods=5).max()
    g["ret_rank_20"] = ((ret_1d - ret_min_20) / (ret_max_20 - ret_min_20).clip(lower=1e-8)).replace([np.inf, -np.inf], np.nan)

    # ═══ 第二梯队: 波动率调整动量 (4 个) ═══
    # Sharpe型动量 = 收益 / 波动率
    ret_std_5 = ret_1d.rolling(5, min_periods=2).std()
    ret_std_20 = ret_1d.rolling(20, min_periods=5).std()
    ret_std_60 = ret_1d.rolling(60, min_periods=10).std()
    g["mom_sharpe_5"] = c.pct_change(5) / ret_std_5.clip(lower=1e-6)
    g["mom_sharpe_20"] = c.pct_change(20) / ret_std_20.clip(lower=1e-6)
    g["mom_sharpe_60"] = c.pct_change(60) / ret_std_60.clip(lower=1e-6)

    # 风险调整后的相对强度
    ret_20 = c.pct_change(20)
    g["mom_risk_adj_20"] = ((ret_20 - ret_20.rolling(20, min_periods=5).mean()) / ret_std_20.clip(lower=1e-6)).replace([np.inf, -np.inf], np.nan)

    # ═══ 第三梯队: 量价配合度 (4 个) ═══
    log_vol = np.log(v.clip(lower=1))

    # 量价相关性 (正=量价齐升)
    g["pv_corr_20"] = ret_1d.rolling(20, min_periods=10).corr(log_vol).clip(-1, 1).fillna(0)
    g["pv_corr_10"] = ret_1d.rolling(10, min_periods=5).corr(log_vol).clip(-1, 1).fillna(0)

    # 放量上涨占比 (20日里上涨日成交量占总量比)
    up_vol = pd.Series(np.where(ret_1d > 0, v, 0), index=g.index)
    g["up_volume_ratio_20"] = up_vol.rolling(20, min_periods=5).sum() / v.rolling(20, min_periods=5).sum().clip(lower=1e-6)

    # 量价背离 (价格排名 - 成交量排名) — 向量化近似
    c_min_20 = c.rolling(20, min_periods=5).min()
    c_max_20 = c.rolling(20, min_periods=5).max()
    v_min_20 = v.rolling(20, min_periods=5).min()
    v_max_20 = v.rolling(20, min_periods=5).max()
    c_rank = (c - c_min_20) / (c_max_20 - c_min_20).clip(lower=1e-8)
    v_rank = (v - v_min_20) / (v_max_20 - v_min_20).clip(lower=1e-8)
    g["pv_divergence_20"] = c_rank - v_rank

    # ═══ 第四梯队: 趋势质量因子 (3 个) ═══
    # 20日趋势R² — 向量化: R² = corr(price, time_index)²
    time_idx = pd.Series(np.arange(len(c), dtype=float), index=c.index)
    g["trend_r2_20"] = (c.rolling(20, min_periods=10).corr(time_idx) ** 2).clip(upper=1).fillna(0)

    # 20日趋势斜率 — 向量化: slope = corr * std(price) / std(t) / mean(price)
    c_std_20 = c.rolling(20, min_periods=10).std()
    t_std = np.sqrt((np.arange(20) - np.arange(20).mean()) ** 2).sum() / 20
    corr_ct = c.rolling(20, min_periods=10).corr(time_idx)
    g["trend_slope_20"] = corr_ct * c_std_20 / (t_std + 1e-6) / c.rolling(20, min_periods=10).mean().clip(lower=1e-6)

    # 连续上涨/下跌强度 (5日涨跌天数差)
    up_down = pd.Series(np.where(ret_1d > 0, 1, np.where(ret_1d < 0, -1, 0)), index=g.index)
    g["consecutive_updown_5"] = up_down.rolling(5, min_periods=1).sum()

    # ═══ 第五梯队: 时序滞后特征 (2 个) ═══
    g["ret_1d_lag1"] = ret_1d.shift(1)
    g["ret_1d_lag2"] = ret_1d.shift(2)

    # ═══ 第六梯队: 新增高价值因子 ═══

    # -- 波动率曲面 --
    # vol_smile_20: 波动率微笑 (上行波动 / 下行波动)
    g["vol_smile_20"] = pos_ret.rolling(20, min_periods=5).std() / neg_ret.rolling(20, min_periods=5).std().replace(0, np.nan)
    g["vol_smile_20"] = g["vol_smile_20"].fillna(1.0)  # 对称时 = 1

    # vol_term_structure: 波动率期限结构 (60日波动 / 20日波动)
    vol_60 = log_ret.rolling(60, min_periods=10).std()
    vol_20 = log_ret.rolling(20, min_periods=5).std()
    g["vol_term_structure"] = vol_60 / vol_20.replace(0, np.nan)
    g["vol_term_structure"] = g["vol_term_structure"].fillna(1.0)

    # vol_of_vol: 波动率的波动率
    daily_vol = log_ret.rolling(5, min_periods=2).std()
    g["vol_of_vol"] = daily_vol.rolling(20, min_periods=5).std()

    # -- 技术形态因子 --
    # bollinger_position: 布林带位置 (close - mid) / (upper - lower)
    bb_mid = c.rolling(20, min_periods=5).mean()
    bb_std = c.rolling(20, min_periods=5).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    g["tech_bollinger_position"] = (c - bb_mid) / (bb_upper - bb_lower).replace(0, np.nan)

    # williams_r_14: Williams %R = (High14 - Close) / (High14 - Low14) * -100
    high_14 = h.rolling(14, min_periods=1).max()
    low_14 = lo.rolling(14, min_periods=1).min()
    g["tech_williams_r_14"] = (high_14 - c) / (high_14 - low_14).replace(0, np.nan) * -100

    # cci_20: 商品通道指标 = (TP - SMA(TP,20)) / (0.015 * MeanDev(TP,20))
    tp_cci = (h + lo + c) / 3
    tp_sma = tp_cci.rolling(20, min_periods=5).mean()
    tp_mad = tp_cci.rolling(20, min_periods=5).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    g["tech_cci_20"] = (tp_cci - tp_sma) / (0.015 * tp_mad).replace(0, np.nan)

    # -- Alpha101 风格因子 --
    # alpha_decay_ret_10: 10日收益率线性衰减加权 (近期权重更大)
    weights_10 = np.arange(10, 0, -1, dtype=float)
    weights_10 = weights_10 / weights_10.sum()
    g["alpha_decay_ret_10"] = ret_1d.rolling(10, min_periods=5).apply(
        lambda x: np.dot(x, weights_10[:len(x)]), raw=True
    )

    # alpha_corr_cv_20: 收盘价与成交量的20日相关性
    log_vol = np.log(v.clip(lower=1))
    g["alpha_corr_cv_20"] = c.rolling(20, min_periods=10).corr(log_vol).clip(-1, 1).fillna(0)

    # alpha_tsrank_ret_20: 20日收益率时序排名 (当前收益在过去20天中的位置)
    g["alpha_tsrank_ret_20"] = ret_1d.rolling(20, min_periods=10).apply(
        lambda x: (x[-1] >= x).mean() if len(x) > 0 else 0.5, raw=True
    )

    # alpha_tsrank_volume_20: 20日成交量时序排名
    g["alpha_tsrank_volume_20"] = v.rolling(20, min_periods=10).apply(
        lambda x: (x[-1] >= x).mean() if len(x) > 0 else 0.5, raw=True
    )

    # -- Alpha360 补充因子 --
    # alpha_high_20d_ratio: 20日内创新高天数占比
    high_20_max = h.rolling(20, min_periods=1).max()
    is_new_high = (h >= high_20_max * 0.995).astype(float)  # 0.5% 容差
    g["alpha_high_20d_ratio"] = is_new_high.rolling(20, min_periods=5).mean()

    # alpha_low_20d_ratio: 20日内创新低天数占比
    low_20_min = lo.rolling(20, min_periods=1).min()
    is_new_low = (lo <= low_20_min * 1.005).astype(float)
    g["alpha_low_20d_ratio"] = is_new_low.rolling(20, min_periods=5).mean()

    # alpha_close_open_gap: 跳空缺口 (前日收盘 vs 今日开盘)
    g["alpha_close_open_gap"] = (o - c.shift()) / c.shift().clip(lower=1e-8)

    # -- 基本面因子补充 --
    # pe_percentile: PE 历史分位数
    if "pe_ttm" in g.columns:
        pe_val = pd.to_numeric(g["pe_ttm"], errors="coerce")
        g["fund_pe_percentile"] = pe_val.rolling(120, min_periods=20).apply(
            lambda x: (x[-1] >= x).mean() if len(x) > 0 else 0.5, raw=True
        )

    # pb_percentile: PB 历史分位数
    if "pb" in g.columns:
        pb_val = pd.to_numeric(g["pb"], errors="coerce")
        g["fund_pb_percentile"] = pb_val.rolling(120, min_periods=20).apply(
            lambda x: (x[-1] >= x).mean() if len(x) > 0 else 0.5, raw=True
        )

    return g
