/**
 * 评估中心「一图一问」长序列（阶段 2 第 3 段）：`/eval/series` 侧车 → ECharts option。
 *
 * 三条纪律：
 *
 * 1. **图上数与卡上分同源**：这里只摊平后端算好的序列（`factor_series.py` /
 *    `model_series.py`），前端不重算任何统计量——否则「分数 82、曲线却是另一组数」
 *    这种偏差没人能发现。
 * 2. **不给空图**：序列为空 → `option=null`，由 `answer` 说明原因；一条平的空线
 *    会被读成「算过且没事」。
 * 3. **每句话都出自所画的那条序列**：`answer` 里的均值/占比/比例全部现场从同一数组
 *    算，不引用别处数字。
 *
 * 配色只从 `evalCenterModel.CHART_COLORS` 取（A 股红涨绿跌）。
 */

import type { EvalSeriesData, EvalSeriesPoint } from '../types/evalCenter';
import { CHART_COLORS as C, formatNumber, formatPercent } from './evalCenterModel';

export type SeriesKind =
  | 'ic_line'
  | 'decile_bar'
  | 'decay_bar'
  | 'segment_bar'
  | 'turnover_line'
  | 'corr_bar';

/** 每张图回答的那个问题（图题即问句） */
export const SERIES_QUESTIONS: Record<SeriesKind, string> = {
  ic_line: 'IC 是稳定为正，还是忽正忽负？',
  decile_bar: '按预测值分档，收益是单调爬升的吗？',
  decay_bar: '信号能撑几天（IC 随持有期衰减）？',
  segment_bar: '换一段行情，结论还成立吗？',
  turnover_line: '多久换一次仓（换手节奏）？',
  corr_bar: '跟已有因子重复吗？',
};

const SERIES_KEYS: Record<SeriesKind, string> = {
  ic_line: 'daily_ic',
  decile_bar: 'decile_mean',
  decay_bar: 'ic_decay',
  segment_bar: 'segment_ic',
  turnover_line: 'turnover',
  corr_bar: 'correlation',
};

/** 各类对象实际会展示的图（因子没有 turnover 序列、模型没有相关矩阵） */
const SERIES_SLOTS: Record<string, SeriesKind[]> = {
  factor: ['ic_line', 'decile_bar', 'decay_bar', 'segment_bar', 'corr_bar'],
  model: ['ic_line', 'decile_bar', 'decay_bar', 'segment_bar', 'turnover_line'],
};

const DEFAULT_SLOTS: SeriesKind[] = [
  'ic_line',
  'decile_bar',
  'decay_bar',
  'segment_bar',
  'turnover_line',
  'corr_bar',
];

/** 十分位买卖侧分界：上半档=做多侧（红），下半档=做空侧（绿） */
const DECILE_SPLIT = 5;
/** 相关性红线（与因子卡独立性维同口径：>0.9 去重） */
const CORR_RED_LINE = 0.9;

function points(data: EvalSeriesData | null | undefined, key: string): EvalSeriesPoint[] {
  const raw = data?.series?.[key];
  if (!Array.isArray(raw)) return [];
  return raw.filter((point) => point && Number.isFinite(Number(point.value)));
}

/** 序列缺省原因：优先用后端 notes 原文；后端也没说 → 如实说没产出 */
function noteFor(data: EvalSeriesData | null | undefined, key: string): string {
  const note = data?.notes?.[key];
  if (typeof note === 'string' && note.trim()) return note.trim();
  return `${key} 序列未产出（后端 notes 里也没有说明）`;
}

function axisBase(categories?: string[]) {
  return {
    grid: { left: 56, right: 16, top: 16, bottom: 34 },
    tooltip: { trigger: 'axis' as const, confine: true },
    xAxis: {
      type: 'category' as const,
      ...(categories ? { data: categories } : {}),
      axisLabel: { color: C.axis, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: C.split } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: { color: C.axis, fontSize: 10 },
      splitLine: { lineStyle: { color: C.split } },
    },
  };
}

/** 数值 → 涨跌色（0 用中性色：既不涨也不跌，不该染成红或绿） */
function signColor(value: number): string {
  if (value > 0) return C.up;
  if (value < 0) return C.down;
  return C.neutral;
}

function barChart(
  categories: string[],
  data: Array<{ value: number; itemStyle: Record<string, unknown> }>,
  name: string
): Record<string, unknown> {
  return { ...axisBase(categories), series: [{ type: 'bar', name, data }] };
}

