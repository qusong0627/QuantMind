/**
 * 回测中心类型（T-FB-12..15）——后端 `/api/v1/factor-backtest/*` 契约。
 *
 * 归一约定：service 层把后端 snake_case 映射为 camelCase；**指标键例外**——
 * 矩阵/台账的 metrics 字典保留后端原始键（ic / rank_ic / sharpe_net …），
 * 因为指标词表由后端 metrics 层单源定义（`factor_backtest/ic.py`），前端
 * 再起一套改名会在两侧漂移。展示标签统一走 `MATRIX_METRIC_SPECS`。
 */

/** 台账/矩阵的终态词表（与后端 store.TERMINAL_STATUSES 同词表，诚实降级三态在内） */
export const BACKTEST_TERMINAL_STATUSES = [
  'completed',
  'failed',
  'cancelled',
  'data_unsupported',
  'insufficient',
  'unavailable',
] as const;

export type BacktestTerminalStatus = (typeof BACKTEST_TERMINAL_STATUSES)[number];
export type BacktestRunStatus = 'running' | BacktestTerminalStatus;
/** 矩阵格：未跑过是独立状态（带静态兼容档），不是「失败」 */
export type MatrixCellStatus = 'not_run' | BacktestRunStatus;

/** 静态兼容档（跑之前的结论：代码列 token ⊆ 市场列集） */
export type CompatStatus = 'portable' | 'data_unsupported' | 'unknown';

// ── 市场档案（GET /markets） ─────────────────────────────────────────

export interface MarketInfo {
  market: string;
  qlibMarket: string;
  label: string;
  /** true = 样本内（CN，挖掘原始市场）；false = 样本外 */
  inSample: boolean;
  experimental: boolean;
  note: string | null;
  /** 数据面是否就绪（provider 可读）；false 时不可派发 */
  ready: boolean;
  calendarStart: string | null;
  calendarEnd: string | null;
  instruments: number | null;
  columns: string[];
  universeMode: string;
  defaultUniverse: string | null;
  universeTopN: number | null;
  windowYears: number;
  costBps: number;
  benchmark: string | null;
  minDays: number;
}

// ── 适配矩阵（POST /matrix） ─────────────────────────────────────────

export interface MatrixCell {
  status: MatrixCellStatus;
  runId: string | null;
  /** 静态兼容档（status=not_run 时是唯一结论来源） */
  compat: CompatStatus;
  /** 缺哪些列（data_unsupported 的诚实话术） */
  missing: string[];
  /** 动态列引用（静态判不穿，照跑裁决） */
  dynamic: boolean;
  error: string | null;
  universe: string | null;
  dateRange: string | null;
  finishedAt: string | null;
  inSample: boolean;
  /**
   * 指标字典（后端原始键）：metrics_json 主源 + 台账专列回落
   * （ic/rank_ic/icir/sharpe/ann_return_net/max_drawdown 六键已合并，
   * 缺失是**键缺席或 null**，一律显「—」，绝不显示成 0）。
   */
  metrics: Record<string, number | null>;
}

export interface MatrixFactorRow {
  factorId: string;
  factorName: string | null;
  /** 因子库里是否存在（不存在=已删/他人） */
  found: boolean;
  /** 归属可见（他人因子不展示数据格） */
  owned: boolean;
  /** 因子库行上的 CN IC（样本内基准旁的标签值） */
  cnIc: number | null;
  cells: Record<string, MatrixCell>;
}

export interface MatrixMarketCol {
  market: string;
  label: string;
  inSample: boolean;
  experimental: boolean;
  benchmark: string | null;
  costBps: number;
}

export interface MatrixResult {
  markets: MatrixMarketCol[];
  factors: MatrixFactorRow[];
  /** 状态直方图（全表格数） */
  counts: Record<string, number>;
}

// ── 批量派发（POST /batch、GET /batch/status、POST /batch/cancel） ──

export interface BatchSkippedItem {
  factorId: string;
  market: string | null;
  reason: string;
}

export interface BatchLaunchResult {
  /** null = 全部因子被跳过（空批，未落库） */
  batchId: string | null;
  total: number;
  queued: number;
  skipped: BatchSkippedItem[];
  status: string;
  message: string;
}

export interface BatchUnit {
  factorId: string;
  market: string;
  status: MatrixCellStatus;
  runId: string | null;
  attempts: number;
  error: string | null;
  finishedAt: string | null;
  ic: number | null;
  rankIc: number | null;
  icir: number | null;
  sharpe: number | null;
  maxDrawdown: number | null;
  nDays: number | null;
}

