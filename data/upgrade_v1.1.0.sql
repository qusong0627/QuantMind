-- ============================================================
-- QuantMind 数据库升级脚本
-- ============================================================
-- 使用说明：
--   1. 执行前请先备份数据库：pg_dump -U quantmind quantmind > backup_$(date +%Y%m%d).sql
--   2. 执行升级脚本：psql -U quantmind -d quantmind -f data/upgrade_vX.X.X.sql
--   3. 验证升级结果：检查受影响表的列数/结构是否符合预期
--
-- 版本记录：
--   v1.0.0 (2026-05-11) - 初始版本，修复 stock_daily_latest 表字段类型
--   v1.1.0 (2026-05-11) - 重建 stock_daily_latest 表，统一 volume_trend_3d 为 double precision
--   v1.2.0 (2026-06-07) - 升级 sim_trades 表结构，添加缺失字段以匹配代码模型
--   v1.3.0 (2026-06-13) - 对齐模拟盘三表与 ORM：sim_orders / sim_trades / simulation_fund_snapshots
--   v1.4.0 (2026-06-19) - 标签迁移至 stock_tag 长表，删 stock_daily_latest 16 个标签列
--                         （详见 data/migrations/upgrade_v1.4.0_stock_tag.sql）
-- ============================================================

BEGIN;

-- ============================================================
-- v1.1.0: 重建 stock_daily_latest 表（89列）
-- ============================================================
-- 变更说明：
--   1. 清空并重建 stock_daily_latest 表
--   2. 修正 volume_trend_3d 字段类型：boolean -> double precision
--   3. 统一所有字段定义与当前运行环境一致
-- ============================================================

-- 1. 删除旧表（包括所有分区和数据）
DROP TABLE IF EXISTS public.stock_daily_latest CASCADE;

-- 2. 重建表（89列）
CREATE TABLE public.stock_daily_latest (
    trade_date date NOT NULL,
    symbol character varying(32) NOT NULL,
    stock_name text,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    amount double precision,
    pct_change double precision,
    turnover_rate double precision,
    pe_ttm double precision,
    pb double precision,
    total_mv double precision,
    float_mv double precision,
    listed_days integer,
    is_st smallint,
    listing_market character varying(16),
    industry text,
    province text,
    consecutive_limit_up_days integer,
    limit_up_today smallint,
    limit_down_today smallint,
    return_1d double precision,
    return_3d double precision,
    return_5d double precision,
    return_10d double precision,
    return_20d double precision,
    return_60d double precision,
    ma5 double precision,
    ma10 double precision,
    ma20 double precision,
    ma60 double precision,
    ma_gap_5 double precision,
    ma_gap_10 double precision,
    ma_gap_20 double precision,
    rsi_6 double precision,
    rsi_14 double precision,
    kdj_k double precision,
    kdj_d double precision,
    kdj_j double precision,
    macd_dif double precision,
    macd_dea double precision,
    macd_hist double precision,
    vol_std_5 double precision,
    vol_std_20 double precision,
    vol_std_60 double precision,
    vol_atr_14 double precision,
    volume_ratio_5 double precision,
    volume_ratio_20 double precision,
    volume_ma_5 double precision,
    amount_ma_5 double precision,
    bp double precision,
    ep_ttm double precision,
    ln_mv_total double precision,
    beta_20 double precision,
    label double precision,
    ind_code_l1 text,
    ind_code_l2 text,
    micro_effective_spread double precision,
    micro_imbalance_volume double precision,
    micro_jump_flag smallint,
    roe double precision,
    volume_trend_3d double precision,
    adj_factor double precision DEFAULT 1.0,
    volume_ma_3 double precision,
    idx_all integer DEFAULT 1,
    idx_hs300 integer DEFAULT 0,
    idx_zz1000 integer DEFAULT 0,
    idx_margin integer DEFAULT 0,
    concept_ai integer DEFAULT 0,
    concept_chip integer DEFAULT 0,
    concept_new_energy integer DEFAULT 0,
    concept_pv integer DEFAULT 0,
    concept_military integer DEFAULT 0,
    concept_medical integer DEFAULT 0,
    concept_fintech integer DEFAULT 0,
    concept_consumption integer DEFAULT 0,
    concept_state_owned integer DEFAULT 0,
    main_flow double precision,
    inst_ownership double precision,
    profit_growth double precision,
    idx_chinext integer DEFAULT 1,
    lrg_trd_tolbuynum double precision,
    lrg_trd_tolsellnum double precision,
    flow_net_amount double precision,
    b_volume double precision,
    s_volume double precision,
    concept_lithium integer DEFAULT 0
)
PARTITION BY RANGE (trade_date);

