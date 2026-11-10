/** 因子研究（factor-lib-demo 复刻版）类型定义 —— 与 /api/v1/factor-research 响应一一对应 */

/**
 * 因子研究已接入的市场（全局 AppMarket 的子集）。
 * CRYPTO 不在内：工作台未接基准表（后端 422「未接入市场」），页面按诚实阻断面
 * 处理（绝不回退 CN 数据 —— 拿沪深300 给加密因子算超额是数据污染）。
 */
export type ResearchMarket = 'CN' | 'HK' | 'US' | 'FUTURES';

export interface FactorMeta {
  code: string;
  name_cn: string;
  /** 完整名称（私人因子库的英文全名；经典因子为空） */
  display_name?: string;
  l1: string;
  l2: string;
  /** 语义类别 id（私人库由后端 taxonomy 富化；经典库无此字段） */
  category?: string;
  /** 语义类别中文名（与 category 同时下发） */
  category_name?: string;
  /** 1=正（越大越好），-1=负 */
  direction: number;
  description: string;
  formula: string;
  wind_source: string;
  /** 静态标签（demo 原始口径；可用因子的实时标签见排行榜/单因子） */
  env_tag: string;
  time_tag: string;
  available: boolean;
  unavailable_reason: string;
}

export interface BenchRef {
  symbol: string;
  name: string;
}

export interface CatalogResponse {
  factors: FactorMeta[];
  l1_order: string[];
  l2_order: Record<string, string[]>;
  benchmarks: BenchRef[];
  /** 本响应所属市场（后端回执；CN 缺省也回传） */
  market?: ResearchMarket;
  meta: Record<string, unknown>;
}

export interface SeriesPoint {
  date: string;
  value: number;
}

export interface RangeMeta {
  start: string | null;
  end: string | null;
  n_months: number;
  /** 主库全窗口（快照边界；与当前切片 start/end 不同） */
  window?: [string, string] | null;
  lookback_start?: string | null;
  /** 方向校准窗：私有因子 sign 只在这里算一次，评估窗口只读 */
  direction_calibration?: { start: string; end: string; source?: string } | null;
  /** 历史窗口退市股覆盖缺口；true = 只能做研究，不得作为验收依据 */
  survivorship_bias?: boolean;
}

export interface FactorKpi {
  annual_return?: number | null;
  sharpe?: number | null;
  max_drawdown?: number | null;
  win_rate?: number | null;
  calmar?: number | null;
  n_months?: number;
  ic_mean?: number | null;
  ic_std?: number | null;
  ic_ir?: number | null;
  ic_win_rate?: number | null;
  /**
   * BRAIN 口径 Fitness = IR × √(|Returns| / max(Turnover, 0.125))。
   * 工作台为月频回测，后端已按日频口径折算（见 scorecard.brain_metrics）：
   * Returns=月均毛收益×12、IR=月均/标准差×√12、Turnover=月均单边换手/21。
   * 样本不足 6 个月为 null（不写 0）。
   */
  fitness?: number | null;
  /** Fitness 五档：卓越 / 优秀 / 良好 / 一般 / 需改进（后端世坤口径） */
  fitness_grade?: string | null;
  /** Fitness 可对账输入：简单年化毛收益（小数） */
  brain_returns?: number | null;
  /** Fitness 可对账输入：毛信息比率（月频×√12） */
  brain_ir?: number | null;
  /** Fitness 可对账输入：日均单边换手（月均换手/21；均摊假设） */
  brain_turnover?: number | null;
}

/**
 * 年化超额：**按基准代码映射**（2026-10 市场分层后为动态键——键集 = 本市场
 * 基准全表，如 CN 的 000300.SH/000906.SH/000905.SH、HK 的 HSI.HK…）。
 * 前端一律按当前市场的 benchmarks 清单取键，不得再写死 CN 三码。
 */
export type ExcessKpi = Record<string, number | null>;