export interface BatchProgress {
  pending: number;
  running: number;
  completed: number;
  failed: number;
  cancelled: number;
  dataUnsupported: number;
  insufficient: number;
  unavailable: number;
  total: number;
  /** 终态计数之和（≠ 全成功） */
  done: number;
  consecFails: number;
  maxConsecFails: number;
  /** 排水器是否在内存中（false = engine 重启后未被 resume 兜底拾起） */
  draining: boolean;
  current: { factorId: string; market: string; runId: string }[];
}

export interface BatchStatus {
  batch: {
    batchId: string;
    userId: string | null;
    status: 'running' | 'completed' | 'cancelled' | 'aborted';
    error: string | null;
    createdAt: string | null;
    finishedAt: string | null;
  };
  spec: {
    factorIds: string[];
    markets: string[];
    start: string | null;
    end: string | null;
    costBps: number | null;
    skipped: BatchSkippedItem[];
  };
  progress: BatchProgress;
  units: BatchUnit[];
  /** 最新尝试为 failed 的单元（熔断判据面） */
  failures: BatchUnit[];
}

export interface BatchListItem {
  batchId: string;
  userId: string | null;
  status: 'running' | 'completed' | 'cancelled' | 'aborted';
  error: string | null;
  createdAt: string | null;
  finishedAt: string | null;
  spec: {
    factorIds: string[];
    markets: string[];
    start: string | null;
    end: string | null;
    costBps: number | null;
    skipped: BatchSkippedItem[];
  };
}

// ── 运行台账（GET /runs、GET /runs/{run_id}/series） ────────────────

export interface LedgerRun {
  runId: string;
  factorId: string;
  factorName: string | null;
  status: BacktestRunStatus;
  kind: string | null;
  market: string | null;
  universe: string | null;
  dataSource: string | null;
  dateRange: string | null;
  error: string | null;
  metrics: Record<string, number | null>;
  /** 是否有曲线可下钻（has_series） */
  hasSeries: boolean;
  createdAt: string | null;
  finishedAt: string | null;
}

/** `/runs/{run_id}/series` 的序列载荷（后端 build_series_payload 逐字契约） */
export interface RunSeries {
  dates: string[];
  ic: (number | null)[];
  icCum: (number | null)[];
  navLong: (number | null)[];
  navLs: (number | null)[];
  navBench: (number | null)[];
  /** 分位桶净值（键 q1..qN，q1=最低分位） */
  qCurves: Record<string, (number | null)[]>;
  turnover: (number | null)[];
  coverage: number[];
  /** 基准口径；'equal_weight' = 等权兜底（无该市场基准指数） */
  bench: string;
  meta: {
    costBps: number;
    topPct: number;
    nBuckets: number;
    turnoverConvention: string;
  };
}

export interface RunSeriesResult {
  run: LedgerRun;
  series: RunSeries;
}

// ── 机构报告标量块（GET /report/{run_id}，T-FB-16） ──────────────────

/** 多空腿头部（BRAIN 口径；Returns 为简单年化 μ×252，不是 CAGR） */
export interface RunReportHeadline {
  nDays: number | null;
  muDaily: number | null;
  sigmaDaily: number | null;
  annVol: number | null;
  returns: number | null;
  cumReturn: number | null;
  ir: number | null;
  /** 日均双边换手（日口径；IC 累计曲线页的年化为另一列） */
  turnover: number | null;
  fitness: number | null;
  margin: number | null;
}

export interface RunReportBootstrap {
  lo: number | null;
  hi: number | null;
  point: number | null;
  level: number | null;
  nBoot: number | null;
  stat: string | null;
}

export interface RunReportCrowding {
  score: number | null;
  turnoverPct: number | null;
  icAutocorrLag1: number | null;
  nDays: number | null;
  note: string | null;
}

export interface RunReportSignificance {
  /** 普通 t（未校正自相关；对照 NW t 读） */
  plainT: number | null;
  /** Newey-West 调整 t（与台账 metrics.ic_nw_t 同源同值） */
  nwT: number | null;
  pValue: number | null;
  /** BY 校正 q（族 = 同批次完成单元；无族上下文时 q=p 并见 familyNote） */
  qValueBhy: number | null;
  familyN: number;
  familyNote: string | null;
  dsr: number | null;
  nTrials: number;
  /** batch_completed_units | param | default_single */
  nTrialsSource: string;
  dsrNote: string | null;
  bootstrap: RunReportBootstrap | null;
  crowding: RunReportCrowding | null;
}