-- 3. 设置表所有者
ALTER TABLE public.stock_daily_latest OWNER TO quantmind;

-- 4. 添加表注释
COMMENT ON TABLE public.stock_daily_latest IS '股票日线行情最新数据（分区表），包含行情、技术指标、基本面、概念标签等综合字段';

-- 5. 添加字段注释
COMMENT ON COLUMN public.stock_daily_latest.trade_date IS '交易日期';
COMMENT ON COLUMN public.stock_daily_latest.symbol IS '股票代码（前缀格式，如 SH600000）';
COMMENT ON COLUMN public.stock_daily_latest.stock_name IS '股票名称';
COMMENT ON COLUMN public.stock_daily_latest.open IS '开盘价（后复权）';
COMMENT ON COLUMN public.stock_daily_latest.high IS '最高价（后复权）';
COMMENT ON COLUMN public.stock_daily_latest.low IS '最低价（后复权）';
COMMENT ON COLUMN public.stock_daily_latest.close IS '收盘价（后复权）';
COMMENT ON COLUMN public.stock_daily_latest.volume IS '成交量（手）';
COMMENT ON COLUMN public.stock_daily_latest.amount IS '成交额（元）';
COMMENT ON COLUMN public.stock_daily_latest.pct_change IS '涨跌幅（%）';
COMMENT ON COLUMN public.stock_daily_latest.turnover_rate IS '换手率（%）';
COMMENT ON COLUMN public.stock_daily_latest.pe_ttm IS '市盈率（TTM）';
COMMENT ON COLUMN public.stock_daily_latest.pb IS '市净率';
COMMENT ON COLUMN public.stock_daily_latest.total_mv IS '总市值（元）';
COMMENT ON COLUMN public.stock_daily_latest.float_mv IS '流通市值（元）';
COMMENT ON COLUMN public.stock_daily_latest.listed_days IS '上市天数';
COMMENT ON COLUMN public.stock_daily_latest.is_st IS '是否ST（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.listing_market IS '上市板块（SH/SZ/BJ）';
COMMENT ON COLUMN public.stock_daily_latest.industry IS '所属行业';
COMMENT ON COLUMN public.stock_daily_latest.province IS '所属省份';
COMMENT ON COLUMN public.stock_daily_latest.consecutive_limit_up_days IS '连续涨停天数';
COMMENT ON COLUMN public.stock_daily_latest.limit_up_today IS '今日是否涨停（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.limit_down_today IS '今日是否跌停（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.return_1d IS '1日收益率';
COMMENT ON COLUMN public.stock_daily_latest.return_3d IS '3日收益率';
COMMENT ON COLUMN public.stock_daily_latest.return_5d IS '5日收益率';
COMMENT ON COLUMN public.stock_daily_latest.return_10d IS '10日收益率';
COMMENT ON COLUMN public.stock_daily_latest.return_20d IS '20日收益率';
COMMENT ON COLUMN public.stock_daily_latest.return_60d IS '60日收益率';
COMMENT ON COLUMN public.stock_daily_latest.ma5 IS '5日均线';
COMMENT ON COLUMN public.stock_daily_latest.ma10 IS '10日均线';
COMMENT ON COLUMN public.stock_daily_latest.ma20 IS '20日均线';
COMMENT ON COLUMN public.stock_daily_latest.ma60 IS '60日均线';
COMMENT ON COLUMN public.stock_daily_latest.ma_gap_5 IS '5日乖离率';
COMMENT ON COLUMN public.stock_daily_latest.ma_gap_10 IS '10日乖离率';
COMMENT ON COLUMN public.stock_daily_latest.ma_gap_20 IS '20日乖离率';
COMMENT ON COLUMN public.stock_daily_latest.rsi_6 IS 'RSI(6)';
COMMENT ON COLUMN public.stock_daily_latest.rsi_14 IS 'RSI(14)';
COMMENT ON COLUMN public.stock_daily_latest.kdj_k IS 'KDJ-K值';
COMMENT ON COLUMN public.stock_daily_latest.kdj_d IS 'KDJ-D值';
COMMENT ON COLUMN public.stock_daily_latest.kdj_j IS 'KDJ-J值';
COMMENT ON COLUMN public.stock_daily_latest.macd_dif IS 'MACD-DIF';
COMMENT ON COLUMN public.stock_daily_latest.macd_dea IS 'MACD-DEA';
COMMENT ON COLUMN public.stock_daily_latest.macd_hist IS 'MACD柱状图';
COMMENT ON COLUMN public.stock_daily_latest.vol_std_5 IS '5日成交量标准差';
COMMENT ON COLUMN public.stock_daily_latest.vol_std_20 IS '20日成交量标准差';
COMMENT ON COLUMN public.stock_daily_latest.vol_std_60 IS '60日成交量标准差';
COMMENT ON COLUMN public.stock_daily_latest.vol_atr_14 IS 'ATR(14)';
COMMENT ON COLUMN public.stock_daily_latest.volume_ratio_5 IS '5日量比';
COMMENT ON COLUMN public.stock_daily_latest.volume_ratio_20 IS '20日量比';
COMMENT ON COLUMN public.stock_daily_latest.volume_ma_5 IS '5日均量';
COMMENT ON COLUMN public.stock_daily_latest.amount_ma_5 IS '5日均额';
COMMENT ON COLUMN public.stock_daily_latest.bp IS '买卖压力指标';
COMMENT ON COLUMN public.stock_daily_latest.ep_ttm IS '盈利收益率（TTM）';
COMMENT ON COLUMN public.stock_daily_latest.ln_mv_total IS '总市值对数';
COMMENT ON COLUMN public.stock_daily_latest.beta_20 IS 'Beta(20)';
COMMENT ON COLUMN public.stock_daily_latest.label IS '标签';
COMMENT ON COLUMN public.stock_daily_latest.ind_code_l1 IS '一级行业代码';
COMMENT ON COLUMN public.stock_daily_latest.ind_code_l2 IS '二级行业代码';
COMMENT ON COLUMN public.stock_daily_latest.micro_effective_spread IS '微观有效价差';
COMMENT ON COLUMN public.stock_daily_latest.micro_imbalance_volume IS '微观失衡成交量';
COMMENT ON COLUMN public.stock_daily_latest.micro_jump_flag IS '微观跳空标志（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.roe IS '净资产收益率';
COMMENT ON COLUMN public.stock_daily_latest.volume_trend_3d IS '3日成交量趋势值';
COMMENT ON COLUMN public.stock_daily_latest.adj_factor IS '复权因子';
COMMENT ON COLUMN public.stock_daily_latest.volume_ma_3 IS '3日均量';
COMMENT ON COLUMN public.stock_daily_latest.idx_all IS '全市场指数成分（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.idx_hs300 IS '沪深300成分（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.idx_zz1000 IS '中证1000成分（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.idx_margin IS '融资融券标的（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_ai IS 'AI概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_chip IS '芯片概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_new_energy IS '新能源概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_pv IS '光伏概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_military IS '军工概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_medical IS '医药概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_fintech IS '金融科技概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_consumption IS '消费概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.concept_state_owned IS '国企概念（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.main_flow IS '主力资金流向';
COMMENT ON COLUMN public.stock_daily_latest.inst_ownership IS '机构持股比例';
COMMENT ON COLUMN public.stock_daily_latest.profit_growth IS '利润增长率';
COMMENT ON COLUMN public.stock_daily_latest.idx_chinext IS '创业板成分（0/1）';
COMMENT ON COLUMN public.stock_daily_latest.lrg_trd_tolbuynum IS '大宗交易买入笔数';
COMMENT ON COLUMN public.stock_daily_latest.lrg_trd_tolsellnum IS '大宗交易卖出笔数';
COMMENT ON COLUMN public.stock_daily_latest.flow_net_amount IS '资金净流入';
COMMENT ON COLUMN public.stock_daily_latest.b_volume IS '主动买入量';
COMMENT ON COLUMN public.stock_daily_latest.s_volume IS '主动卖出量';
COMMENT ON COLUMN public.stock_daily_latest.concept_lithium IS '锂电概念（0/1）';

