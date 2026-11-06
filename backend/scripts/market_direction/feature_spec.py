"""大盘方向模型的特征清单：因子聚合规格 + 基座特征列名。

因子来源 = 「因子研究 → 筛选」保留集里的 L1/L2 因子（``l1_l2_factors`` 宽表），
外加少量市场状态必需但未进保留集的字段（估值 ``fun_pe/fun_pb``、筹码、
概念热度）。每个因子按 ``AGG_KINDS`` 组合成市场级聚合（等权 / 市值加权 /
大盘-小盘差 / 截面离散度），再由构建脚本做 252 日滚动分位。
"""

from __future__ import annotations

from backend.scripts.market_direction.features import AggSpec

# 核心市场状态因子：四种聚合全做（等权 / 市值加权 / 大小盘差 / 离散度）
CORE_FACTORS: tuple[str, ...] = (
    "turn_20",  # 20 日换手：情绪/拥挤度
    "amt_ratio_5_20",  # 成交额短/长比：量能加速度
    "vol_std_20",  # 已实现波动
    "mom_ret_1d",  # 当日收益（截面离散度 = 分化）
    "micro_vpin_20",  # 知情交易比例（毒性）
    "micro_liquidity_amihud_5",  # Amihud 非流动性
    "flow_net_ratio",  # 资金净流入比例
    "style_idio_vol_20",  # 特质波动
)

# 次级因子：等权均值 + 大小盘差（成本更低，保留「大盘股 vs 小盘股」信息）
SECONDARY_FACTORS: tuple[str, ...] = (
    "turn_hl_pos_20",
    "amt_high_days_10",
    "mom_ret_20d",
    "vol_atr_14",
    "vol_realized_rv",
    "vol_realized_rrv",
    "vol_persistence",
    "mfi_14",
    "micro_vpin_vol_ratio",
    "micro_vpin_amount_ratio",
    "micro_toxicity_persistence",
    "micro_amihud_illiquidity",
    "micro_kyle_lambda",
    "flow_large_ratio",
    "flow_big_trade_ratio",
    "flow_sell_amount",
    "micro_trade_buy_pressure",
    "micro_trade_sign_autocorr",
    "micro_depth_slope",
    "chip_profit_ratio_20",
    "chip_concentration_20",
    "style_residual_ret_20",
    "concept_hot_score",
    "concept_crowding_max",
    "fun_pe",
    "fun_pb",
    "ind_dispersion_20",
    "ind_breadth_up_20",
)


def build_factor_specs() -> tuple[AggSpec, ...]:
    specs: list[AggSpec] = []
    for factor in CORE_FACTORS:
        for kind in ("mean", "capw", "ls", "std"):
            specs.append(AggSpec(factor, kind))
    for factor in SECONDARY_FACTORS:
        for kind in ("mean", "ls"):
            specs.append(AggSpec(factor, kind))
    return tuple(specs)


FACTOR_SPECS: tuple[AggSpec, ...] = build_factor_specs()
FACTOR_AGG_COLUMNS: tuple[str, ...] = tuple(s.name for s in FACTOR_SPECS)
FACTOR_FEATURE_COLUMNS: tuple[str, ...] = tuple(
    f"{name}_pct" for name in FACTOR_AGG_COLUMNS
)

# 因子表里除聚合列外还需要读取的辅助列
FACTOR_AUX_COLUMNS: tuple[str, ...] = ("symbol", "date", "fun_mv_rank", "fun_total_mv")

# 全 A 广度（``by_universe`` universe=all）
MARKET_BREADTH_SOURCE: dict[str, str] = {
    "n_limit_up": "mkt_n_limit_up",
    "n_limit_down": "mkt_n_limit_down",
    "ret_equal_weight": "mkt_ret_ew",
    "ret_disp": "mkt_disp",
    "pct_above_ma20": "mkt_pct_ma20",
    "pct_above_ma60": "mkt_pct_ma60",
    "trin_ema10": "mkt_trin_ema10",
    "mcclellan": "mkt_mcclellan",
    "adl_slope": "mkt_adl_slope",
    "nh_nl_pct": "mkt_nh_nl_pct",
    "limit_net": "mkt_limit_net",
    "amt_shock": "mkt_amt_shock",
    "stress": "mkt_stress",
}

# 大盘股广度（``by_universe`` universe=hs300：逐日流通市值前 300，PIT）
LARGE_CAP_BREADTH_SOURCE: dict[str, str] = {
    "pct_above_ma20": "lc_pct_ma20",
    "pct_above_ma60": "lc_pct_ma60",
    "ret_equal_weight": "lc_ret_ew",
    "ret_disp": "lc_disp",
    "trin_ema10": "lc_trin_ema10",
    "adl_slope": "lc_adl_slope",
    "nh_nl_pct": "lc_nh_nl_pct",
    "n_limit_up": "lc_n_limit_up",
}

