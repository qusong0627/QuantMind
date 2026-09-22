/** 因子报告（Alphalens 式）类型定义 —— 与 backend/services/engine/factor_report 对齐 */

export interface FactorSummary {
  name: string;
  /** 因子库：alpha158 / alpha101 / gtja191 / L1 / L2 */
  library: string;
  /** 中文名（来自平台因子字典，可能为空） */
  display_name?: string | null;
  /** 中文分类（如「动量」「换手与流动性」「信息不对称与毒性」） */
  category_name?: string | null;
  ic_mean: number;
  icir: number;
  t_value: number;
  win_rate: number;
  /** 10 个分位的平均前瞻收益（升序：Q1 = 因子值最小） */
  quantiles: number[];
  /** 多空价差（Q10 − Q1） */
  ls_mean: number;
  /** 分位收益单调性（分位序号与收益的秩相关，±1 表示完美单调） */
  monotonicity: number | null;
  /**
   * **全截面换组比例**（逐日 decile 迁移比例），不是组合换手 ——
   * 组合换手在 `headline.turnover`（两条腿的 gt 均值）。
   */
  turnover: number;
  /** 各前瞻期的 IC（T+1/2/5/10/20）→ IC 衰减曲线；旧快照可能没有 */
  ic_by_horizon?: Record<string, number | null>;
  /** 各前瞻期的多空价差 */
  ls_by_horizon?: Record<string, number | null>;
  /** 默认分组（G3/G9）口径的 7 项指标 —— 指标环「全库百分位」的比较基准；旧快照没有 */
  headline?: FactorHeadlineSnapshot | null;
  /** 半截面 / 中性化 / 分域 IC 与数据质量（构建期列） */
  ic_top_mean?: number | null;
  ic_bot_mean?: number | null;
  ic_neutral_mean?: number | null;
  ic_neutral_days?: number;
  ic_domain?: Record<'large' | 'mid' | 'small', number | null>;
  clip_frac_mean?: number | null;
  n_valid_mean?: number | null;
  ic_half_life?: number | null;
  /** Barra 风格相关性（构建期列，风格清单见 style_block.styles） */
  style_corr?: Record<string, number | null> | null;
}

/** 报告页可选的数据集及其快照状态 */
export interface FactorDatasetInfo {
  dataset: string;
  label: string;
  available: boolean;
  horizon?: string | null;
  n_factors?: number | null;
  start?: string | null;
  end?: string | null;
  generated_at?: string | null;
}

export interface FactorDatasetList {
  default: string;
  items: FactorDatasetInfo[];
}

export interface FactorReportMeta {
  generated_at: string;
  dataset?: string;
  horizon: string;
  label_mode?: string;
  start: string;
  end: string;
  n_dates: number;
  n_factors: number;
  universe: string;
  step?: number;
  elapsed_sec?: number;
}

export interface FactorSummaryResponse {
  available: boolean;
  reason?: string;
  dataset?: string;
  meta?: FactorReportMeta;
  total?: number;
  factors: FactorSummary[];
}

export interface FactorDetail {
  dataset?: string;
  factor: string;
  horizon: string;
  empty: boolean;
  reason?: string;
  source?: 'series_snapshot' | 'partition_scan';
  /** 快照结构版本；缺字段的旧快照为 1 或 null → 页面提示「需重跑构建」 */
  schema_version?: number | null;
  dates: string[];
  quantile_mean: number[];
  quantile_curves: number[][];
  ls_curve: number[];
  ic_series: (number | null)[];
  ic_rolling: (number | null)[];
  ic_mean: number | null;
  ic_std: number | null;
  turnover_dates: string[];
  turnover_series: number[];
  turnover_mean: number | null;
  coverage_mean: number | null;
  n_dates: number;
  start: string;
  end: string;
  /** 机构级派生块（读时从逐日序列算出）。派生失败时是 `{error}`，不阻塞旧字段。 */
  blocks?: FactorBlocks | FactorBlocksError | null;
}

