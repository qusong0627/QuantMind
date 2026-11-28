/**
 * 组合选股（model blend）类型契约。
 *
 * 与 `backend/services/api/routers/model_blends.py` 一一对应；字段名按后端原样，
 * **不在前端改名**——改名会制造第二套词表，后端一加字段两边就漂。
 *
 * 合规边界：这里的 `position` 是后端 `BUY/HOLD/SELL` 枚举（位置判定），
 * 展示前必须过 `inference-center-shared/positionLabels.ratingPositionLabel`
 * 转成「靠前 / 居中 / 靠后」；组件里不得直接渲染该枚举。
 */

export type BlendMethod = 'mean' | 'median' | 'trimmed';
export type BlendPurpose = 'rebalance' | 'next_day';

/** 位置枚举：**内部值**，展示面必须经 `ratingPositionLabel` 转中性词 */
export type BlendPosition = 'BUY' | 'HOLD' | 'SELL' | string;

/** 分歧档位由后端唯一口径给出（`blend_engine.dispersion_band`） */
export type DispersionBand = 'low' | 'medium' | 'high' | 'unknown';

export interface BlendMemberRef {
  model_id: string;
  model_source: string;
  weight: number;
}

export interface BlendRecord {
  blend_id: string;
  tenant_id: string;
  user_id: string;
  name: string;
  market: string;
  purpose: BlendPurpose;
  source: 'manual' | 'system';
  template_id: string | null;
  horizon_days: number;
  weighting: 'equal' | 'manual';
  method: BlendMethod;
  trim_count: number;
  min_coverage: number;
  consensus_min_count: number;
  entry_per_day: number;
  rolling_window: number;
  target_positions: number;
  feature_signature: string;
  data_source: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  /** `list_blends` 带成员；`create_blend` 返回的也带 */
  members?: BlendMemberRef[];
}

/** 榜单历史标注：与纸面台账/近 10 日 TopK 同源，缺失时全 null / 0 */
export interface BlendHistory {
  first_entry_date: string | null;
  last_entry_date: string | null;
  reentry_count: number;
  held_days: number;
  consecutive_days_in_top50: number;
}

export interface BlendRankingRow {
  code: string;
  symbol: string;
  name: string;
  /** 原始组合分（成员分位的加权平均，0–1） */
  S: number;
  S_pct: number;
  score: number;
  pct: number;
  rank: number;
  /** 对外分数刻度（0–100，一位小数），缺失为 null → 渲染 `—` */
  research_score: number | null;
  position: BlendPosition;
  coverage: number;
  dispersion: number | null;
  dispersion_band: DispersionBand;
  /** 成员贡献（分位口径），键为 model_id */
  contributions: Record<string, number>;
  missing_members: string[];
  history?: BlendHistory;
  /** 以下为后端未来可能补充的可选列（如行业/市值）；当前通常缺省 */
  industry?: string | null;
  market_cap_yi?: number | null;
}

export interface BlendRankingMember {
  model_id: string;
  display_name: string;
  model_source: string;
  model_type: string;
  horizon_days: number;
  weight: number;
  available: boolean;
  run_id?: string | null;
}

export interface BlendRankingStats {
  scored?: number;
  coverage?: number;
  dispersion?: Record<DispersionBand, number>;
  [key: string]: unknown;
}

export interface BlendRankingResponse {
  blend_id?: string;
  data_trade_date: string;
  members: BlendRankingMember[];
  ranking: BlendRankingRow[];
  stats?: BlendRankingStats;
  warnings: string[];
  blacklist?: { applied: boolean; removed?: number; excluded?: number };
}

export interface BlendDatesResponse {
  blend_id: string;
  dates: string[];
}

/**
 * 历史数据端口：`pred` = 成员模型 `pred.parquet` 测试集（可回到 2025），
 * `runs` = `engine_signal_scores` 推理台账（通常只到最近一次串行补推）。
 * 整组合成员齐全才走 pred，否则整体降级 runs，**绝不混源**。
 */
export type BlendDataSource = 'pred' | 'runs';

export interface BlendBoundsSpan {
  start: string | null;
  end: string | null;
  trading_days: number;
}