export interface RunReportCostRow {
  bps: number;
  netReturn: number | null;
  netIr: number | null;
  netFitness: number | null;
}

export interface RunReport {
  /** false = 降级/序列缺失：只有 status/reason/note，**没有任何数字** */
  available: boolean;
  status: string;
  runId?: string;
  nDays?: number;
  headline?: RunReportHeadline;
  significance?: RunReportSignificance;
  costGrid?: {
    rows: RunReportCostRow[];
    breakEvenBps: number | null;
    breakEvenNote: string | null;
    defaultBps: number | null;
  };
  /** 超额基准标注：等权兜底绝不冒充指数超额 */
  excess?: {
    kind: string;
    benchmarkRef: string | null;
    label: string;
    note: string;
  };
  /** 序列载荷算不出的报告块（各写明缺什么输入；不做近似替代） */
  unavailable?: { block: string; reason: string }[];
  meta?: {
    costBps: number | null;
    topPct: number | null;
    turnoverConvention: string | null;
    source: string;
  };
  /** available=false 时的原因原文 */
  reason?: string | null;
  note?: string | null;
}

export interface RunReportResult {
  run: LedgerRun;
  report: RunReport;
}

/** 暂缺报告块的中文标签（报告抽屉「暂缺」清单） */
export const REPORT_BLOCK_LABELS: Record<string, string> = {
  capacity: '容量估算',
  holding_period: '多期持有对比',
  ic_half_life: 'IC 衰减半衰期',
  style_attribution: '风格归因',
};

// ── 钻取目标（矩阵格 / 台账行 → 报告抽屉） ──────────────────────────

export interface DrillTarget {
  factorId: string;
  factorName?: string | null;
  market: string;
  marketLabel?: string;
  runId: string | null;
  /** 台账行直开时带上状态/错误（无序列也能说清为什么没有图） */
  status?: MatrixCellStatus;
  error?: string | null;
  dateRange?: string | null;
  universe?: string | null;
  /** 矩阵格指标（概览卡数据源；台账行直开时缺席，概览按曲线可用性降级） */
  metrics?: Record<string, number | null>;
}

// ── 指标词表（矩阵切换器 / CSV / 颜色方向单源） ─────────────────────

export interface MatrixMetricSpec {
  key: string;
  label: string;
  /** higher = 越大越好（Best 徽标 / 排序方向 / 阈值筛选） */
  direction: 'higher' | 'lower' | 'none';
  format: 'number' | 'percent';
  precision: number;
}

export const MATRIX_METRIC_SPECS: MatrixMetricSpec[] = [
  { key: 'rank_ic', label: 'Rank IC', direction: 'higher', format: 'number', precision: 4 },
  { key: 'ic', label: 'IC', direction: 'higher', format: 'number', precision: 4 },
  { key: 'icir', label: 'ICIR', direction: 'higher', format: 'number', precision: 3 },
  { key: 'rank_icir', label: 'Rank ICIR', direction: 'higher', format: 'number', precision: 3 },
  { key: 'sharpe_net', label: '扣费夏普', direction: 'higher', format: 'number', precision: 2 },
  { key: 'ann_return_net', label: '扣费年化', direction: 'higher', format: 'percent', precision: 2 },
  { key: 'max_drawdown', label: '最大回撤', direction: 'lower', format: 'percent', precision: 2 },
  { key: 'ann_turnover', label: '年化换手', direction: 'lower', format: 'number', precision: 1 },
  { key: 'n_days', label: '有效天数', direction: 'none', format: 'number', precision: 0 },
];

export function matrixMetricSpec(key: string): MatrixMetricSpec {
  return (
    MATRIX_METRIC_SPECS.find((s) => s.key === key) ?? {
      key,
      label: key,
      direction: 'none',
      format: 'number',
      precision: 4,
    }
  );
}

/** 状态 → 中文标签（矩阵/台账/批次同词表） */
export const BACKTEST_STATUS_LABELS: Record<string, string> = {
  not_run: '未回测',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  data_unsupported: '数据不支持',
  insufficient: '样本不足',
  unavailable: '数据缺失',
};