export interface LeaderboardRow extends FactorKpi {
  rank: number;
  code: string;
  name_cn: string;
  l1: string;
  l2: string;
  /** 语义类别 id（私人库；经典库无） */
  category?: string;
  /** 语义类别中文名（与 category 同时下发；经典库为空串，显示回退 l2） */
  category_name?: string;
  composite: number;
  eff_z: number;
  perf_z: number;
  /** 按基准代码映射的年化超额（键集 = 本市场基准全表；无序列的基准为 null） */
  excess_by_bench?: Record<string, number | null>;
  /** CN 旧键（一期兼容；**前端统一读 excess_by_bench**；非 CN 下这些键不存在） */
  excess_300?: number | null;
  excess_800?: number | null;
  excess_500?: number | null;
  /** 最新截面 Top-N 持仓的中位总市值（亿；非 CN 无估值源时缺失） */
  median_mv_yi?: number | null;
  /** 市值风格：大盘（≥500亿）/ 中盘（100-500亿）/ 小盘（<100亿）；非 CN 多为 null */
  mv_style?: string | null;
  /** 最新截面 Top-N 持仓的前三行业（CN=申万二级；非 CN=该市场板块维度，缺源则空） */
  top_industries?: Array<{ name: string; count: number }> | null;
  env_tag: string;
  time_tag: string;
  /** 回测月数不足 6（年化等已置空，排在榜尾） */
  insufficient?: boolean;
  /** 疑似未来函数（|IC|>0.3 / |ICIR|>5，超真实因子上限；排在正常因子之后） */
  suspicious?: boolean;
}

/** 排行榜去重元信息（每次响应都下发；applied=false 表示本次未开启去重） */
export interface LeaderboardDedupMeta {
  applied: boolean;
  /** 剔除阈值（|ρ| ≥ threshold 视为同源重复） */
  threshold?: number;
  /** 去重前榜上行数 */
  n_before?: number;
  /** 本次剔除（同源重复）行数 */
  dropped?: number;
  /** 相关矩阵覆盖的因子数（未覆盖者不参与去重、必保留） */
  covered?: number;
}

/** 基准清单条目（leaderboard meta 口径：code+name；与 catalog 的 BenchRef symbol 键不同源，勿混用） */
export interface BenchMeta {
  code: string;
  name: string;
}

export interface LeaderboardResponse {
  leaderboard: LeaderboardRow[];
  meta: Record<string, unknown> & {
    range?: RangeMeta;
    dedup?: LeaderboardDedupMeta;
    /** 本响应所属市场 */
    market?: ResearchMarket;
    /** 本市场基准清单（榜单超额列的数据源；无基准市场为空表） */
    benchmarks?: BenchMeta[];
  };
}

/** 左侧目录选中的分类限定（l2 为 null = 整个大类）；null = 全部因子 */
export interface CategoryFilter {
  l1: string;
  l2: string | null;
  /** 语义类别限定（私人库树第三级；与 l2 互斥使用） */
  category?: string | null;
  /** 类别中文名（仅用于筛选芯片显示；过滤键是 category id） */
  categoryLabel?: string | null;
}

export interface StockRow {
  rank: number;
  symbol: string;
  name?: string | null;
  industry?: string | null;
  total_mv_yi?: number | null;
  pe_ttm?: number | null;
  pb?: number | null;
  avg_amount_yi?: number | null;
  score?: number | null;
  raw?: number | null;
}

export interface FactorVariant {
  n: number;
  kpi: FactorKpi;
  excess: ExcessKpi;
  nav: SeriesPoint[];
}

export interface BenchSeries {
  code: string;
  name: string;
  kpi?: FactorKpi;
  nav: SeriesPoint[];
}

export interface NScanRow {
  n: number;
  final_nav: number;
  annual_return: number;
}

export interface DistRow {
  name: string;
  count: number;
}

export interface FactorDetail {
  code: string;
  name_cn: string;
  display_name?: string;
  l1: string;
  l2: string;
  direction: number;
  description: string;
  formula: string;
  wind_source: string;
  /**
   * 来源库目录名（= 因子报告的数据集名，如 l1_factors / alpha_library）。
   * 后端读时由 discovery.source_of(wind_source) 现算；经典 demo 的
   * wind_source 是 Wind 字段名，反查为空串。「因子报告」跳转入口按它定位。
   */
  source_library?: string;
  available: boolean;
  /** 该因子在所选窗口内无数据时的可读原因（含数据起始日）；有数据时不出现 */
  unavailable_reason?: string;
  env_tag: string;
  time_tag: string;
  range: RangeMeta;
  variants: FactorVariant[];
  benchmarks: BenchSeries[];
  nscan: NScanRow[];
  ic: SeriesPoint[];
  ic_kpi: FactorKpi;
  stocks: StockRow[];
  /** 个股表截面日；该因子整段无数据时为 null */
  stocks_date: string | null;
  /** true = 面板末月无该因子数据，已回退到它自身最近一个有数据的截面 */
  stocks_stale?: boolean;
  industry_dist: DistRow[];
  cap_dist: DistRow[];
}