export interface BlendPredBounds extends BlendBoundsSpan {
  members_ready: number;
  members_total: number;
}

/** 组合历史可用区间（`/bounds` 与 rolling/period 响应内嵌同形） */
export interface BlendBounds {
  source: BlendDataSource;
  pred: BlendPredBounds | null;
  runs: BlendBoundsSpan;
  effective: BlendBoundsSpan;
  /** 成员级降级码：`member_unavailable:<id>` / `member_pred_unavailable:<id>` … */
  warnings: string[];
}

export interface BlendBoundsResponse extends BlendBounds {
  blend_id: string;
}

/** 基准徽标：只描述数据可用性，不重复行情序列 */
export interface BlendBenchmark {
  symbol: string | null;
  name: string | null;
  available: boolean;
  days: number;
}

export type BlendMarketTone = 'up' | 'down' | 'flat' | 'unknown';
export type BlendMarketPosition = 'above_ma20' | 'below_ma20' | 'unknown';

/** 当日大盘（沪深 300）：涨跌幅、收盘、相对 20 日均线位置 */
export interface BlendMarketDay {
  change_pct: number | null;
  close: number | null;
  ma20_deviation_pct: number | null;
  position: BlendMarketPosition;
  tone: BlendMarketTone;
}

/** 单只标的在窗口内的统计（按窗口内出现过的每个标的给一份） */
export interface BlendRollingCellStats {
  /** 窗口内在榜天数 */
  appearances: number | null;
  /** 当前连续在榜天数（断榜重计） */
  streak_days: number | null;
  /** 窗口内首次入选日 */
  first_seen: string | null;
  /** 首次入选日收盘 → 末日收盘的涨跌（%） */
  since_first_pct: number | null;
  /** 在榜日次日收益均值（%） */
  next_day_avg_pct: number | null;
  /** 在榜日次日上涨占比（%） */
  next_day_win_rate_pct: number | null;
  /** 次日收益样本数 */
  next_day_samples: number | null;
  /** 次日收益标准差（样本 <3 时为 null，不硬凑） */
  next_day_std_pct: number | null;
  /** 末日收盘价 */
  latest_close: number | null;
  /** 末日当日涨跌幅（%） */
  change_pct: number | null;
}

/** 当日批次的建仓状态（可交易性口径） */
export type BlendRollingBuyStatus =
  | 'bought'
  | 'next_day'
  | 'skip_st'
  | 'skip_low_adv'
  | 'skip_no_amount'
  | 'skip_no_price';

/** 滚动建仓：每列一日、每列 Top `per_day`；`exits` 只在「昨列→今日」标注 */
export interface BlendRollingPick {
  symbol: string;
  code?: string;
  name: string;
  score: number;
  rank: number;
  row_rank: number;
  coverage: number;
  pct: number;
  position: BlendPosition;
  /** 本窗口内连续在榜天数（断榜后重新计数，不伪造跨窗连续） */
  held_days: number;
  is_new: boolean;
  industry?: string | null;
  market_cap_yi?: number | null;
  /** 当日涨跌幅（%）；缺行情为 null → `—` */
  change_pct?: number | null;
  /** 当日持仓权重（%）：仅前一交易日已在榜（当日实际持有）才有值 */
  weight_pct?: number | null;
  /** 当日盈亏金额（元）：仅当日实际持有的标的才有值 */
  day_pnl_amount?: number | null;
  /** 当日批次的建仓状态；旧后端缺失为 undefined */
  buy_status?: BlendRollingBuyStatus | null;
  /** 计划建仓权重（%）：跳过者为 0，末日候选为次日计划权重 */
  entry_weight_pct?: number | null;
  /** 入选日 ADV20（亿元）；缺失为 null */
  adv_yi?: number | null;
  /** 窗口内表现统计（在榜天数 / 首次至今 / 次日收益等） */
  stats?: BlendRollingCellStats | null;
}

