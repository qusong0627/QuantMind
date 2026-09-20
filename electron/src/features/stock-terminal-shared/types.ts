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
  /** 风险载荷（仅 A 股候选列表返回；无任何命中/标注时整个字段缺席） */
  risk?: StockRisk | null;
}

/** 近 20 天新闻标签的一条（通道 B）。`samples` 只有利空档带——用户要能看到「凭什么说它利空」 */
export interface NewsTagBucket {
  tag: string;
  n: number;
  last?: string | null;   // 最近发布时间（ISO8601，UTC）
  samples?: string[];     // 证据标题（最多 3 条，仅 risk 档）
}

/** 名单命中一条（通道 A）。`reason` 可能很长（多条来源拼接），只在悬停里展开 */
export interface StockRiskHit {
  symbol: string;
  sources: string[];
  source_labels?: string[];
  flags?: string[];
  reason?: string;
  /** 时间窗到期日（如解禁类）；已过期的条目不计入 blocking */
  expire?: string | null;
  blocking?: boolean;
  expired?: boolean;
}

export interface StockRisk {
  /** 命中「默认排除判据」。与开关是否打开无关——关掉开关后前端仍要把这些行标出来 */
  excluded: boolean;
  hits?: StockRiskHit[];
  /** 五档方向固定存在（空桶保留），前端按固定桶渲染 */
  news?: Record<string, NewsTagBucket[]>;
}