// ── 图：每种图一个 builder（无数据 → null，不给空图）─────────────────

function icLineOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  return {
    ...axisBase(pts.map((p) => String(p.date || ''))),
    series: [
      {
        type: 'line',
        name: '逐日 IC',
        data: pts.map((p) => p.value),
        showSymbol: false,
        lineStyle: { width: 1.6, color: C.neutral },
        itemStyle: { color: C.neutral },
        // 0 轴虚线：忽正忽负这件事靠它一眼看出来
        markLine: {
          symbol: 'none',
          silent: true,
          data: [{ yAxis: 0 }],
          lineStyle: { type: 'dashed', color: C.axis },
          label: { show: false },
        },
      },
    ],
  };
}

function decileOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  return barChart(
    pts.map((p) => (p.bucket === undefined ? String(p.label || '') : `D${p.bucket}`)),
    pts.map((p) => {
      const bucket = Number(p.bucket);
      const isBuy = Number.isFinite(bucket) && bucket > DECILE_SPLIT;
      return { value: Number(p.value), itemStyle: { color: isBuy ? C.up : C.down } };
    }),
    '分档日均收益'
  );
}

function decayOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  return barChart(
    pts.map((p) => `${p.horizon}日`),
    pts.map((p) => ({ value: Number(p.value), itemStyle: { color: signColor(Number(p.value)) } })),
    '各持有期 IC'
  );
}

function segmentOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  return barChart(
    pts.map((p) => String(p.label || '')),
    pts.map((p) => ({
      value: Number(p.value),
      itemStyle: {
        color: signColor(Number(p.value)),
        // 最低段描边：不靠颜色深浅猜「哪段最差」
        borderColor: C.risk,
        borderWidth: p.is_min_segment ? 1.5 : 0,
      },
    })),
    '分段 IC'
  );
}

function turnoverOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  return {
    ...axisBase(pts.map((p) => `#${p.pair ?? ''}`)),
    series: [
      {
        type: 'line',
        name: '换手率',
        data: pts.map((p) => Number(p.value)),
        showSymbol: false,
        lineStyle: { width: 1.6, color: C.neutral },
        itemStyle: { color: C.neutral },
      },
    ],
  };
}

/** 横向条：降序 + inverse，最相关的那条显示在最上面 */
function corrOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  const ordered = [...pts].sort((a, b) => Number(b.value) - Number(a.value));
  return {
    ...axisBase(),
    grid: { left: 104, right: 24, top: 12, bottom: 28 },
    xAxis: {
      type: 'value',
      axisLabel: { color: C.axis, fontSize: 10 },
      splitLine: { lineStyle: { color: C.split } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: ordered.map((p) => String(p.name ?? p.label ?? '')),
      axisLabel: { color: C.neutral, fontSize: 10 },
      axisLine: { lineStyle: { color: C.split } },
      axisTick: { show: false },
    },
    series: [
      {
        type: 'bar',
        name: '|相关性|',
        data: ordered.map((p) => ({
          value: Number(p.value),
          itemStyle: { color: Number(p.value) >= CORR_RED_LINE ? C.risk : C.neutral },
        })),
      },
    ],
  };
}

const OPTION_BUILDERS: Record<
  SeriesKind,
  (pts: EvalSeriesPoint[]) => Record<string, unknown>
> = {
  ic_line: icLineOption,
  decile_bar: decileOption,
  decay_bar: decayOption,
  segment_bar: segmentOption,
  turnover_line: turnoverOption,
  corr_bar: corrOption,
};

/** 单条序列 → ECharts option；无数据 → null（不给空图） */
export function seriesToOption(
  kind: SeriesKind,
  data: EvalSeriesData | null | undefined
): Record<string, unknown> | null {
  const pts = points(data, SERIES_KEYS[kind]);
  if (pts.length === 0) return null;
  return OPTION_BUILDERS[kind](pts);
}

// ── 一句话结论：只复述所画序列本身的事实 ────────────────────────────

function valuesOf(pts: EvalSeriesPoint[]): number[] {
  return pts.map((p) => Number(p.value));
}

function meanOf(values: number[]): number {
  return values.reduce((acc, value) => acc + value, 0) / values.length;
}

