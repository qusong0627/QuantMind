/** 个股终端类型定义 */

export interface StockListItem {
  symbol: string;        // 600519.SH
  name: string;
  board: string;         // 沪市主板/科创板/深市主板/创业板/北交所
  industry: string | null;
  close: number | null;
  pct_change: number | null;  // 百分数
  total_mv: number | null;    // 亿元
  float_mv: number | null;    // 亿元
  pe: number | null;
  pb: number | null;
  is_st: boolean;
  fusion: number | null;
  side: string | null;
  signal_date: string | null;
  model: string | null;
  /** 仓位信号分 0=不入场 / 0.1~0.99=建议投入比例（半凯利+截面基准） */
  position_score: number | null;
  /** 所在行业前10均分基准（直观参照） */
  industry_top10_avg: number | null;
  /** 所在板块前10均分基准 */
  board_top10_avg: number | null;
  /** 所在市值档前10均分基准 */
  cap_top10_avg: number | null;
  /** 个股在行业内的百分位 0~1 */
  pct_industry: number | null;
  /** 大盘是否空仓信号（弱市） */
  market_empty: boolean | null;
  cap_tier?: string;   // 微盘/小盘/中盘/大盘/超大盘
  trend?: string;      // 连续上升/连续下降/先升后降/上升/下降/持平/-
}

export interface StockListResponse {
  total: number;
  page: number;
  page_size: number;
  trade_date: string;
  signal_date?: string;
  items: StockListItem[];
  /** 定位股票在当前排序中的名次（find_symbol 参数时返回，1-based；无分数为 null） */
  find_rank?: number | null;
  /** 推理模型选项（真实 model_id + display_name，供筛选下拉） */
  models?: { model_id: string; display_name?: string }[];
  /** 筛选下拉选项命中数（with_counts=true 时返回），如 { board: {沪市主板: 1500}, model: {...} } */
  option_counts?: Record<string, Record<string, number>>;
  /** 列表内各列的取值集合（表头筛选选项），如 { board: [...], industry: [...], cap_tier: [...], trend: [...], side: [...] } */
  facets?: Record<string, string[]>;
}

export interface IndexMembership {
  index_code: string;
  index_name: string;
  weight: number | null;
}

export interface StockProfile {
  symbol: string;
  name: string;
  board: string;
  industry: string | null;
  trade_date: string;
  close: number | null;
  pct_change: number | null;
  total_mv: number | null;        // 亿元
  float_mv: number | null;        // 亿元
  total_share: number | null;     // 万股
  free_float_share: number | null;
  pe_dynamic: number | null;
  pb: number | null;
  dividend_yield: number | null;
  beta: number | null;
  staff_num: number | null;
  main_business: string | null;
  ipo_price: number | null;
  limit_up_price: number | null;
  limit_down_price: number | null;
  flags: {
    hs300: boolean;
    marginable: boolean;
    sh_hk_connect: boolean;
    is_st: boolean;
    is_hk_listed: boolean;
  };
  valuation: {
    pe_ttm?: number | null;
    pe_static?: number | null;
    pb?: number | null;
    ps_ttm?: number | null;
    dividend_rate?: number | null;
    total_mv?: number | null;
    float_mv?: number | null;
    net_profit_ttm?: number | null;
    revenue_ttm?: number | null;
    equity?: number | null;
  };
  index_membership: IndexMembership[];
  concepts: string[];
  /** 预测日（推理信号日） */
  signal_date?: string | null;
  /** L2 微观结构因子（预测日前一交易日，14 个推荐因子 + 当日全市场百分位） */
  l2_features?: {
    feature_date: string;
    factors: {
      name: string;
      label: string;
      category: string;
      icir: number;
      value: number | null;
      pct_rank: number | null;   // 0~1，全市场低于该值的占比
    }[];
  } | null;
}

export interface KlineBar {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  amount?: number | null;
}

export type Exchange = 'SH' | 'SZ' | 'BJ';