-- ============================================================
-- 升级完成验证
-- ============================================================
-- 兼容历史库：补齐 user_profiles.api_key（用户级，全系统通用 API Key）
ALTER TABLE public.user_profiles
    ADD COLUMN IF NOT EXISTS api_key text;

-- 验证 stock_daily_latest 表列数应为 89 列
SELECT COUNT(*) AS column_count 
FROM information_schema.columns 
WHERE table_name = 'stock_daily_latest';

COMMIT;

-- ============================================================
-- 后续升级脚本模板（复制以下内容创建新版本）
-- ============================================================
/*
-- v1.X.X (YYYY-MM-DD) - 升级说明
BEGIN;

-- ALTER TABLE public.xxx ADD COLUMN new_column type DEFAULT value;
-- ALTER TABLE public.xxx DROP COLUMN old_column;
-- ALTER TABLE public.xxx ALTER COLUMN col_name TYPE new_type;
-- CREATE INDEX idx_xxx ON public.xxx(column);

COMMIT;
*/

-- 更新 model_qlib 模型元数据
UPDATE public.qm_user_models 
SET 
    metadata_json = '{"run_id": "train_20260530233605_37cfc5d5", "job_name": "model_train_t5_20260530233604", "framework": "lightgbm", "model_type": "lightgbm", "model_file": "model.lgb", "feature_count": 48, "requested_feature_count": 48, "requested_features": ["mom_ret_5d", "mom_ret_20d", "mom_ret_60d", "mom_ret_120d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_ma_gap_60", "mom_macd_hist", "mom_rsi_14", "mom_breakout_20d", "vol_std_10", "vol_std_20", "vol_std_60", "vol_atr_14", "vol_parkinson_20", "vol_downside_20", "vol_realized_rv", "liq_turnover_tl", "liq_volume_ratio_5", "liq_volume_ratio_20", "liq_amount_ma_20", "liq_avg_trade_size", "liq_obv_20", "liq_amihud_20", "liq_amihud_60", "liq_mfi_14", "flow_net_amount_ratio", "flow_large_net_ratio", "flow_net_order_ratio", "flow_vpin_ma_20", "micro_imbalance_volume", "micro_effective_spread", "micro_pressure_score", "micro_jump_flag", "style_ln_mv_float", "style_bp", "style_ep_ttm", "style_beta_60", "style_idio_vol_60", "style_residual_ret_20", "style_valuation_composite", "style_size_percentile", "style_value_percentile", "ind_relative_volume_20", "ind_relative_volatility_20", "ind_strength_20", "ind_momentum_rank_20", "ind_value_rank"], "auto_appended_feature_count": 0, "auto_appended_features": [], "features": ["mom_ret_5d", "mom_ret_20d", "mom_ret_60d", "mom_ret_120d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_ma_gap_60", "mom_macd_hist", "mom_rsi_14", "mom_breakout_20d", "vol_std_10", "vol_std_20", "vol_std_60", "vol_atr_14", "vol_parkinson_20", "vol_downside_20", "vol_realized_rv", "liq_turnover_tl", "liq_volume_ratio_5", "liq_volume_ratio_20", "liq_amount_ma_20", "liq_avg_trade_size", "liq_obv_20", "liq_amihud_20", "liq_amihud_60", "liq_mfi_14", "flow_net_amount_ratio", "flow_large_net_ratio", "flow_net_order_ratio", "flow_vpin_ma_20", "micro_imbalance_volume", "micro_effective_spread", "micro_pressure_score", "micro_jump_flag", "style_ln_mv_float", "style_bp", "style_ep_ttm", "style_beta_60", "style_idio_vol_60", "style_residual_ret_20", "style_valuation_composite", "style_size_percentile", "style_value_percentile", "ind_relative_volume_20", "ind_relative_volatility_20", "ind_strength_20", "ind_momentum_rank_20", "ind_value_rank"], "feature_columns": ["mom_ret_5d", "mom_ret_20d", "mom_ret_60d", "mom_ret_120d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_ma_gap_60", "mom_macd_hist", "mom_rsi_14", "mom_breakout_20d", "vol_std_10", "vol_std_20", "vol_std_60", "vol_atr_14", "vol_parkinson_20", "vol_downside_20", "vol_realized_rv", "liq_turnover_tl", "liq_volume_ratio_5", "liq_volume_ratio_20", "liq_amount_ma_20", "liq_avg_trade_size", "liq_obv_20", "liq_amihud_20", "liq_amihud_60", "liq_mfi_14", "flow_net_amount_ratio", "flow_large_net_ratio", "flow_net_order_ratio", "flow_vpin_ma_20", "micro_imbalance_volume", "micro_effective_spread", "micro_pressure_score", "micro_jump_flag", "style_ln_mv_float", "style_bp", "style_ep_ttm", "style_beta_60", "style_idio_vol_60", "style_residual_ret_20", "style_valuation_composite", "style_size_percentile", "style_value_percentile", "ind_relative_volume_20", "ind_relative_volatility_20", "ind_strength_20", "ind_momentum_rank_20", "ind_value_rank"], "fill_values": {"mom_ret_5d": 0.0, "mom_ret_20d": 0.0, "mom_ret_60d": 0.0, "mom_ret_120d": 0.0, "mom_ma_gap_5": 0.0, "mom_ma_gap_20": 0.0, "mom_ma_gap_60": 0.0, "mom_macd_hist": 0.0, "mom_rsi_14": 0.0, "mom_breakout_20d": 0.0, "vol_std_10": 0.0, "vol_std_20": 0.0, "vol_std_60": 0.0, "vol_atr_14": 0.0, "vol_parkinson_20": 0.0, "vol_downside_20": 0.0, "vol_realized_rv": 0.0, "liq_turnover_tl": 0.0, "liq_volume_ratio_5": 0.0, "liq_volume_ratio_20": 0.0, "liq_amount_ma_20": 0.0, "liq_avg_trade_size": 0.0, "liq_obv_20": 0.0, "liq_amihud_20": 0.0, "liq_amihud_60": 0.0, "liq_mfi_14": 0.0, "flow_net_amount_ratio": 0.0, "flow_large_net_ratio": 0.0, "flow_net_order_ratio": 0.0, "flow_vpin_ma_20": 0.0, "micro_imbalance_volume": 0.0, "micro_effective_spread": 0.0, "micro_pressure_score": 0.0, "micro_jump_flag": 0.0, "style_ln_mv_float": 0.0, "style_bp": 0.0, "style_ep_ttm": 0.0, "style_beta_60": 0.0, "style_idio_vol_60": 0.0, "style_residual_ret_20": 0.0, "style_valuation_composite": 0.0, "style_size_percentile": 0.0, "style_value_percentile": 0.0, "ind_relative_volume_20": 0.0, "ind_relative_volatility_20": 0.0, "ind_strength_20": 0.0, "ind_momentum_rank_20": 0.0, "ind_value_rank": 0.0}, "train_start": "2016-01-04", "train_end": "2023-04-09", "val_start": "2023-04-14", "val_end": "2024-10-28", "test_start": "2024-11-02", "test_end": "2026-05-21", "data_source": "parquet", "best_iteration": 1500, "actual_best_iteration": 1500, "target_horizon_days": 5, "target_mode": "return", "label_formula": "label = CSZScore(future_return(T, T+5)) = CSZScore(open(T+5) / open(T) - 1)", "effective_trade_date": "2024-11-07", "training_window": "2016-01-04 → 2023-04-09 | 2023-04-14 → 2024-10-28 | 2024-11-02 → 2026-05-21", "metrics": {"train_ic": 0.13758859529069328, "train_rank_ic": 0.11329817262865656, "train_rank_icir": 1.2016261832599144, "val_ic": 0.10116139348982978, "val_rank_ic": 0.07444384525275476, "val_rank_icir": 0.5893313499152358, "test_ic": 0.09967784084034892, "test_rank_ic": 0.09650378334139832, "test_rank_icir": 0.9637338767045075}, "pred_coverage_start": "2016-01-04", "pred_coverage_end": "2026-05-15", "pred_rows": 10367422, "shap": {"enabled": true, "status": "completed", "split": "valid", "rows_requested": 30000, "rows_used": 30000, "file": "shap_summary.csv", "error": "", "elapsed_seconds": 3.4276957511901855}, "generated_at": "2026-05-30T15:43:40.933551", "elapsed_seconds": 251.22331738471985}'::jsonb,
    metrics_json = '{"train_ic": 0.13758859529069328, "train_rank_ic": 0.11329817262865656, "train_rank_icir": 1.2016261832599144, "val_ic": 0.10116139348982978, "val_rank_ic": 0.07444384525275476, "val_rank_icir": 0.5893313499152358, "test_ic": 0.09967784084034892, "test_rank_ic": 0.09650378334139832, "test_rank_icir": 0.9637338767045075}'::jsonb,
    updated_at = NOW()