export interface CorrPair {
  factor_a: string;
  factor_b: string;
  corr: number | null;
}

export interface CompareItemReq {
  code: string;
  n: number;
}

export interface CompareFactor {
  code: string;
  name_cn: string;
  l1: string;
  l2: string;
  n: number;
  kpi: FactorKpi;
  excess: ExcessKpi;
  nav: SeriesPoint[];
  ic: SeriesPoint[];
}

export interface CompareResponse {
  factors: CompareFactor[];
  corr: CorrPair[] | null;
  benchmarks: BenchSeries[];
  range: RangeMeta;
}

export interface ComposeRequest {
  weights: Record<string, number>;
  top_n: number;
  threshold?: number | null;
  thresholds?: Record<string, number> | null;
  start?: string | null;
  end?: string | null;
  dataset?: 'classic' | 'private';
  market?: ResearchMarket;
}

export interface ComposeHolding {
  symbol: string;
  name?: string;
  industry?: string;
  score?: number;
}

export interface ComposeResponse {
  kpi: FactorKpi;
  excess: ExcessKpi;
  nav: SeriesPoint[];
  turnover: SeriesPoint[];
  benchmarks: BenchSeries[];
  holdings: ComposeHolding[];
  holdings_date: string | null;
  weights: Record<string, number>;
  thresholds: Record<string, number> | null;
  top_n: number;
  threshold: number | null;
  range: RangeMeta;
}

export interface OptimalWinner {
  weights: Record<string, number>;
  sharpe: number | null;
  annual_return: number;
  max_drawdown: number;
  win_rate: number;
  /** 对**本市场主基准**的年化超额（全市场统一键） */
  excess_primary: number | null;
  /** CN 旧键（一期兼容；非 CN 不存在） */
  excess_300?: number | null;
}

export interface OptimalResponse {
  objectives: {
    sharpe: OptimalWinner | null;
    annual_return: OptimalWinner | null;
    /** 主基准超额目标（全市场统一键；CN 同时保留 excess_300 旧键） */
    excess_primary: OptimalWinner | null;
    excess_300?: OptimalWinner | null;
  };
  /** 本市场主基准（无基准市场为 null，此时超额目标不存在、`excess_primary` 键缺席） */
  bench_primary?: BenchMeta | null;
  n_combos: number;
  n_months: number;
  codes: string[];
  elapsed_ms: number;
}

// ---------------------------------------------------------------------------
// 因子筛选（质量门槛 + 同源去重，screen_factors.py 产物）
// ---------------------------------------------------------------------------
export interface ScreeningRow {
  name: string;
  display_name: string;
  library: string;
  l1?: string;
  l2?: string;
  ic_mean: number | null;
  icir: number | null;
  turnover: number | null;
}

export interface ScreeningDropDup {
  name: string;
  library: string;
  duplicate_of: string;
  abs_corr: number;
}

export interface ScreeningGateDrop {
  name: string;
  library: string;
  reason: string;
}

export interface ScreeningResponse {
  /**
   * 非 CN 市场诚实缺席（该产品是 A 股口径的七库筛选）：`available=false` +
   * `reason`。**缺省视为可用** —— CN 存量产物没有这个字段，前端按 `!== false` 判。
   */
  available?: boolean;
  reason?: string;
  /** 本响应所属市场（非 CN 缺席时后端回话带市场） */
  market?: ResearchMarket;
  generated_at: string;
  gates: { min_abs_ic: number; min_abs_icir: number; corr_threshold: number };
  cross_corr: string;
  counts: {
    candidates: number;
    kept: number;
    gated_out: number;
    deduped: number;
    total_considered: number;
  };
  kept: ScreeningRow[];
  dropped_gated: ScreeningGateDrop[];
  dropped_duplicate: ScreeningDropDup[];
  cluster_summary?: Record<string, unknown>;
}