/** 派生层整体失败（新代码异常）——旧字段照常可用，页签显示原因 */
export interface FactorBlocksError {
  error: string;
}

/** 降级块的统一形态：**必须带 reason**，不用 0 或空数组冒充 */
export interface DegradedBlock {
  available: false;
  reason: string;
}

export interface HistBlock {
  bin_edges: number[];
  counts: number[];
  n: number;
}

export interface BootstrapCI {
  lo: number | null;
  hi: number | null;
  point: number | null;
  level: number;
  n_boot: number;
  stat: string;
}

// ─────────────────────────── 7 指标环 ───────────────────────────

/**
 * 7 指标环的数值。`turnover` 是**两条腿的组合换手**（gt 列），
 * 与 `FactorSummary.turnover`（全截面换组比例）不是一回事，勿混用。
 */
export interface HeadlineBlock {
  available: true;
  long_group: number;
  short_group: number;
  cost_bps: number;
  n_dates: number;
  /** 毛口径（与 WorldQuant BRAIN 参考形态对齐） */
  returns: number | null;
  ir: number | null;
  fitness: number | null;
  margin: number | null;
  turnover: number | null;
  cum_return: number | null;
  mu_daily: number | null;
  sigma_daily: number | null;
  ann_vol: number | null;
  /** 净口径：扣掉 cost_bps 后的同一套指标 */
  net_returns: number | null;
  net_ir: number | null;
  net_fitness: number | null;
  net_cum_return: number | null;
  ic: number | null;
  ic_std: number | null;
  icir: number | null;
}

/** 快照里逐因子预存的默认口径 headline —— 指标环的「全库百分位」基准 */
export interface FactorHeadlineSnapshot {
  returns: number | null;
  ir: number | null;
  turnover: number | null;
  fitness: number | null;
  margin: number | null;
  n_days: number;
  long_group: number;
  short_group: number;
}

// ─────────────────────────── 显著性 ───────────────────────────

export interface SignificanceBlock {
  available: true;
  /** 普通 t：ICIR × √n（IC 有自相关时会高估显著性） */
  t_value: number | null;
  /** Newey-West 调整 t；`nw_shrunk` 为真说明普通 t 明显虚高 */
  nw_t_value: number | null;
  nw_lag: number | null;
  p_value: number | null;
  /** Benjamini–Yekutieli 多重检验校正后的 q 值（≥ p） */
  q_value_bhy: number | null;
  n_factors_tested: number | null;
  nw_shrunk: boolean;
  deflated_sharpe: number | null;
  bootstrap_ic_mean: BootstrapCI | null;
  bootstrap_ir: BootstrapCI | null;
}

// ─────────────────────────── IC ───────────────────────────

export interface IndependenceBlock {
  max_corr: number | null;
  mean_corr_top: number | null;
  n_peers: number;
  peers: Array<{ name: string; corr: number }>;
  note: string;
}

export interface IcBlock {
  available: true;
  /** 噪声型序列：服从 lookback */
  dates: string[];
  ic_series: (number | null)[];
  ic_rolling: (number | null)[];
  ic_rolling_long: (number | null)[];
  /** 累计型序列：**全窗口**（lookback 会截断掉「累计」的意义） */
  ic_cum_full: (number | null)[];
  cum_dates_full: string[];
  cum_ic_top_full: (number | null)[] | null;
  cum_ic_bot_full: (number | null)[] | null;
  ic_mean: number | null;
  ic_std: number | null;
  win_rate: number | null;
  ic_top_mean: number | null;
  ic_bot_mean: number | null;
  ic_neutral_mean: number | null;
  /** 中性化 IC 的有效天数：天数少时均值无意义，页面必须一并显示 */
  ic_neutral_days: number;
  ic_neutral_series: (number | null)[] | null;
  ic_domain: Record<'large' | 'mid' | 'small', number | null>;
  ic_domain_series: Record<'large' | 'mid' | 'small', (number | null)[] | null>;
  ic_hist: HistBlock | null;
  /** lag1..lag20 的 IC 自相关 */
  ic_autocorr: (number | null)[];
  /** 各前瞻期的 IC 均值：键是 1/2/5/10/20 */
  decay: Record<string, number | null>;
  independence: IndependenceBlock | null;
  clip_frac_mean: number | null;
  n_valid_mean: number | null;
  half_life_days: number | null;
  ir_rolling_full: (number | null)[];
  monotonicity: number | null;
}