function icLineAnswer(pts: EvalSeriesPoint[]): string {
  if (pts.length < 2) return `只有 ${pts.length} 个有效交易日，画不出曲线`;
  const values = valuesOf(pts);
  const positive = values.filter((value) => value > 0).length;
  return `近 ${pts.length} 个交易日 IC 均值 ${formatNumber(meanOf(values), 4)}，正向占比 ${formatPercent(
    positive / values.length,
    1
  )}`;
}

function decileAnswer(pts: EvalSeriesPoint[]): string {
  if (pts.length < 2) return `只有 ${pts.length} 档，看不出单调性`;
  const values = valuesOf(pts);
  const steps = values.filter((value, index) => index > 0 && value > values[index - 1]).length;
  const head = pts[0];
  const tail = pts[pts.length - 1];
  return `第 ${head.bucket ?? 1} 档 ${formatNumber(head.value, 4)} → 第 ${
    tail.bucket ?? pts.length
  } 档 ${formatNumber(tail.value, 4)}，递增 ${steps}/${values.length - 1} 步`;
}

function decayAnswer(pts: EvalSeriesPoint[]): string {
  if (pts.length < 2) return `只有 ${pts.length} 个视界，看不出衰减`;
  const head = pts[0];
  const tail = pts[pts.length - 1];
  const first = Math.abs(Number(head.value));
  const last = Math.abs(Number(tail.value));
  const keep = first > 0 ? `（保留 ${formatPercent(last / first, 1)}）` : '（首档为 0，比例无从算）';
  return `|IC| 从 ${head.horizon} 日 ${formatNumber(first, 4)} 到 ${tail.horizon} 日 ${formatNumber(
    last,
    4
  )}${keep}`;
}

function segmentAnswer(pts: EvalSeriesPoint[]): string {
  const lowest = pts.reduce((acc, p) => (Number(p.value) < Number(acc.value) ? p : acc), pts[0]);
  return `共 ${pts.length} 段，最低 ${lowest.label ?? '—'}（${formatNumber(lowest.value, 4)}）`;
}

function turnoverAnswer(pts: EvalSeriesPoint[]): string {
  return `均值换手 ${formatPercent(meanOf(valuesOf(pts)), 1)}（共 ${pts.length} 期）`;
}

function corrAnswer(pts: EvalSeriesPoint[]): string {
  const top = pts.reduce((acc, p) => (Number(p.value) > Number(acc.value) ? p : acc), pts[0]);
  return `最高相关 ${top.name ?? top.label ?? '—'} ${formatNumber(top.value, 4)}`;
}

const ANSWER_BUILDERS: Record<SeriesKind, (pts: EvalSeriesPoint[]) => string> = {
  ic_line: icLineAnswer,
  decile_bar: decileAnswer,
  decay_bar: decayAnswer,
  segment_bar: segmentAnswer,
  turnover_line: turnoverAnswer,
  corr_bar: corrAnswer,
};

/** 每张图的一句话结论（数字全部来自所画的那条序列） */
export function seriesAnswer(kind: SeriesKind, data: EvalSeriesData | null | undefined): string {
  const pts = points(data, SERIES_KEYS[kind]);
  // 一点数据都没有 → 原因来自后端 notes（不是「0 个视界」这种自己编的说法）
  if (pts.length === 0) return noteFor(data, SERIES_KEYS[kind]);
  return ANSWER_BUILDERS[kind](pts);
}

export interface SeriesChart {
  kind: SeriesKind;
  question: string;
  answer: string;
  /** null = 这项没有序列（answer 说明原因），调用方渲染说明而不是空图 */
  option: Record<string, unknown> | null;
}

/**
 * 该类对象的全部一问一图。
 *
 * **侧车整个不存在**（`data` 为 null 或没有 `series` 键）→ 空数组，调用方改渲染
 * `/eval/series` 的 `meta.note`；**侧车在但某些序列为空** → 照常排图位，
 * 缺的那张用 `answer` 说明原因（缺一个序列不该让另外四张跟着消失）。
 */
export function buildSeriesCharts(
  objectType: string,
  data: EvalSeriesData | null | undefined
): SeriesChart[] {
  if (!data?.series || Object.keys(data.series).length === 0) return [];
  const slots = SERIES_SLOTS[String(objectType || '')] || DEFAULT_SLOTS;
  return slots.map((kind) => ({
    kind,
    question: SERIES_QUESTIONS[kind],
    answer: seriesAnswer(kind, data),
    option: seriesToOption(kind, data),
  }));
}
