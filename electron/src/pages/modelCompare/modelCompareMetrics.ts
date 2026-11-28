/**
 * 模型对比弹窗的指标口径与展示工具（唯一出处）。
 *
 * 数据口径：
 * - 测试集 IC / IR：训练产物 metadata 的 test 段（`resolveSplitMetrics` 唯一出处）；
 * - 近 3 个月：模型管理同一份 `getModelSignalEval`（滚动 test 切分，窗口随推理前移）。
 *
 * 缺失一律显示「—」，不折算成 0。
 */
import type { UserModelRecord } from '../../services/modelTrainingService';
import type { SignalEvalReport, SignalEvalState } from '../../services/modelSignalEvalTypes';
import { getMeta, resolveSplitMetrics } from '../modelRegistryUtils';

/** 左侧指标列固定宽度（px），冻结列的 sticky 偏移以它为基准。 */
export const METRIC_COL_WIDTH = 208;
/** 单个模型列固定宽度（px）；窗口默认宽度按列数推算。 */
export const MODEL_COL_WIDTH = 236;
/** 最多冻结列数：指标列 + 3 个冻结列 = 916px，仍小于窗口最小宽度 940px。 */
export const MAX_FROZEN_COLUMNS = 3;

export type EvalEntry = {
  state: SignalEvalState | null;
  loading: boolean;
  error: string | null;
};

type MetricKind = 'decimal' | 'ratio' | 'percent';

export type MetricKey =
  | 'test_rank_ic'
  | 'test_ir'
  | 'rank_ic'
  | 'icir'
  | 'win_rate'
  | 'hac_t'
  | 'neutral_ic'
  | 'neutral_hac_t'
  | 'spread'
  | 'monotonicity'
  | 'spread_t'
  | 'top20_return'
  | 'top20_excess'
  | 'top20_mdd'
  | 'top20_annualized'
  | 'top20_sharpe'
  | 'top50_excess'
  | 'top100_excess'
  | 'stability_autocorr'
  | 'coverage_avg'
  | 'coverage_min';

export interface CompareMetric {
  key: MetricKey;
  label: string;
  group: 'test' | 'ranking' | 'quantile' | 'portfolio' | 'live';
  kind: MetricKind;
  hint?: string;
}

export const METRICS: CompareMetric[] = [
  {
    key: 'test_rank_ic',
    label: '测试集 RankIC',
    group: 'test',
    kind: 'decimal',
    hint: '训练产物 test 段；不是近 3 个月滚动值',
  },
  {
    key: 'test_ir',
    label: '测试集 IR',
    group: 'test',
    kind: 'ratio',
    hint: 'RankIC 的 ICIR（test 段）',
  },
  { key: 'rank_ic', label: '截面 RankIC', group: 'ranking', kind: 'decimal' },
  { key: 'icir', label: 'ICIR', group: 'ranking', kind: 'ratio' },
  { key: 'win_rate', label: 'IC 胜率', group: 'ranking', kind: 'percent' },
  { key: 'hac_t', label: 'RankIC HAC t', group: 'ranking', kind: 'ratio' },
  { key: 'neutral_ic', label: '中性化 RankIC', group: 'ranking', kind: 'decimal' },
  { key: 'neutral_hac_t', label: '中性化 HAC t', group: 'ranking', kind: 'ratio' },
  { key: 'spread', label: '分位价差 Q10−Q1', group: 'quantile', kind: 'percent' },
  { key: 'monotonicity', label: '分位单调性', group: 'quantile', kind: 'ratio' },
  { key: 'spread_t', label: '价差 HAC t', group: 'quantile', kind: 'ratio' },
  { key: 'top20_return', label: 'Top20 收益（近3月）', group: 'portfolio', kind: 'percent' },
  { key: 'top20_excess', label: 'Top20 超额', group: 'portfolio', kind: 'percent' },
  { key: 'top20_mdd', label: 'Top20 回撤', group: 'portfolio', kind: 'percent' },
  { key: 'top20_annualized', label: 'Top20 年化', group: 'live', kind: 'percent' },
  { key: 'top20_sharpe', label: 'Top20 Sharpe', group: 'live', kind: 'ratio' },
  { key: 'top50_excess', label: 'Top50 超额', group: 'live', kind: 'percent' },
  { key: 'top100_excess', label: 'Top100 超额', group: 'live', kind: 'percent' },
  { key: 'stability_autocorr', label: '排序稳定性（日自相关）', group: 'live', kind: 'ratio' },
  { key: 'coverage_avg', label: '覆盖率均值', group: 'live', kind: 'percent' },
  { key: 'coverage_min', label: '覆盖率最低', group: 'live', kind: 'percent' },
];

export const GROUP_META: Record<
  CompareMetric['group'],
  { label: string; hint: string; className: string; solidClassName: string }