WHERE model_id = 'model_qlib';

-- 同步模型名称为 Qlib_Base_Signal
UPDATE public.qm_user_models
SET
    metadata_json = '{"run_id": "train_20260530233605_37cfc5d5", "job_name": "model_train_t5_20260530233604", "framework": "lightgbm", "model_type": "lightgbm", "model_file": "model.lgb", "feature_count": 48, "requested_feature_count": 48, "requested_features": ["mom_ret_5d", "mom_ret_20d", "mom_ret_60d", "mom_ret_120d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_ma_gap_60", "mom_macd_hist", "mom_rsi_14", "mom_breakout_20d", "vol_std_10", "vol_std_20", "vol_std_60", "vol_atr_14", "vol_parkinson_20", "vol_downside_20", "vol_realized_rv", "liq_turnover_tl", "liq_volume_ratio_5", "liq_volume_ratio_20", "liq_amount_ma_20", "liq_avg_trade_size", "liq_obv_20", "liq_amihud_20", "liq_amihud_60", "liq_mfi_14", "flow_net_amount_ratio", "flow_large_net_ratio", "flow_net_order_ratio", "flow_vpin_ma_20", "micro_imbalance_volume", "micro_effective_spread", "micro_pressure_score", "micro_jump_flag", "style_ln_mv_float", "style_bp", "style_ep_ttm", "style_beta_60", "style_idio_vol_60", "style_residual_ret_20", "style_valuation_composite", "style_size_percentile", "style_value_percentile", "ind_relative_volume_20", "ind_relative_volatility_20", "ind_strength_20", "ind_momentum_rank_20", "ind_value_rank"], "auto_appended_feature_count": 0, "auto_appended_features": [], "features": ["mom_ret_5d", "mom_ret_20d", "mom_ret_60d", "mom_ret_120d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_ma_gap_60", "mom_macd_hist", "mom_rsi_14", "mom_breakout_20d", "vol_std_10", "vol_std_20", "vol_std_60", "vol_atr_14", "vol_parkinson_20", "vol_downside_20", "vol_realized_rv", "liq_turnover_tl", "liq_volume_ratio_5", "liq_volume_ratio_20", "liq_amount_ma_20", "liq_avg_trade_size", "liq_obv_20", "liq_amihud_20", "liq_amihud_60", "liq_mfi_14", "flow_net_amount_ratio", "flow_large_net_ratio", "flow_net_order_ratio", "flow_vpin_ma_20", "micro_imbalance_volume", "micro_effective_spread", "micro_pressure_score", "micro_jump_flag", "style_ln_mv_float", "style_bp", "style_ep_ttm", "style_beta_60", "style_idio_vol_60", "style_residual_ret_20", "style_valuation_composite", "style_size_percentile", "style_value_percentile", "ind_relative_volume_20", "ind_relative_volatility_20", "ind_strength_20", "ind_momentum_rank_20", "ind_value_rank"], "feature_columns": ["mom_ret_5d", "mom_ret_20d", "mom_ret_60d", "mom_ret_120d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_ma_gap_60", "mom_macd_hist", "mom_rsi_14", "mom_breakout_20d", "vol_std_10", "vol_std_20", "vol_std_60", "vol_atr_14", "vol_parkinson_20", "vol_downside_20", "vol_realized_rv", "liq_turnover_tl", "liq_volume_ratio_5", "liq_volume_ratio_20", "liq_amount_ma_20", "liq_avg_trade_size", "liq_obv_20", "liq_amihud_20", "liq_amihud_60", "liq_mfi_14", "flow_net_amount_ratio", "flow_large_net_ratio", "flow_net_order_ratio", "flow_vpin_ma_20", "micro_imbalance_volume", "micro_effective_spread", "micro_pressure_score", "micro_jump_flag", "style_ln_mv_float", "style_bp", "style_ep_ttm", "style_beta_60", "style_idio_vol_60", "style_residual_ret_20", "style_valuation_composite", "style_size_percentile", "style_value_percentile", "ind_relative_volume_20", "ind_relative_volatility_20", "ind_strength_20", "ind_momentum_rank_20", "ind_value_rank"], "fill_values": {"mom_ret_5d": 0.0, "mom_ret_20d": 0.0, "mom_ret_60d": 0.0, "mom_ret_120d": 0.0, "mom_ma_gap_5": 0.0, "mom_ma_gap_20": 0.0, "mom_ma_gap_60": 0.0, "mom_macd_hist": 0.0, "mom_rsi_14": 0.0, "mom_breakout_20d": 0.0, "vol_std_10": 0.0, "vol_std_20": 0.0, "vol_std_60": 0.0, "vol_atr_14": 0.0, "vol_parkinson_20": 0.0, "vol_downside_20": 0.0, "vol_realized_rv": 0.0, "liq_turnover_tl": 0.0, "liq_volume_ratio_5": 0.0, "liq_volume_ratio_20": 0.0, "liq_amount_ma_20": 0.0, "liq_avg_trade_size": 0.0, "liq_obv_20": 0.0, "liq_amihud_20": 0.0, "liq_amihud_60": 0.0, "liq_mfi_14": 0.0, "flow_net_amount_ratio": 0.0, "flow_large_net_ratio": 0.0, "flow_net_order_ratio": 0.0, "flow_vpin_ma_20": 0.0, "micro_imbalance_volume": 0.0, "micro_effective_spread": 0.0, "micro_pressure_score": 0.0, "micro_jump_flag": 0.0, "style_ln_mv_float": 0.0, "style_bp": 0.0, "style_ep_ttm": 0.0, "style_beta_60": 0.0, "style_idio_vol_60": 0.0, "style_residual_ret_20": 0.0, "style_valuation_composite": 0.0, "style_size_percentile": 0.0, "style_value_percentile": 0.0, "ind_relative_volume_20": 0.0, "ind_relative_volatility_20": 0.0, "ind_strength_20": 0.0, "ind_momentum_rank_20": 0.0, "ind_value_rank": 0.0}, "train_start": "2016-01-04", "train_end": "2023-04-09", "val_start": "2023-04-14", "val_end": "2024-10-28", "test_start": "2024-11-02", "test_end": "2026-05-21", "data_source": "parquet", "best_iteration": 1500, "actual_best_iteration": 1500, "target_horizon_days": 5, "target_mode": "return", "label_formula": "label = CSZScore(future_return(T, T+5)) = CSZScore(open(T+5) / open(T) - 1)", "effective_trade_date": "2024-11-07", "training_window": "2016-01-04 → 2023-04-09 | 2023-04-14 → 2024-10-28 | 2024-11-02 → 2026-05-21", "metrics": {"train_ic": 0.13758859529069328, "train_rank_ic": 0.11329817262865656, "train_rank_icir": 1.2016261832599144, "val_ic": 0.10116139348982978, "val_rank_ic": 0.07444384525275476, "val_rank_icir": 0.5893313499152358, "test_ic": 0.09967784084034892, "test_rank_ic": 0.09650378334139832, "test_rank_icir": 0.9637338767045075}, "pred_coverage_start": "2016-01-04", "pred_coverage_end": "2026-05-15", "pred_rows": 10367422, "shap": {"enabled": true, "status": "completed", "split": "valid", "rows_requested": 30000, "rows_used": 30000, "file": "shap_summary.csv", "error": "", "elapsed_seconds": 3.4276957511901855}, "generated_at": "2026-05-30T15:43:40.933551", "elapsed_seconds": 251.22331738471985, "model_name": "Qlib_Base_Signal", "display_name": "Qlib_Base_Signal"}'::jsonb,
    metrics_json = '{"train_ic": 0.13758859529069328, "train_rank_ic": 0.11329817262865656, "train_rank_icir": 1.2016261832599144, "val_ic": 0.10116139348982978, "val_rank_ic": 0.07444384525275476, "val_rank_icir": 0.5893313499152358, "test_ic": 0.09967784084034892, "test_rank_ic": 0.09650378334139832, "test_rank_icir": 0.9637338767045075}'::jsonb,
    updated_at = NOW()