// ─────────────────────────── 分组回测 ───────────────────────────

export interface DrawdownEpisode {
  i0: number;
  i1: number;
  dd: number;
  days: number;
  recovered: boolean;
  start: string | null;
  end: string | null;
}

export interface LsDistribution {
  n: number;
  mu: number | null;
  sigma: number | null;
  skew: number | null;
  kurt: number | null;
  bin_edges: number[];
  counts: number[];
  var_95: number | null;
  cvar_95: number | null;
  var_99: number | null;
  cvar_99: number | null;
}

export interface LsMonthly {
  years: number[];
  months: number[];
  /** matrix[年下标][月下标]，1 月 = 下标 0；无数据为 null */
  matrix: (number | null)[][];
}

export interface HoldingRow {
  hold_days: number;
  gross_return: number | null;
  net_return: number | null;
  turnover: number | null;
  ir: number | null;
}

/**
 * 可交易轨 vs 理想轨。
 * ⚠️ `lost_return` 表达的是**理想口径高估了多少**，不是「策略会亏这么多」。
 */
export interface TradableBlock {
  available: boolean;
  reason?: string;
  n_days?: number;
  /** 日收益序列的轴（窗口） */
  dates?: string[];
  /** 累计曲线（`ls_cum`）的轴：**全窗口**，与理想轨同轴才能对照 */
  cum_dates_full?: string[];
  ls_daily?: (number | null)[];
  ls_cum?: (number | null)[];
  ls_dd?: number | null;
  /** 被涨跌停/停牌挡掉的天数（计数，不是逐日序列） */
  blocked_days?: number;
  blocked_long_total?: number;
  blocked_short_total?: number;
  ideal_cum_end?: number | null;
  tradable_cum_end?: number | null;
  lost_return?: number | null;
  note?: string;
}

export interface GroupBlock {
  available: true;
  long_group: number;
  short_group: number;
  groups: number[];
  dates: string[];
  group_daily_mean: (number | null)[];
  /** 累计型序列的轴：**全窗口**。与 `dates`（窗口）不是同一根轴，混用会错位 */
  cum_dates_full?: string[];
  /** 多头腿（G{long_group}）自身日收益 */
  long_daily: (number | null)[];
  /** 空头腿（G{short_group}）**作为多头持有**的日收益 —— 做空 P&L 需取负并复利 */
  short_daily: (number | null)[];
  /** 多空组合日收益 = 0.5×(多头腿 − 空头腿)，毛收益 */
  ls_daily: (number | null)[];
  /** 以下累计序列均为**全窗口**，横轴用 `cum_dates_full` */
  long_cum: (number | null)[];
  /** 空头腿作为**多头**持有的累计 —— 不是做空口径 */
  short_cum: (number | null)[];
  /** 空头腿的**做空 P&L** 累计 ∏(1−r) —— 与「取负的 short_cum」不是一回事 */
  short_book_cum?: (number | null)[];
  /** 多空组合累计净值 */
  ls_cum: (number | null)[];
  long_dd: number | null;
  short_dd: number | null;
  ls_dd: number | null;
  ls_dd_episodes: DrawdownEpisode[];
  /** G1..G10 各自的日均换手 */
  group_turnover: (number | null)[];
  turnover_ls: number | null;
  ls_dist: LsDistribution | null;
  ls_monthly: LsMonthly | null;
  holding_sweep: HoldingRow[];
  tradable: TradableBlock;
}

// ─────────────────────────── 成本与容量 ───────────────────────────

export interface CostRow {
  bps: number;
  net_return: number | null;
  net_ir: number | null;
  net_fitness: number | null;
}