/** 一笔已了结的滚动持仓（收盘价买入 → 退出日收盘价卖出） */
export interface BlendRollingExit {
  symbol: string;
  code?: string;
  name: string;
  industry?: string | null;
  market_cap_yi?: number | null;
  entry_date: string;
  exit_date: string;
  entry_price: number | null;
  exit_price: number | null;
  /** 收盘价口径收益（%）；缺行情为 null（展示 —，绝不写 0） */
  return_pct: number | null;
  /** 本窗口内在榜天数（与卡片 held_days 同口径） */
  held_days: number;
  /** 入场日 = 窗口首列（更早的入场日不可见，按窗口起点计） */
  entry_from_window_start: boolean;
}

export interface BlendRollingColumn {
  date: string;
  /** 当日大盘（沪深 300）；缺数据时各字段为 null / unknown */
  market?: BlendMarketDay;
  picks: BlendRollingPick[];
  /** 兼容旧字段：退出代码列表 */
  exits: string[];
  /** 退出明细（含收盘价损益）；旧后端可能缺失 */
  exit_rows?: BlendRollingExit[];
}

/** 一个每日批次：入选日收盘按全部权益等权建仓 → 次一交易日收盘结算 */
export interface BlendRollingBatch {
  entry_date: string;
  exit_date: string;
  /** 当日候选只数 */
  positions: number;
  /** 实际建仓只数（ST/退市、成交额不足等不建仓）；旧后端缺失 */
  bought?: number;
  priced: number;
  return_pct: number | null;
  notional_amount: number;
  pnl_amount: number | null;
  equity: number;
  /** false = 最后一批，仅末日估值（计入浮动），不结算 */
  settled: boolean;
  holding_days: number;
}

/** 逐日净值（组合与基准同日对照） */
export interface BlendRollingDaily {
  date: string;
  exit_date: string;
  return_pct: number | null;
  equity: number;
  market_value: number;
  cash: number;
  exposure_pct: number | null;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
  settled: boolean;
}

/**
 * 滚动窗口的每日批次口径绩效（收盘价、等权、未计费用）。
 *
 * 恒等式：`realized + unrealized == final_equity − initial_capital`。
 * 最后一批只估值不结算，所以「已实现」= 倒数第二个交易日结算后的权益差，
 * 「浮动」= 末日估值 − 倒数第二个交易日权益。
 */
export interface BlendRollingPerformance {
  initial_capital: number;
  final_equity: number;
  profit_amount: number;
  total_return_pct: number | null;
  /** 与 total_return_pct 同值，保留旧字段名 */
  cycle_return_pct: number | null;
  realized_pnl_amount: number;
  realized_return_pct: number | null;
  unrealized_pnl_amount: number;
  unrealized_return_pct: number | null;
  /** 基准覆盖率不足 95% 时基准/超额/IR 一律 null（不写 0） */
  benchmark_return_pct: number | null;
  benchmark_coverage_pct: number | null;
  excess_return_pct: number | null;
  annualized_return_pct: number | null;
  volatility_pct: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  max_drawdown_pct: number | null;
  drawdown_peak_date: string | null;
  drawdown_trough_date: string | null;
  drawdown_recovery_date: string | null;
  drawdown_recovery_days: number | null;
  tracking_error_pct: number | null;
  information_ratio: number | null;
  avg_daily_excess_pct: number | null;
  /** 已结算批次（笔） */
  trades: number;
  wins: number;
  losses: number;
  flats: number;
  win_rate_pct: number | null;
  avg_return_pct: number | null;
  avg_win_pct: number | null;
  avg_loss_pct: number | null;
  payoff_ratio: number | null;
  profit_factor: number | null;
  gross_profit_amount: number | null;
  gross_loss_amount: number | null;
  avg_holding_days: number | null;
  turnover_x: number | null;
  avg_exposure_pct: number | null;
  final_cash_pct: number | null;
  trading_days: number;
  batches: number;
  settled_batches: number;
  open_batches: number;
  priced_batches: number;
  /** 有建仓但算不出收益的批次数（真缺价）；跳过全部标的的批次不计入 */
  unpriced_batches: number;
  /** 已建仓标的中部分缺价的批次数 */
  partial_batches: number;
  /** 全部标的被跳过、实际未建仓的批次数；旧后端缺失 */
  no_position_batches?: number;
  cycle_days: number;
  cycle_priced_days: number;
  cycle_win_days: number;
  cycle_loss_days: number;
  entry_convention: string;
  /** 是否实际应用成交额约束；旧后端缺失 */
  capacity_applied?: boolean;
  /** 是否实际应用 ST/退市过滤；旧后端缺失 */
  st_filter_applied?: boolean;
  /** 各批次候选标的合计（不含末列） */
  evaluated_picks?: number;
  /** 实际建仓标的合计 */
  bought_positions?: number;
  skipped_st?: number;
  skipped_low_adv?: number;
  skipped_no_amount?: number;
  skipped_no_price?: number;
  min_adv_yi?: number;
  adv_participation?: number;
  adv_lookback_days?: number;
  min_adv_days?: number;
}