WHERE model_id = 'model_qlib';

-- ============================================================
-- v1.2.0: 升级 sim_trades 表结构 (2026-06-07)
-- ============================================================
-- 变更说明：
--   1. 添加缺失字段以匹配 SQLAlchemy 模型
--   2. 新增: trade_id (UUID唯一标识), portfolio_id, trading_mode
--   3. 新增: trade_value, stamp_duty, transfer_fee, total_fee
--   4. 新增: executed_at (执行时间), price_source (价格来源)
--   5. 数据迁移: trade_time -> executed_at, 计算 trade_value
--   6. 字段类型变更: id VARCHAR -> INTEGER, user_id VARCHAR -> INTEGER
-- ============================================================

-- 添加新列（如果不存在）
ALTER TABLE public.sim_trades
    ADD COLUMN IF NOT EXISTS trade_id UUID DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS portfolio_id INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS trading_mode VARCHAR(20) NOT NULL DEFAULT 'SIMULATION',
    ADD COLUMN IF NOT EXISTS trade_value DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS stamp_duty DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS transfer_fee DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS total_fee DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS executed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS price_source VARCHAR(64);

-- 添加 trade_id 唯一约束
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sim_trades_trade_id_key') THEN
        ALTER TABLE public.sim_trades ADD CONSTRAINT sim_trades_trade_id_key UNIQUE (trade_id);
    END IF;