/** ⚠️ 简化模型：`assumed_participation` 是**假设值**，必须原样呈现，不给裸数字 */
export interface CapacityBlock {
  est_aum: number | null;
  assumed_participation: number;
  median_amount: number | null;
  n_positions: number | null;
  turnover: number | null;
  note: string;
  median_amount_scope: string;
}

export interface CostBlock {
  available: boolean;
  reason?: string;
  sensitivity: {
    rows: CostRow[];
    /** 净 IR 归零的 bps。毛收益为负时该值为负 —— 见 break_even_note，别当 bug 渲染 */
    break_even_bps: number | null;
    /** 毛收益为负时的解释文案（此时刻意的「负盈亏平衡点」含义） */
    break_even_note?: string | null;
    default_bps: number;
  };
  capacity?: CapacityBlock;
}

// ─────────────────────── 相对基准超额 ───────────────────────

export interface ExcessStats {
  tracking_error: number | null;
  information_ratio: number | null;
  beta: number | null;
  corr: number | null;
  annual_excess: number | null;
  n_days: number;
}

export interface AnnualExcess {
  year: number;
  n_days: number;
  long_ret: number | null;
  bench_ret: number | null;
  excess: number | null;
  ls_ret: number | null;
}

export interface BenchmarkExcess {
  symbol: string;
  name: string;
  available: boolean;
  /** 取不到时必须给原因，**不静默换成别的基准** */
  reason?: string;
  n_days?: number;
  long_excess_cum?: (number | null)[];
  long_excess_dd?: number | null;
  excess_annual?: number | null;
  excess_stats?: ExcessStats;
  excess_hist?: HistBlock | null;
  top_drawdowns?: DrawdownEpisode[];
  annual?: AnnualExcess[];
}

export interface ExcessBlock {
  available: boolean;
  reason?: string;
  /** 实际采用的基准（可能与用户点名的不一致 —— 那时被点名项会在 benchmarks[] 里带 reason） */
  bench_symbol?: string;
  bench_name?: string;
  benchmarks?: BenchmarkExcess[];
  /**
   * 累计序列（累计超额 / 超额回撤 / 回撤区间 / 分年度）的轴：**全窗口**。
   * 日频的 `ls_dates`/`ls_daily` 另走窗口口径，两者不可互换。
   */
  dates?: string[];
  long_excess_cum?: (number | null)[];
  long_excess_dd?: number | null;
  excess_annual?: number | null;
  excess_stats?: ExcessStats;
  excess_hist?: HistBlock | null;
  top_drawdowns?: DrawdownEpisode[];
  annual?: AnnualExcess[];
  cvar_95?: number | null;
  cvar_99?: number | null;
  /** 多空日收益的轴（窗口） */
  ls_dates?: string[];
  ls_daily?: (number | null)[];
}

// ─────────────────────────── 风格 ───────────────────────────

export interface StyleExposureRow {
  rank: number;
  style: string;
  label: string;
  mean_corr: number | null;
  std_corr: number | null;
  n_days: number;
}

/** 收益对风格纯因子收益的时序回归 —— 回答「超额是真本事还是风格 beta」 */
export interface StyleAttribution {
  alpha: number | null;
  t_alpha: number | null;
  betas: Array<{ style: string; beta: number | null; t: number | null }>;
  r_squared: number | null;
  n: number;
  note: string;
}

export interface StyleBlock {
  available: boolean;
  reason?: string;
  /** 风格目录（后端按 STYLE_NAMES 顺序给出 key → 中文名）；口径声明据此列举，前端不自持清单 */
  styles?: Array<{ key: string; label: string }>;
  exposures?: StyleExposureRow[];
  /** 风格 → 逐日相关性序列（与 dates 同长） */
  exposure_ts?: Record<string, (number | null)[]>;
  dates?: string[];
  excess_corr?: Array<{ style: string; label: string; corr: number | null; n_days: number }>;
  /** `excess_corr` 为空时的**具体**原因（基准取不到 / 风格产物缺失 / 天数不足） */
  excess_corr_reason?: string;
  attribution?: StyleAttribution | null;
  attribution_reason?: string;
  excess_attribution?: StyleAttribution | null;
  n_attribution_days?: number;
}