/** 本轮可交易性参数与数据源状态 */
export interface BlendRollingFilters {
  market?: string;
  exclude_st?: boolean;
  /** ST/退市过滤是否生效（数据源故障时为 false） */
  st_applied?: boolean;
  st_error?: string | null;
  /** 成交额约束是否生效 */
  capacity_applied?: boolean;
  min_adv_yi?: number;
  adv_participation?: number;
  adv_lookback_days?: number;
  min_adv_days?: number;
}

export interface BlendRollingSummary {
  unique_symbols: number;
  new_today: number;
  exited_today: number;
  /** 旧后端可能缺失 */
  performance?: BlendRollingPerformance;
}

export interface BlendRollingResponse {
  blend_id: string;
  data_trade_date: string;
  target_date: string;
  window: number;
  per_day: number;
  /** 批次本金（元）；旧后端缺失时按默认 1000 万理解 */
  capital?: number;
  source?: BlendDataSource;
  bounds?: BlendBounds;
  benchmark?: BlendBenchmark;
  days: string[];
  columns: BlendRollingColumn[];
  /** 每日批次（含未结算批） */
  batches?: BlendRollingBatch[];
  /** 逐日净值与基准对照 */
  daily?: BlendRollingDaily[];
  members: BlendRankingMember[];
  summary: BlendRollingSummary;
  /** 本轮可交易性参数与数据源状态；旧后端缺失 */
  filters?: BlendRollingFilters;
  warnings: string[];
}

// ── 周期收益（固定持有期滚动桶，收盘口径无费用）──────────────────────

export interface BlendPeriodRequested {
  start: string;
  end: string;
  capital: number;
  positions: number;
}

/** 实际生效区间：请求区间被可用评分日夹取后的结果 */
export interface BlendPeriodEffective {
  start: string;
  end: string;
  first_scoring_day: string;
  last_scoring_day: string;
}

export interface BlendPeriodTemplate {
  entry_per_day: number;
  hold_days: number;
  target_positions: number;
  method: BlendMethod;
}

export type BlendPeriodDataQuality = 'complete' | 'partial';

export interface BlendPeriodSummary {
  initial_capital: number;
  final_equity: number;
  total_return_pct: number | null;
  profit_amount: number;
  /** 已了结交易累计损益（元） */
  realized_pnl_amount: number;
  /** 已实现收益率（% = 已实现损益 ÷ 初始资金） */
  realized_return_pct: number | null;
  /** 期末在场浮动损益（元） */
  unrealized_pnl_amount: number;
  /** 浮动收益率（% = 浮动损益 ÷ 初始资金） */
  unrealized_return_pct: number | null;
  max_drawdown_pct: number | null;
  drawdown_peak_date: string | null;
  drawdown_trough_date: string | null;
  drawdown_recovery_date: string | null;
  drawdown_recovery_days: number | null;
  /** 基准（沪深 300）；覆盖率不足 95% 时为 null，绝不写 0 */
  benchmark_return_pct: number | null;
  benchmark_coverage_pct: number | null;
  excess_return_pct: number | null;
  annualized_return_pct: number | null;
  volatility_pct: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  tracking_error_pct: number | null;
  information_ratio: number | null;
  profit_factor: number | null;
  payoff_ratio: number | null;
  avg_win_pct: number | null;
  avg_loss_pct: number | null;
  gross_profit_amount: number | null;
  gross_loss_amount: number | null;
  avg_holding_days: number | null;
  /** 建仓名义金额合计 ÷ 初始资金（倍） */
  turnover_x: number | null;
  avg_exposure_pct: number | null;
  final_cash_pct: number | null;
  trading_days: number;
  scoring_days: number;
  entries: number;
  exits: number;
  closed_trades: number;
  open_positions: number;
  wins: number;
  losses: number;
  flats: number;
  win_rate_pct: number | null;
  avg_trade_return_pct: number | null;
  avg_positions: number;
  /** 固定口径：收盘价建仓/退出 */
  entry_convention: string;
  /** 固定口径：不计手续费/印花税/滑点 */
  fee_convention: string;
  data_quality: BlendPeriodDataQuality;
  scoring_coverage_pct: number;
}

