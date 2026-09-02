/** 港股市场分析 · 类型定义（与 backend market_analysis_hk 响应对齐） */

/** 恒生四大指数快照 */
export interface HkIndexItem {
  symbol: string;
  name: string;
  price: number;
  change: number;
  pct_change: number;
  turnover_yi: number;
  trend: number[];
  trade_date: string;
}

/** 市场温度计（港股无涨跌停，±5% 为快涨快跌口径） */
export interface HkBreadthData {
  trade_date: string;
  total_stocks: number;
  advance_count: number;
  decline_count: number;
  flat_count: number;
  big_up_count: number;   // 涨幅 >= +5%
  big_down_count: number; // 跌幅 <= -5%
  total_turnover_yi: number;
  profit_effect: number;  // 上涨占比 %
  sentiment_score: number;
}

/** 恒生行业热力图条目（与 A 股 ShenwanHeatmapChart 数据形状一致） */
export interface HkSectorHeatItem {
  name: string;
  value: number;
  pct_change: number;
  leader: string;
  leader_pct: number;
  stock_count: number;
}

/** 南向资金总览 */
export interface HkSouthOverview {
  trade_date: string;
  covered_stocks: number;
  total_hold_value_yi: number;
  up_days_change: number;
  down_days_change: number;
  south_stock_count: number;
}

/** 南向个股增减持条目 */
export interface HkSouthFlowItem {
  symbol: string;
  name: string;
  pct_change_abs: number; // 持股占比变动（百分点）
  holding_pct: number;    // 当前持股占比 %
  price?: number;
  turnover_yi?: number;
}

export interface HkSouthFlow {
  trade_date: string;
  period: number;
  increase: HkSouthFlowItem[];
  decrease: HkSouthFlowItem[];
}

/** 南向板块配置 */
export interface HkSouthSectorItem {
  name: string;
  hold_value_yi: number;
  pct_avg: number;
  stock_count: number;
}

/** CCASS 全市场集中度 */
export interface HkCcassRankItem {
  symbol: string;
  name: string;
  top10_pct: number;
  south_pct: number;
  cust_pct: number;
  hhi: number;
  top10_d1: number;
}

export interface HkCcassRankings {
  trade_date: string;
  items: HkCcassRankItem[];
}

/** 个股 CCASS 席位明细 */
export interface HkCcassHoldingItem {
  participant_id: string;
  participant_name: string;
  holding_quantity: number;
  holding_pct: number;
}

export interface HkCcassHolding {
  trade_date: string;
  symbol: string;
  name: string;
  items: HkCcassHoldingItem[];
}

/** CCASS 席位异动 */
export interface HkCcassMoverItem {
  symbol: string;
  name: string;
  count: number;
  top10_d1: number;
}

export interface HkCcassMovers {
  trade_date: string;
  new_entrants: HkCcassMoverItem[];
  exits: HkCcassMoverItem[];
}

/** AH 对应股（含溢价） */
export interface HkAhPairItem {
  h_symbol: string;
  h_name: string;
  h_pct_change: number;
  h_close: number;
  a_symbol: string;
  cn_name: string;
  premium_pct?: number | null; // >0 = A 贵（H 折价）；<0 = 倒挂
}

/** 估值主题 */
export interface HkValuationItem {
  symbol: string;
  name: string;
  value: number;
  total_market_cap_yi?: number;
  pe?: number;
  pb?: number;
}

export interface HkValuationRanking {
  kind: string;
  titlename: string;
  published_at: string;
  items: HkValuationItem[];
}

/** 行业轮动 */
export interface HkRotationItem {
  name: string;
  ret_1d: number;
  ret_5d: number;
  ret_20d: number | null;
  turnover_yi: number;
}

export interface HkSectorRotation {
  trade_date: string;
  items: HkRotationItem[];
}

/** 个股综合赚钱效应 */
export interface HkProfitLeaderItem {
  symbol: string;
  name: string;
  pct_change: number;
  turnover_yi: number;
  score: number;
}

export interface HkProfitLeaders {
  trade_date: string;
  items: HkProfitLeaderItem[];
}

/** AH 溢价 */
export interface HkAhPremiumItem {
  h_symbol: string;
  h_name: string;
  a_symbol: string;
  a_close: number;
  h_close: number;
  fx_hkd_cny: number;
  premium_pct: number;
}

export interface HkAhPremium {
  trade_date: string;
  premium: HkAhPremiumItem[]; // A 贵 H 便宜（H 折价）
  discount: HkAhPremiumItem[]; // 倒挂（H 贵 A 便宜）
}

/** 派息日历 */
export interface HkDividendItem {
  symbol: string;
  name: string;
  ex_date: string;
  pay_date: string;
  plan: string;
  dividend: number | null;
}

export interface HkDividendCalendar {
  trade_date: string;
  items: HkDividendItem[];
}

/** 行业估值温度计 */
export interface HkSectorValuationItem {
  name: string;
  pe_median: number | null;
  dividend_yield: number | null;
  stock_count: number;
}

/** 数据状态 */
export interface HkFeedStatus {
  available: boolean;
  data_dir: string;
  latest_kline_date: string | null;
  latest_ccass_date: string | null;
  industry_count: number;
  south_files: number;
}