> = {
  test: {
    label: '测试集 IC · 训练产物',
    hint: '训练结束时 test 段的排序指标',
    className: 'from-slate-100 to-white',
    solidClassName: 'bg-slate-100',
  },
  ranking: {
    label: '最近 3 个月 · 截面排序',
    hint: '滚动 test 切分；窗口随推理与行情前移',
    className: 'from-indigo-50 to-white',
    solidClassName: 'bg-indigo-50',
  },
  quantile: {
    label: '最近 3 个月 · 分位结构',
    hint: 'Q10−Q1 极值价差；单调性越接近 1 越好',
    className: 'from-violet-50 to-white',
    solidClassName: 'bg-violet-50',
  },
  portfolio: {
    label: '最近 3 个月 · Top20 长多',
    hint: 'Top20 等权模拟；超额相对等权全市场',
    className: 'from-amber-50 to-white',
    solidClassName: 'bg-amber-50',
  },
  live: {
    label: '实盘力补充',
    hint: '年化 / Sharpe / 不同持仓数超额 / 稳定性 / 覆盖率',
    className: 'from-emerald-50 to-white',
    solidClassName: 'bg-emerald-50',
  },
};

export const GROUP_ORDER: CompareMetric['group'][] = [
  'test',
  'ranking',
  'quantile',
  'portfolio',
  'live',
];

function fmtDecimal(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
}

function fmtRatio(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toFixed(digits);
}

function fmtPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const pct = value * 100;
  return `${pct > 0 ? '+' : ''}${pct.toFixed(digits)}%`;
}

export function fmtMetric(value: number | null, kind: MetricKind): string {
  if (value === null) return '—';
  if (kind === 'percent') return fmtPercent(value);
  if (kind === 'ratio') return fmtRatio(value);
  return fmtDecimal(value);
}

export function reportOf(state: SignalEvalState | null): SignalEvalReport | null {
  if (!state) return null;
  if (state.status === 'ready' || state.status === 'stale') return state.report;
  if (state.status === 'running') return state.cached_report ?? null;
  return null;
}

export function evalStatusLabel(entry: EvalEntry | undefined): string {
  if (!entry || entry.loading) return '统计读取中…';
  if (entry.error) return '统计读取失败';
  const status = entry.state?.status;
  if (status === 'running') return '正在统计…';
  if (status === 'missing' || !entry.state) return '未统计';
  if (status === 'unavailable') return '不可用';
  if (status === 'stale') return '统计已过期';
  return '';
}

function topNOf(report: SignalEvalReport | null, n: number) {
  if (!report?.topn?.length) return undefined;
  return report.topn.find((row) => Number(row.n) === n);
}

function top20Of(report: SignalEvalReport | null) {
  return topNOf(report, 20) ?? report?.topn?.[0];
}

export function metricValue(
  model: UserModelRecord,
  entry: EvalEntry | undefined,
  key: MetricKey,
): number | null {
  const test = resolveSplitMetrics(getMeta(model)).test;
  if (key === 'test_rank_ic') return test.rankIc;
  if (key === 'test_ir') return test.icir;
  const report = reportOf(entry?.state ?? null);
  if (!report) return null;
  const ranking = report.metrics.rank_ic;
  const neutral = report.metrics.neutral_ic;
  const quantiles = report.metrics.quantiles;
  const top20 = top20Of(report);
  switch (key) {
    case 'rank_ic':
      return ranking.mean;
    case 'icir':
      return ranking.icir;
    case 'win_rate':
      return ranking.win_rate;
    case 'hac_t':
      return ranking.hac_t ?? ranking.t;
    case 'neutral_ic':
      return neutral.mean;
    case 'neutral_hac_t':
      return neutral.hac_t ?? neutral.t;
    case 'spread':
      return quantiles.spread_mean;
    case 'monotonicity':
      return quantiles.monotonicity;
    case 'spread_t':
      return quantiles.spread_t;
    case 'top20_return':
      return top20?.total_return ?? null;
    case 'top20_excess':
      return top20?.excess ?? null;
    case 'top20_mdd':
      return top20?.mdd ?? null;
    case 'top20_annualized':
      return top20?.annualized ?? null;
    case 'top20_sharpe':
      return top20?.sharpe ?? null;
    case 'top50_excess':
      return topNOf(report, 50)?.excess ?? null;
    case 'top100_excess':
      return topNOf(report, 100)?.excess ?? null;
    case 'stability_autocorr':
      return report.metrics.stability.rank_autocorr_daily;
    case 'coverage_avg':
      return report.coverage?.avg ?? null;
    case 'coverage_min':
      return report.coverage?.min ?? null;
    default:
      return null;
  }
}

export function metricTone(key: MetricKey, value: number | null): string {
  if (value === null) return 'text-slate-300';
  // A 股口径：收益类涨红跌绿；IC/IR 等排序指标用中性色，避免把「正 IC」误读成涨跌。
  if (key === 'top20_mdd') return value < 0 ? 'text-amber-600' : 'text-slate-800';
  if (
    key === 'top20_return' ||
    key === 'top20_excess' ||
    key === 'top20_annualized' ||
    key === 'top50_excess' ||
    key === 'top100_excess'
  ) {
    return value > 0 ? 'text-rose-600' : value < 0 ? 'text-emerald-600' : 'text-slate-500';
  }
  return 'text-slate-800';
}

export function gradeBadge(entry: EvalEntry | undefined): { text: string; className: string } {
  const grade = entry?.state && 'admission' in entry.state ? entry.state.admission.grade : 'unknown';
  if (grade === 'green') return { text: '通过', className: 'bg-emerald-100 text-emerald-700' };
  if (grade === 'yellow') return { text: '预警', className: 'bg-amber-100 text-amber-700' };
  if (grade === 'red') return { text: '未通过', className: 'bg-rose-100 text-rose-600' };
  return { text: '未评估', className: 'bg-slate-100 text-slate-500' };
}