export interface BlendPeriodMonth {
  month: string;
  start_date: string;
  end_date: string;
  start_equity: number;
  end_equity: number;
  return_pct: number | null;
  profit_amount: number;
  /** 本月新增已实现损益（元） */
  realized_pnl_amount: number;
  /** 月末浮动损益存量（元） */
  unrealized_pnl_amount: number;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
  entries: number;
  exits: number;
  closed_trades: number;
  month_end_positions: number;
  win_rate_pct: number | null;
  /** 区间首月/末月未覆盖整月 */
  partial: boolean;
}

export interface BlendPeriodDaily {
  date: string;
  equity: number;
  cash: number;
  market_value: number;
  /** 仓位（市值 ÷ 权益，%）；权益非正时为 null */
  exposure_pct: number | null;
  positions: number;
  entries: number;
  exits: number;
  realized_pnl_amount: number;
  unrealized_pnl_amount: number;
  return_pct: number | null;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
}

export interface BlendPeriodTrade {
  symbol: string;
  code: string;
  name: string;
  entry_date: string;
  exit_date: string;
  entry_price: number;
  exit_price: number;
  shares: number;
  notional: number;
  return_pct: number | null;
  pnl_amount: number;
  held_days: number;
  exit_reason: string;
  /** 到期日缺价顺延到下一个有效收盘价 */
  deferred: boolean;
}

export interface BlendPeriodOpen {
  symbol: string;
  code: string;
  name: string;
  entry_date: string;
  mark_date: string;
  entry_price: number;
  mark_price: number;
  shares: number;
  notional: number;
  return_pct: number | null;
  pnl_amount: number;
  held_days: number;
  /** 期末无有效收盘价，沿用上一有效价 */
  stale: boolean;
}

export interface BlendPeriodResponse {
  blend_id: string;
  /** 本次计算实际使用的数据端口（pred 测试集 / runs 推理台账） */
  source?: BlendDataSource;
  bounds?: BlendBounds;
  benchmark?: BlendBenchmark;
  requested: BlendPeriodRequested;
  effective: BlendPeriodEffective;
  template: BlendPeriodTemplate;
  summary: BlendPeriodSummary;
  months: BlendPeriodMonth[];
  daily: BlendPeriodDaily[];
  trades: BlendPeriodTrade[];
  open: BlendPeriodOpen[];
  /** 机读码：effective_start_clamped / scoring_gap / partial_data / … */
  warnings: string[];
  members: BlendRankingMember[];
}

export interface BlendPeriodQuery {
  start?: string;
  end?: string;
  capital?: number;
  positions?: number;
}

export interface BlendPlanConfig {
  capital: number;
  top_k: number;
  hold_days: number;
  entry_per_day: number;
  target_positions: number;
  cash_buffer: number;
  adv_participation: number;
  exit_delta: number;
  lookback: number;
  min_lookback_days: number;
  /** 新进最低 ADV20（亿元）；0 表示不设门槛 */
  min_adv_yi: number;
  /** 是否剔除 ST/退市（硬条件） */
  exclude_st: boolean;
  adv_lookback_days: number;
  min_adv_days: number;
}

export interface BlendPlanSummary {
  capital: number;
  target_positions: number;
  entry_per_day: number;
  hold_days: number;
  top_k: number;
  cash_buffer: number;
  /** 扣除现金缓冲后的可用资金（元） */
  capital_available?: number;
  /** 已占用资金（持仓现值 + 新进目标金额，元） */
  capital_used?: number;
  /** 剩余资金（元） */
  capital_remaining?: number;
  min_adv_yi?: number;
  exclude_st?: boolean;
  adv_lookback_days?: number;
  /** false=行情或风险标记缺失；此类计划只能预览，不能确认 */
  data_ok?: boolean;
  n_entries: number;
  n_holds: number;
  n_exits: number;
  n_open_after: number;
}

