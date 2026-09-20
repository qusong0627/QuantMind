/** 个股终端类型定义 */

export interface StockListItem {
  symbol: string;        // 600519.SH
  name: string;
  board: string;         // 沪市主板/科创板/深市主板/创业板/北交所
  industry: string | null;
  close: number | null;
  pct_change: number | null;  // 百分数
  total_mv: number | null;    // 亿元（A 股/港股口径）
  float_mv: number | null;    // 亿元
  /** 市场惯用市值显示串（美股为美元，由后端格式化）；有值时搜索框优先展示它而非 total_mv */
  cap_display?: string | null;
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

/** 事件竖线（如美股拆股）：在指定日期画一条竖虚线并标注，用于解释未复权价的跳变 */
export interface KlineMarker {
  date: string;
  label: string;
  color?: string;
}

/** K 线响应里携带的拆股事件（后端只在窗口内返回） */
export interface KlineSplitsEvent {
  date: string;
  ratio: number | null;
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

/** 模拟交易点（K 线买卖标记；T-FE-08 下钻：理由/订单号） */
export interface TradeMarker {
  date: string;
  side: 'buy' | 'sell';
  price: number;
  shares: number;
  reason?: string;
  order_id?: string;
  amount?: number;
  fee?: number;
}

// ---------------------------------------------------------------------------
// 信号准确率回看（T-N 分数/排名 → 至今涨跌）
//
// 契约类型放共享层（与 StockListResponse 同理：描述接口形状）；
// 展示口径的纯函数在 `features/stock-terminal/lookbackModel.ts`。
// 后端唯一事实源：`backend/services/api/stock_lookback.py`。
// ---------------------------------------------------------------------------

/** 单个回看点的涨跌用了什么价：实时快照 还是 最新收盘 */
export type PriceSourceKind = 'live' | 'close';
/** 整张表的价格构成：全实时 / 混合 / 全收盘 */
export type PriceKind = 'live' | 'mixed' | 'close';

/** 某只票在 T-N 那天的分数/名次 + 从那天的基准价到现价的涨跌。null 一律表示**缺**。 */
export interface LookbackPoint {
  lookback: number;
  signal_date: string;
  score: number | null;
  rank: number | null;
  /** 该 run 内的分位（0~1，1 = 最高分） */
  rank_pct: number | null;
  /** 当日参与打分的全市场只数（名次的分母） */
  day_n: number | null;
  /** 相对该回看日基准价的涨跌（小数） */
  ret: number | null;
  price_source: PriceSourceKind;
}

export interface LookbackSummaryRow {
  lookback: number;
  label: string;
  signal_date: string;
  /** 基准价取自分区日（= 回看日自己）；与顶层 `price_as_of`（现价日）不是一回事 */
  base_price_date: string | null;
  run_id: string | null;
  model_version: string | null;
  /** 该行 run 的分数离散度与锚点同档。**只表示量纲可比，不表示同一个模型** */
  comparable: boolean;
  sample: number;
  n_hi: number;
  n_lo: number;
  n_neg: number;
  missing_price: number;
  score_std: number | null;
  hi_avg: number | null;
  lo_avg: number | null;
  spread: number | null;
  hi_hit: number | null;
  lo_hit: number | null;
  neg_avg: number | null;
  avg_score_hi: number | null;
  avg_score_lo: number | null;
}

export interface LookbackDetailItem {
  symbol: string;
  name: string;
  score_now: number | null;
  rank_now: number | null;
  side_now: string;
  points: LookbackPoint[];
}

export interface SignalLookbackData {
  status: 'ok' | 'unavailable';
  reason?: string;
  as_of?: string;
  /** 现价取数日（可能是锚点日之前的最近分区） */
  price_as_of?: string | null;
  price_source?: PriceKind;
  live_count?: number;
  close_count?: number;
  /** 所有回看点都同口径才为 true；逐行的判定看 `summary[].comparable` */
  comparable?: boolean;
  bucket_pct?: number;
  lookbacks?: number[];
  summary?: LookbackSummaryRow[];
  detail?: {
    total: number;
    page: number;
    page_size: number;
    items: LookbackDetailItem[];
  };
}

export interface SignalLookbackParams {
  lookbacks?: string;
  model?: string;
  side?: string;
  bucket_pct?: number;
  /** auto = 实时优先缺则收盘；close = 只用收盘 */
  price_source?: 'auto' | 'close';
  /** 锚定信号日 YYYY-MM-DD；缺省 = 最近覆盖充分日 */
  asof?: string;
  page?: number;
  page_size?: number;
}