END $$;

-- 数据迁移：从 trade_time 复制到 executed_at
UPDATE public.sim_trades
SET executed_at = trade_time
WHERE trade_time IS NOT NULL;

-- 数据迁移：计算 trade_value
UPDATE public.sim_trades
SET trade_value = quantity * price
WHERE trade_value = 0 OR trade_value IS NULL;

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_sim_trades_trade_id ON public.sim_trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_sim_trades_order_id ON public.sim_trades(order_id);
CREATE INDEX IF NOT EXISTS idx_sim_trades_portfolio_id ON public.sim_trades(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_sim_trades_trading_mode ON public.sim_trades(trading_mode);
CREATE INDEX IF NOT EXISTS idx_sim_trades_tenant_user_symbol ON public.sim_trades(tenant_id, user_id, symbol);

-- 验证升级：检查列数
SELECT COUNT(*) AS sim_trades_column_count
FROM information_schema.columns
WHERE table_name = 'sim_trades' AND table_schema = 'public';

-- ============================================================
-- v1.3.0: 对齐模拟盘三表与 ORM 模型 (2026-06-13)
-- ============================================================
-- 变更说明：
--   1. sim_orders / sim_trades 新增 trade_action / position_side / is_margin_trade
--      （对应 backend/services/trade/simulation/models/{order,trade}.py）
--   2. sim_orders / sim_trades 的 user_id 从 VARCHAR/INTEGER 统一升级为 BIGINT
--      （ORM 中 SimOrder.user_id / SimTrade.user_id 均为 Mapped[int]）
--   3. simulation_fund_snapshots 新增 account_id / data(JSONB) / created_at
--      （对应 SimulationFundSnapshotService.capture_all 写入的字段集）
-- 幂等：所有语句使用 IF NOT EXISTS 与 DO 块，可重复执行
-- ============================================================

BEGIN;

-- --- sim_orders 列扩展 ---
ALTER TABLE public.sim_orders
    ADD COLUMN IF NOT EXISTS trade_action    VARCHAR(32),
    ADD COLUMN IF NOT EXISTS position_side   VARCHAR(16) DEFAULT 'long',
    ADD COLUMN IF NOT EXISTS is_margin_trade INTEGER NOT NULL DEFAULT 0;

-- --- sim_orders.user_id -> bigint ---
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'sim_orders'
          AND column_name  = 'user_id'
          AND data_type IN ('character varying', 'text', 'integer')
    ) THEN
        EXECUTE 'ALTER TABLE public.sim_orders ALTER COLUMN user_id TYPE BIGINT USING user_id::bigint';
    END IF;