/** 计划行：新进(entries)/留存(holds)/退出(exits) 共用形状，字段按类别可缺 */
export interface BlendPlanRow {
  symbol: string;
  /** 简称/行业/市值（当前截面；缺失为 null → 渲染 `—`） */
  name?: string | null;
  /** 仅 holds/exits：纸面台账首入日期 */
  first_entry_date?: string;
  /** 仅 holds/exits：已持有交易日数 */
  held_days?: number;
  score?: number | null;
  pct?: number | null;
  research_score?: number | null;
  position?: BlendPosition | null;
  rank?: number | null;
  mean20: number | null;
  exit_line: number | null;
  exit_reason?: string;
  note?: string;
  target_value?: number;
  ref_price?: number;
  shares?: number;
  /** ADV20（亿元）；缺失为 null → 渲染 `—` */
  adv_yi?: number | null;
  industry?: string | null;
  market_cap_yi?: number | null;
}

export interface BlendPlan {
  plan_id: string | null;
  blend_id: string;
  data_trade_date: string;
  target_date: string;
  params: BlendPlanConfig;
  summary: BlendPlanSummary;
  entries: BlendPlanRow[];
  holds: BlendPlanRow[];
  exits: BlendPlanRow[];
  warnings: string[];
  /** false=行情或 ST/退市标记缺失；后端 confirm_plan 会拒绝确认 */
  data_ok?: boolean;
  config_hash?: string;
  positions_hash?: string;
  confirmed_at?: string | null;
  /** 后端标注的确认窗口（最近 2 个交易日）；null=日历不可用，前端退回 /dates 序位判断 */
  can_confirm?: boolean | null;
  created_at?: string | null;
  blend?: { blend_id: string; name?: string; method?: string; horizon_days?: number };
}

/** 计划覆盖参数：只发用户显式改过的字段（缺省沿用组合自身配置） */
export interface BlendPlanOverrides {
  capital?: number;
  top_k?: number;
  hold_days?: number;
  entry_per_day?: number;
  target_positions?: number;
  min_adv_yi?: number;
  exclude_st?: boolean;
}

export interface BlendPlanRequest extends BlendPlanOverrides {
  data_trade_date?: string;
  persist?: boolean;
}

// ── 系统自动识别（模板） ───────────────────────────────────────────────

export type EvidenceGrade = 'recommended' | 'watch' | 'unverified' | string;

export interface ComboTemplateMember {
  model_id: string;
  model_source: string;
  weight: number;
  model_type: string;
  display_name: string;
}

export interface ComboTemplate {
  template_id: string;
  kind: string;
  name: string;
  market: string;
  horizon_days: number | null;
  /** 默认调仓用途与滚动建仓参数（后端按周期派生，旧模板可能缺省） */
  purpose?: BlendPurpose;
  entry_per_day?: number;
  rolling_window?: number;
  target_positions?: number;
  method: BlendMethod;
  trim_count: number;
  min_coverage: number;
  consensus_min_count: number;
  members: ComboTemplateMember[];
  /** 因「从未有过有效推理」被排除的成员数；0/缺省表示成员齐全 */
  missing_members?: number;
  evidence: { grade: EvidenceGrade; candidate_id: string | null };
  /** 基线模板恒排第一（C0 = 全 13 · T+10 等权） */
  baseline: boolean;
}

export interface ComboAutoCandidatesResponse {
  market: string;
  items: ComboTemplate[];
  baseline_template_id: string | null;
  evidence_available: boolean;
  evidence_generated_at: string | null;
  excluded: { model_id: string; reason: string }[];
  warnings: string[];
}

export interface ComboEligibleModel {
  model_id: string;
  display_name: string;
  model_type: string;
  model_source: string;
  horizon_days: number | null;
  feature_count: number;
  status: string;
}

export interface ComboEligibleGroup {
  key: string;
  signature: Record<string, unknown>;
  models: ComboEligibleModel[];
}