# 广度里需要做 252 日滚动 z 的慢漂移水平量
BREADTH_ZSCORE_COLUMNS: tuple[str, ...] = (
    "mkt_mcclellan",
    "mkt_adl_slope",
    "mkt_stress",
    "lc_adl_slope",
)

# 两融面板（``panel_market.parquet``，T+1 披露 → 用 _lag1 版本）
MARGIN_COLUMNS: dict[str, str] = {
    "fin_chg5_lag1": "mg_fin_chg5",
    "fin_chg20_lag1": "mg_fin_chg20",
    "fin_net5_ratio_lag1": "mg_fin_net5_ratio",
    "fin_net20_ratio_lag1": "mg_fin_net20_ratio",
    "slo_chg5_lag1": "mg_slo_chg5",
    "slo_chg20_lag1": "mg_slo_chg20",
    "fin_bal_pct": "mg_fin_bal_pct",
    "fin_chg20_pct": "mg_fin_chg20_pct",
}

# 宏观（``macro_daily.parquet``）
MACRO_COLUMNS: tuple[str, ...] = (
    "shibor_on_chg_5",
    "shibor_3m_chg_5",
    "cn_10y_chg_5",
    "cn_10y_z60",
    "us_10y_chg_5",
    "usdcny_chg_5",
    "usdcny_z60",
    "cn_term_spread",
    "cn_us_10y_spread",
)

# 股指期货基差（``index_futures_basis.parquet``）
# 只保留 2016 年起就有覆盖的品种：IM（2022-07 才有）会把「年份」编码进特征
BASIS_COLUMNS: tuple[str, ...] = (
    "if_basis_z60",
    "ih_basis_z60",
    "ic_basis_z60",
    "basis_if_ic_spread",
)

# 指数技术面（由 index_daily 现算，逐指数）
INDEX_TECH_COLUMNS: tuple[str, ...] = (
    "idx_ret_1",
    "idx_ret_3",
    "idx_ret_5",
    "idx_ret_10",
    "idx_ret_20",
    "idx_ret_60",
    "idx_ret_120",
    "idx_ma_gap_5",
    "idx_ma_gap_10",
    "idx_ma_gap_20",
    "idx_ma_gap_60",
    "idx_ma_gap_120",
    "idx_rsi_6",
    "idx_rsi_14",
    "idx_macd_hist_norm",
    "idx_boll_pctb",
    "idx_vol_10",
    "idx_vol_20",
    "idx_vol_60",
    "idx_vol_ratio_10_60",
    "idx_atr_pct_14",
    "idx_dd_252",
    "idx_dist_high_20",
    "idx_dist_low_20",
    "idx_vol_z_20",
    "idx_amt_z_20",
)

BASE_FEATURE_COLUMNS: tuple[str, ...] = (
    INDEX_TECH_COLUMNS
    + tuple(MARKET_BREADTH_SOURCE.values())
    + ("mkt_adv_ratio",)
    + tuple(LARGE_CAP_BREADTH_SOURCE.values())
    + tuple(MARGIN_COLUMNS.values())
    + MACRO_COLUMNS
    + BASIS_COLUMNS
)

ALL_FEATURE_COLUMNS: tuple[str, ...] = BASE_FEATURE_COLUMNS + FACTOR_FEATURE_COLUMNS

# 理论驱动的小特征集（趋势 / 反转 / 波动 / 广度 / 估值 / 换手），给强正则模型用
COMPACT_FEATURES: tuple[str, ...] = (
    "idx_ma_gap_20",
    "idx_ma_gap_60",
    "idx_ma_gap_120",
    "idx_ret_60",
    "idx_dd_252",
    "idx_ret_5",
    "idx_dist_high_20",
    "idx_rsi_6",
    "idx_vol_20",
    "idx_atr_pct_14",
    "fa_vol_realized_rv_mean_pct",
    "mkt_pct_ma20",
    "lc_pct_ma20",
    "mkt_adv_ratio",
    "fa_fun_pb_mean_pct",
    "fa_fun_pb_ls_pct",
    "fa_turn_20_mean_pct",
    "fa_mom_ret_1d_std_pct",
    "usdcny_chg_5",
)

TARGET_INDICES: dict[str, str] = {
    "000300.SH": "沪深300",
    "000001.SH": "上证综指",
    "000852.SH": "中证1000",
}