END $$;

-- --- sim_trades 列扩展 ---
ALTER TABLE public.sim_trades
    ADD COLUMN IF NOT EXISTS trade_action    VARCHAR(32),
    ADD COLUMN IF NOT EXISTS position_side   VARCHAR(16) DEFAULT 'long',
    ADD COLUMN IF NOT EXISTS is_margin_trade INTEGER NOT NULL DEFAULT 0;

-- --- sim_trades.user_id -> bigint ---
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'sim_trades'
          AND column_name  = 'user_id'
          AND data_type IN ('character varying', 'text', 'integer')
    ) THEN
        EXECUTE 'ALTER TABLE public.sim_trades ALTER COLUMN user_id TYPE BIGINT USING user_id::bigint';
    END IF;
END $$;

-- --- portfolios.user_id -> integer ---
-- 修复 init 脚本历史遗留：portfolios.user_id 原为 varchar，但 ORM
-- (backend/services/trade/portfolio/models/__init__.py) 定义为 Integer，
-- 导致 Portfolio.user_id == int 查询触发 "varchar = integer" 类型错误。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'portfolios'
          AND column_name  = 'user_id'
          AND data_type IN ('character varying', 'text')
    ) THEN
        EXECUTE 'ALTER TABLE public.portfolios ALTER COLUMN user_id TYPE INTEGER USING user_id::integer';
    END IF;