// ─────────────────────────── 稳健性 ───────────────────────────

export interface SubPeriod {
  i0: number;
  i1: number;
  n: number;
  ic_mean: number | null;
  icir: number | null;
  start: string | null;
  end: string | null;
}

export interface RobustBlock {
  available: boolean;
  reason?: string;
  sub_period?: SubPeriod[];
  ic_stability?: number | null;
  oos?: {
    in_sample_ic: number | null;
    out_sample_ic: number | null;
    decay: number | null;
    note: string;
  };
  crowding?: {
    score: number | null;
    turnover_pct: number | null;
    ic_autocorr_lag1: number | null;
    n_days: number;
    note: string;
  };
  regime?: Array<{ regime: string; n_days: number; ls_mean_daily: number | null; ls_annual: number | null }>;
}

/** 详情接口的派生块集合。每个块都可能整体降级（`available:false` + `reason`）。 */
export interface FactorBlocks {
  headline: HeadlineBlock | DegradedBlock;
  significance: SignificanceBlock | DegradedBlock;
  ic_block: IcBlock | DegradedBlock;
  group_block: GroupBlock | DegradedBlock;
  cost_block: CostBlock;
  excess_block: ExcessBlock;
  style_block: StyleBlock;
  robust_block: RobustBlock;
  /** 口径说明（key → 文案）。前端 ⓘ 悬停直接读这里，避免前后端各写一份文案而漂移 */
  definitions: Record<string, string>;
}

/** 详情请求的可配参数 */
export interface FactorDetailParams {
  horizon?: string;
  lookback?: number;
  /** 多头组（G1 = 因子值最小 … G10 = 因子值最大） */
  longGroup?: number;
  shortGroup?: number;
  /** 双边成本（bps） */
  costBps?: number;
  /** 主基准指数代码，如 000300.SH；缺省沪深300 */
  bench?: string;
}

export interface FactorCorrelation {
  available: boolean;
  reason?: string;
  factors: string[];
  matrix: number[][];
}

export interface FactorRelated {
  factor: string;
  related: Array<{ name: string; corr: number }>;
}

/** 去重簇成员 */
export interface FactorClusterMember {
  name: string;
  display_name?: string | null;
  library?: string | null;
  ic_mean?: number | null;
  icir?: number | null;
  turnover?: number | null;
  /** 与簇代表的相关性（反向同源为负） */
  corr_to_rep: number;
  is_rep: boolean;
}

/** 同源因子簇 */
export interface FactorCluster {
  size: number;
  representative: string;
  representative_display?: string | null;
  representative_icir?: number | null;
  representative_ic_mean?: number | null;
  members: FactorClusterMember[];
}

export interface FactorClusterSummary {
  n_total: number;
  n_clusters: number;
  n_duplicates: number;
  n_keep: number;
  largest_cluster: number;
}

export interface FactorClusterResponse {
  available: boolean;
  dataset?: string;
  reason?: string;
  threshold?: number;
  keep?: string;
  summary?: FactorClusterSummary;
  clusters?: FactorCluster[];
}

/** 推荐因子组合（训练勾选的数据来源） */
export interface PortfolioFactor {
  name: string;
  display_name?: string | null;
  library?: string | null;
  weight: number;
  direction: number;
  ic_mean?: number | null;
  icir?: number | null;
  turnover?: number | null;
  coverage?: number | null;
  net_daily?: number | null;
}

export interface FactorPortfolioResponse {
  available: boolean;
  dataset?: string;
  reason?: string;
  generated_at?: string;
  rule?: Record<string, number | boolean | string>;
  summary?: {
    n_universe: number;
    n_passed: number;
    n_selected: number;
    n_rejected: number;
    composite_ic: number | null;
    composite_icir: number | null;
    single_icir_avg: number;
  };
  factors?: PortfolioFactor[];
  rejected?: Array<{ name: string; reason: string }>;
}