/** 列表响应级排除元信息：两条通道各自的基准/新鲜度 + 本轮实际排除只数 */
export interface ExclusionMeta {
  list: {
    imported: boolean;
    market?: string;
    asof?: string;
    generated_at?: string;
    stale_days?: number | null;
    stale?: boolean;
    counts?: { total?: number; blocking?: number; by_source?: Record<string, number> };
    sources?: Record<string, { label?: string; count?: number; asof?: string; blocking?: boolean }>;
    /** imported=false 时的原因（名单文件未导入） */
    reason?: string;
  };
  news: {
    available: boolean;
    window_end?: string;
    stale_days?: number | null;
    tags?: number;
    symbols?: number;
    reason?: string;
  };
  /** 本轮各通道**实际**排除的只数（只减不说的列表会让用户以为数据丢了） */
  excluded: { st?: number; risk_list?: number; news_risk?: number };
  risk_list_size?: number;
  news_risk_size?: number;
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
  /** 排除闸门元信息（仅 A 股返回；港股/美股无该字段，前端自动降级不渲染） */
  exclusion_meta?: ExclusionMeta | null;
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

// ---------------------------------------------------------------------------
// 候选信号多选一键推送（契约类型放共享层，展示口径纯函数在
// `features/stock-terminal/pushModel.ts`）。
// 后端唯一事实源：`backend/services/api/routers/push_orders.py`。
// ---------------------------------------------------------------------------

export type PushSide = 'buy' | 'sell';
/** `real` 是**叠加**在模拟盘建单之上的真单镜像，不是替代品（见 channels_effective） */
export type PushChannel = 'sim' | 'real';

/** 服务端逐笔的阻断位；空串 = 没被阻断 */
export type PushBlockedBy = '' | 'quantity' | 'list' | 'risk';

/** 实盘配额试算：这一笔会不会被镜像闸跳过（`will_skip` 必须显示成「不会下发真单」） */
export interface MirrorPrecheck {
  will_skip: boolean;
  /** max_daily_orders / max_daily_symbols / max_daily_value / max_order_value */
  reason: string;
}

/** 风控单条决策（`l0.*` 进 environment，其余进 subject） */
export interface RiskDecisionRow {
  rule_id: string;
  level?: string;
  /** REJECT | WARN | HALT */
  action?: string;
  reason?: string;
  evidence?: Record<string, unknown>;
}

/** 确认面板的一行（= 服务端算出来的这一笔） */
export interface PushLeg {
  symbol: string;
  name: string;
  price: number | null;
  position_score: number | null;
  signal_date: string | null;
  available_position: number;
  amount: number;
  risk: StockRisk | null;
  quantity: number;
  /** `auto`=服务端按 可用资金×仓位 算的 / `manual`=手填 / `blocked`=算不出 */
  source: string;
  /** 给人看的说明（整手对齐、资金来源），不阻断 */
  note: string;
  /** 必须阻断的违规（与 note 分开：整手对齐照常下单，违规必须点不动确认） */
  problem: string;
  blocked_by: PushBlockedBy | string;
  executable: boolean;
  /** 服务端恒发（`null` 与 `{will_skip:false}` 是「没查」与「查了没事」两件事） */
  mirror_precheck: MirrorPrecheck | null;
  risk_verdict?: string;
  risk_enforced?: boolean;
  risk_rule_id?: string;
  risk_reason?: string;
  environment?: RiskDecisionRow[];
  subject?: RiskDecisionRow[];
}

/** 实盘闸门快照（仅 `channels` 含 `real` 时是真实内容，否则只有 `requested:false`） */
export interface PushMirrorPlan {
  requested: boolean;
  available?: boolean;
  reason?: string;
  enabled?: boolean;
  kill_switch?: boolean;
  trading_time?: boolean;
  real_trading_ready?: boolean;
  blocked_reason?: string;
  /** 通道不就绪的原因（镜像开着也可能不就绪，与 blocked_reason 是两个问题） */
  not_ready_reason?: string;
  /** 非交易时段下单不丢，而是入队等开盘——「排队」与「已成交」对真钱必须分清 */
  will_queue?: boolean;
  broker_selected?: string;
  whitelist?: string[];
  blacklist?: string[];
  queue_length?: number | null;
  config?: Record<string, number | string | boolean>;
  quota?: Record<string, number | null | undefined>;
}

export interface PushLegSummary {
  total: number;
  executable: number;
  blocked: number;
  est_amount: number;
}

/**
 * 整批资金约束（`meta.budget`）。
 *
 * 逐笔各自按**全量**可用资金算量会系统性超配（实测 3 只候选 vs 48.9 万 → 合计 124.5 万，
 * 2.5 倍），多出来的单子在账户层逐笔被拒，看起来像「莫名其妙下单失败」。后端按系数把
 * **自动算出的**量等比例缩回来；手填量不缩。`applied=false` 时只有 `factor:1`。
 */
export interface PushBudget {
  applied: boolean;
  factor: number;
  available_cash?: number;
  planned_amount?: number;
  /** 缩量说明（`applied=false` 时为空串；UI 只在 applied 时展示） */
  note?: string;
}

export interface PushPreflightMeta {
  signal_date: string | null;
  available_cash: number;
  account_found: boolean;
  exclusion?: Record<string, unknown>;
  /** false = 名单未导入（要显式提示，不能当空名单静默放行） */
  exclusion_imported?: boolean;
  budget?: PushBudget;
}

export interface PushPreflight {
  batch_id: string;
  side: PushSide;
  channels: PushChannel[];
  channels_effective: string[];
  ack_risk: boolean;
  legs: PushLeg[];
  mirror: PushMirrorPlan;
  summary: PushLegSummary;
  meta?: PushPreflightMeta;
}

export interface PushLegResult {
  symbol: string;
  success: boolean;
  /** false = 预检就阻断，压根没提交（`skipped_reason` 有原因） */
  executed: boolean;
  skipped_reason?: string;
  order_id?: string | null;
  trade_id?: string | null;
  fill_price?: number | null;
  filled_quantity?: number | null;
  commission?: number | null;
  message?: string;
  duplicate?: boolean;
  /** 仅实盘腿有；`class` 只有 success 才算真发出去了 */
  mirror?: { status: string; class: string; reason?: string; order_id?: string | null; client_order_id?: string | null } | null;
}

export interface PushExecute {
  batch_id: string;
  dry_run: boolean;
  /** executed | partial | failed | blocked | preview */
  status: string;
  side?: PushSide;
  channels: PushChannel[];
  channels_effective: string[];
  results: PushLegResult[];
  summary: PushLegSummary & { attempted: number; succeeded: number; failed: number; skipped: number };
  mirror?: PushMirrorPlan;
}

export interface PushOrdersParams {
  symbols: string[];
  side: PushSide;
  channels: PushChannel[];
  /** 打开确认面板时生成一次；幂等键的一部分，重复点击不重复下单 */
  batch_id: string;
  /** {symbol: 股数} 手填覆盖（键后缀/前缀/裸码都认） */
  quantities?: Record<string, number>;
  ack_risk?: boolean;
  dry_run?: boolean;
}