END $$;

-- --- orders.user_id / trades.user_id -> integer ---
-- init 脚本历史遗留：orders/trades.user_id 为 varchar，但 ORM
-- (Order/Trade.user_id = Column(Integer)) 为 integer，导致查询触发
-- "operator does not exist: character varying = integer"。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'orders'
          AND column_name  = 'user_id'
          AND data_type IN ('character varying', 'text')
    ) THEN
        EXECUTE 'ALTER TABLE public.orders ALTER COLUMN user_id TYPE INTEGER USING user_id::integer';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'trades'
          AND column_name  = 'user_id'
          AND data_type IN ('character varying', 'text')
    ) THEN
        EXECUTE 'ALTER TABLE public.trades ALTER COLUMN user_id TYPE INTEGER USING user_id::integer';
    END IF;
END $$;

-- --- simulation_fund_snapshots 列扩展 ---
ALTER TABLE public.simulation_fund_snapshots
    ADD COLUMN IF NOT EXISTS account_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS data       JSONB,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now();

COMMIT;

-- 验证升级：确认关键列已存在
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('sim_orders', 'sim_trades', 'simulation_fund_snapshots')
  AND column_name IN ('user_id', 'trade_action', 'position_side', 'is_margin_trade',
                      'account_id', 'data', 'created_at')
ORDER BY table_name, column_name;
