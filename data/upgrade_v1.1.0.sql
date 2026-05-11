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