export interface ComboEligibleResponse {
  market: string;
  groups: ComboEligibleGroup[];
  excluded: { model_id: string; reason: string }[];
  total_eligible: number;
}

// ── 证据包（只读 P1 产物，不现算） ─────────────────────────────────────

export interface EvidenceWindowStats {
  days?: number;
  hit_theory_mean?: number | null;
  hit_exec_mean?: number | null;
  ret_theory_mean?: number | null;
  ret_exec_net_mean?: number | null;
  hit_exec_top20?: number | null;
  n_scored_mean?: number | null;
  n_entry_mean?: number | null;
  full_top_ratio?: number | null;
  [key: string]: unknown;
}

export interface EvidenceBootstrap {
  mean: number | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_width: number | null;
  p_value: number | null;
  n_days?: number;
  n_blocks?: number;
}

export interface EvidenceMonthly {
  win_ratio: number | null;
  months?: Record<string, number>;
  candidate?: Record<string, number>;
}

export interface EvidenceGatePerformance {
  total_return?: number | null;
  annual_return?: number | null;
  max_drawdown?: number | null;
  sharpe?: number | null;
  trading_days?: number | null;
}

export interface EvidenceGate {
  performance?: EvidenceGatePerformance;
  avg_trade_return?: number | null;
  n_entries?: number | null;
  n_exits?: number | null;
  capacity_unchecked?: number | null;
  annual_delta?: number | null;
  drawdown_delta?: number | null;
  reference_annual?: number | null;
  reference_max_drawdown?: number | null;
}

export interface EvidenceCandidate {
  id: string;
  name: string;
  status: string;
  verdict: string;
  horizon?: number | null;
  method?: string;
  members: string[];
  windows?: Partial<Record<'selection' | 'validation' | 'test', EvidenceWindowStats>>;
  criteria?: {
    delta_test?: number | null;
    delta_validation?: number | null;
    checks?: Record<string, boolean>;
  };
  bootstrap?: EvidenceBootstrap | null;
  monthly?: EvidenceMonthly | null;
  bh_q?: number | null;
  gate?: EvidenceGate | Record<string, never>;
  leave_one_out?: { max_delta?: number | null; member?: string; deltas?: Record<string, number> } | null;
  notes?: string[];
  failed_checks?: string[];
  [key: string]: unknown;
}

export interface EvidenceRecommendation {
  ids: string[];
  top: string | null;
  reason: string;
}

export interface BlendEvidenceResponse {
  available: boolean;
  path: string;
  schema_version?: number;
  generated_at?: string;
  recommendation?: EvidenceRecommendation;
  kill_criteria?: Record<string, Record<string, unknown>>;
  caveats?: string[];
  candidates?: EvidenceCandidate[];
  /** 证据包实际形状：`selection: ["2025-02-05", "2025-12-31"]` */
  splits?: Record<string, [string, string] | { start?: string; end?: string }>;
  [key: string]: unknown;
}

// ── 请求体 ─────────────────────────────────────────────────────────────

export interface BlendPreviewRequest {
  market?: string;
  model_ids: string[];
  data_trade_date?: string;
  purpose?: BlendPurpose;
  method?: BlendMethod;
  trim_count?: number;
  top_k?: number;
  top?: number;
  min_coverage?: number;
  consensus_min_count?: number;
  weights?: Record<string, number> | null;
  exclude_blacklist?: boolean;
}

export interface BlendCreateRequest {
  name: string;
  market?: string;
  model_ids: string[];
  purpose?: BlendPurpose;
  method?: BlendMethod;
  trim_count?: number;
  min_coverage?: number;
  consensus_min_count?: number;
  entry_per_day?: number;
  rolling_window?: number;
  target_positions?: number;
  weights?: Record<string, number> | null;
  source?: 'manual' | 'system';
  template_id?: string | null;
}

export interface BlendPatchRequest {
  name?: string;
  purpose?: BlendPurpose;
  method?: BlendMethod;
  trim_count?: number;
  min_coverage?: number;
  consensus_min_count?: number;
  entry_per_day?: number;
  rolling_window?: number;
  target_positions?: number;
  model_ids?: string[];
  weights?: Record<string, number> | null;
}
