/**
 * 评估中心「一图一问」长序列（阶段 2 第 3 段 → 阶段 3 扩到六类对象）：
 * `/eval/series` 侧车 → ECharts option。
 *
 * 三条纪律：
 *
 * 1. **图上数与卡上分同源**：这里只摊平后端算好的序列（`factor_series.py` /
 *    `model_series.py` / `strategy_series.py` / `account_series.py` /
 *    `daily_selection_series.py`），前端不重算任何统计量——否则「分数 82、曲线却是
 *    另一组数」这种偏差没人能发现。
 * 2. **不给空图**：序列为空 → `option=null`，由 `answer` 说明原因；一条平的空线
 *    会被读成「算过且没事」。
 * 3. **每句话都出自所画的那条序列**：`answer` 里的均值/占比/比例全部现场从同一数组
 *    算，不引用别处数字。**有数据的图还要把后端 `notes` 摊开**（样本只有 5 天、
 *    截断、几个点缺测），否则一条 5 点折线会被读成趋势。
 *
 * 配色只从 `evalCenterModel.CHART_COLORS` 取（A 股红涨绿跌）。
 */

import type { EvalSeriesData, EvalSeriesPoint } from '../types/evalCenter';
import {
  CHART_COLORS as C,
  formatNumber,
  formatPercent,
  formatSignedPct,
  toNumber,
} from './evalCenterModel';

export type SeriesKind =
  | 'ic_line'
  | 'decile_bar'
  | 'decay_bar'
  | 'segment_bar'
  | 'turnover_line'
  | 'corr_bar'
  | 'equity_line'
  | 'drawdown_area'
  | 'signed_bar'
  | 'count_bar'
  | 'rate_line';

/**
 * 每张图回答的那个问题（图题即问句）。后五种是「图形」，同一图形会挂到不同对象的不同
 * 序列上（账户的 `signed_bar` 画每日盈亏、每日选股的画事后超额），所以问句以槽位为准，
 * 这里是缺省值。
 */
export const SERIES_QUESTIONS: Record<SeriesKind, string> = {
  ic_line: 'IC 是稳定为正，还是忽正忽负？',
  decile_bar: '按预测值分档，收益是单调爬升的吗？',
  decay_bar: '信号能撑几天（IC 随持有期衰减）？',
  segment_bar: '换一段行情，结论还成立吗？',
  turnover_line: '多久换一次仓（换手节奏）？',
  corr_bar: '跟已有因子重复吗？',
  equity_line: '净值在涨还是在跌？',
  drawdown_area: '回撤有多深？',
  signed_bar: '各期正负是怎么分布的？',
  count_bar: '每天选出几只？',
  rate_line: '命中率在抛硬币线之上吗？',
};

/** 图形 → 缺省序列键（槽位可覆盖：`{kind:'signed_bar', key:'daily_pnl'}`） */
const SERIES_KEYS: Record<SeriesKind, string> = {
  ic_line: 'daily_ic',
  decile_bar: 'decile_mean',
  decay_bar: 'ic_decay',
  segment_bar: 'segment_ic',
  turnover_line: 'turnover',
  corr_bar: 'correlation',
  equity_line: 'equity',
  drawdown_area: 'drawdown',
  signed_bar: 'monthly_return',
  count_bar: 'picked',
  rate_line: 'hit_rate',
};

/** 数值口径：百分数（0.04 → 4%）、金额（净值/盈亏）、整只数、其余原样 */
type ValueUnit = 'pct' | 'money' | 'count' | 'plain';

const KEY_UNITS: Record<string, ValueUnit> = {
  equity: 'money',
  daily_pnl: 'money',
  drawdown: 'pct',
  monthly_return: 'pct',
  realized_excess: 'pct',
  hit_rate: 'pct',
  picked: 'count',
};

/** 序列键 → 图上名（新图形的 series name / 结论措辞；未登记的键直接用键名） */
const KEY_LABELS: Record<string, string> = {
  equity: '净值',
  daily_pnl: '当日盈亏',
  drawdown: '回撤',
  monthly_return: '月度收益',
  realized_excess: 'T+H 超额',
  hit_rate: '命中率',
  picked: '入选数',
};

/** 一张图位：图形 + 序列键 + 问句（三类新对象让同图形挂不同序列） */
export interface SeriesSlot {
  kind: SeriesKind;
  key: string;
  question: string;
}

function slot(kind: SeriesKind, key?: string, question?: string): SeriesSlot {
  return {
    kind,
    key: key || SERIES_KEYS[kind],
    question: question || SERIES_QUESTIONS[kind],
  };
}

/** 各类对象实际会展示的图（因子没有 turnover 序列、模型没有相关矩阵…） */
const SERIES_SLOTS: Record<string, SeriesSlot[]> = {
  factor: [
    slot('ic_line'),
    slot('decile_bar'),
    slot('decay_bar'),
    slot('segment_bar'),
    slot('corr_bar'),
  ],
  model: [
    slot('ic_line'),
    slot('decile_bar'),
    slot('decay_bar'),
    slot('segment_bar'),
    slot('turnover_line'),
  ],
  strategy: [
    slot('equity_line', undefined, '这条策略赚了还是亏了？'),
    slot('drawdown_area', undefined, '中途最难受的时候亏了多少？'),
    slot('signed_bar', 'monthly_return', '赢的月份比输的月份多吗？'),
  ],
  account: [
    slot('equity_line', undefined, '账户总资产在涨还是在跌？'),
    slot('signed_bar', 'daily_pnl', '每天赚亏多少？'),
  ],
  daily_selection: [
    slot('count_bar', undefined, '每天选出几只？'),
    slot('signed_bar', 'realized_excess', '事后看，选出来的票跑赢指数了吗？'),
    slot('rate_line', undefined, '命中率在抛硬币线之上吗？'),
  ],
};

const DEFAULT_SLOTS: SeriesSlot[] = [
  slot('ic_line'),
  slot('decile_bar'),
  slot('decay_bar'),
  slot('segment_bar'),
  slot('turnover_line'),
  slot('corr_bar'),
];

/** 十分位买卖侧分界：上半档=做多侧（红），下半档=做空侧（绿） */
const DECILE_SPLIT = 5;
/** 相关性红线（与因子卡独立性维同口径：>0.9 去重） */
const CORR_RED_LINE = 0.9;
/** 命中率的抛硬币基准线：50% 之上才算有选择力 */
const HIT_BASE_LINE = 0.5;
/** 不超过这么多点时显示数据点：少样本（账户快照）不显点会被读成趋势线 */
const SAMPLE_SYMBOL_MAX = 40;

function unitOf(key: string): ValueUnit {
  return KEY_UNITS[key] || 'plain';
}

function labelOf(key: string): string {
  return KEY_LABELS[key] || key;
}

/**
 * 序列点列（缺测点剔除）。
 *
 * `Number(null)` 是 0、`Number('')` 也是 0——拿 `Number.isFinite(Number(v))` 过滤会把
 * 「那一天没数」悄悄画成 0 并算进均值。统一走 `toNumber`（null/undefined/空串/NaN/∞
 * 一律 null）才是「缺测不与 0 同形」。
 */
function points(data: EvalSeriesData | null | undefined, key: string): EvalSeriesPoint[] {
  const raw = data?.series?.[key];
  if (!Array.isArray(raw)) return [];
  return raw.filter((point) => point && toNumber(point.value) !== null);
}

/** 后端 notes 原文（没有 → 空串）：带数据的图把它摊开，不重复编话 */
function backendNote(data: EvalSeriesData | null | undefined, key: string): string {
  const note = data?.notes?.[key];
  return typeof note === 'string' ? note.trim() : '';
}

/** 序列缺省原因：优先用后端 notes 原文；后端也没说 → 如实说没产出 */
function noteFor(data: EvalSeriesData | null | undefined, key: string): string {
  const note = backendNote(data, key);
  if (note) return note;
  return `${key} 序列未产出（后端 notes 里也没有说明）`;
}

/** 金额刻度：上万用「万」、上亿用「亿」（净值/盈亏的轴标签看得懂） */
function moneyTick(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)}万`;
  return String(Math.round(value));
}

/**
 * 百分数刻度：小数位随量级放宽。
 *
 * 固定 0 位会把「-0.35% ~ 0」这种小范围轴的每个刻度都印成同一行「−0%」
 * （实测：事后超额只有一天时，7 个刻度全长一个样）。刻度分不出高低就不是轴。
 */
function pctTick(value: number): string {
  const pct = value * 100;
  const abs = Math.abs(pct);
  const digits = abs >= 10 ? 0 : abs >= 1 ? 1 : 2;
  return `${pct.toFixed(digits)}%`;
}

/** 提示里的原值（ECharts 对柱子的数据项可能给对象，先拆出 value 再格式化） */
function rawNumber(value: unknown): number | null {
  if (value && typeof value === 'object' && 'value' in (value as Record<string, unknown>)) {
    return toNumber((value as { value: unknown }).value);
  }
  return toNumber(value);
}

function tooltipValue(value: unknown, unit: ValueUnit): string {
  const num = rawNumber(value);
  if (num === null) return '—';
  if (unit === 'pct') return `${(num * 100).toFixed(2)}%`;
  if (unit === 'money') return num.toFixed(2);
  if (unit === 'count') return String(Math.round(num));
  return Number.isInteger(num) ? String(num) : num.toFixed(2);
}

function axisBase(categories?: string[], unit: ValueUnit = 'plain') {
  const yFormatter = unit === 'pct' ? pctTick : undefined;
  const axisFormatter = unit === 'money' ? moneyTick : yFormatter;
  return {
    grid: { left: 56, right: 16, top: 16, bottom: 34 },
    tooltip: {
      trigger: 'axis' as const,
      confine: true,
      // plain（IC 这类小数值）不接管：ECharts 默认就显示原值，别把精度截短
      ...(['pct', 'money', 'count'].includes(unit)
        ? { valueFormatter: (value: unknown) => tooltipValue(value, unit) }
        : {}),
    },
    xAxis: {
      type: 'category' as const,
      ...(categories ? { data: categories } : {}),
      axisLabel: { color: C.axis, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: C.split } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value' as const,
      axisLabel: {
        color: C.axis,
        fontSize: 10,
        ...(axisFormatter ? { formatter: axisFormatter } : {}),
      },
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
  name: string,
  unit: ValueUnit = 'plain'
): Record<string, unknown> {
  return { ...axisBase(categories, unit), series: [{ type: 'bar', name, data }] };
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

/** 0 轴虚线：正负分界（IC 忽正忽负、回撤从 0 往下）靠它一眼看出来 */
function zeroMarkLine(): Record<string, unknown> {
  return {
    symbol: 'none',
    silent: true,
    data: [{ yAxis: 0 }],
    lineStyle: { type: 'dashed', color: C.axis },
    label: { show: false },
  };
}

function lineTrendColor(pts: EvalSeriesPoint[]): string {
  const values = valuesOf(pts);
  if (values.length < 2) return C.neutral;
  // 整条线的颜色按窗口方向给（与卡片同一条序列），单点无从谈方向 → 中性色
  return values[values.length - 1] >= values[0] ? C.up : C.down;
}

/** 净值线：红涨绿跌；点少时必须显点（账户快照只有个位数天） */
function equityLineOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  const color = lineTrendColor(pts);
  return {
    ...axisBase(pts.map((p) => String(p.date || p.label || '')), 'money'),
    series: [
      {
        type: 'line',
        name: '净值',
        data: valuesOf(pts),
        showSymbol: pts.length <= SAMPLE_SYMBOL_MAX,
        symbolSize: 4,
        lineStyle: { width: 1.6, color },
        itemStyle: { color },
      },
    ],
  };
}

/** 回撤面积：负向深度用绿（A 股绿=亏向），0 轴虚线标出「没回撤」那条线 */
function drawdownAreaOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  return {
    ...axisBase(pts.map((p) => String(p.date || p.label || '')), 'pct'),
    series: [
      {
        type: 'line',
        name: '回撤',
        data: valuesOf(pts),
        showSymbol: pts.length <= SAMPLE_SYMBOL_MAX,
        symbolSize: 4,
        lineStyle: { width: 1.4, color: C.down },
        itemStyle: { color: C.down },
        areaStyle: { color: C.down, opacity: 0.16 },
        markLine: zeroMarkLine(),
      },
    ],
  };
}

/** 有正负的柱：月度收益 / 当日盈亏 / 事后超额，正红负绿 */
function signedBarOption(pts: EvalSeriesPoint[], key: string): Record<string, unknown> {
  return barChart(
    pts.map((p) => String(p.label ?? p.date ?? '')),
    pts.map((p) => ({
      value: Number(p.value),
      itemStyle: { color: signColor(Number(p.value)) },
    })),
    labelOf(key),
    unitOf(key)
  );
}

/** 计数柱（入选数）：没有正负，统一中性色；0 是真空仓，不是缺测 */
function countBarOption(pts: EvalSeriesPoint[], key: string): Record<string, unknown> {
  return barChart(
    pts.map((p) => String(p.date || p.label || '')),
    pts.map((p) => ({ value: Number(p.value), itemStyle: { color: C.neutral } })),
    labelOf(key),
    unitOf(key)
  );
}

/** 命中率线：50% 抛硬币基准虚线（图上有这把尺子，才知道 0.62 算不算高） */
function rateLineOption(pts: EvalSeriesPoint[]): Record<string, unknown> {
  return {
    ...axisBase(pts.map((p) => String(p.date || p.label || '')), 'pct'),
    series: [
      {
        type: 'line',
        name: '命中率',
        data: valuesOf(pts),
        showSymbol: pts.length <= SAMPLE_SYMBOL_MAX,
        symbolSize: 4,
        lineStyle: { width: 1.6, color: C.neutral },
        itemStyle: { color: C.neutral },
        markLine: {
          symbol: 'none',
          silent: true,
          data: [
            {
              yAxis: HIT_BASE_LINE,
              label: { formatter: '抛硬币 50%', color: C.axis, fontSize: 9, position: 'insideEndTop' },
            },
          ],
          lineStyle: { type: 'dashed', color: C.axis },
        },
      },
    ],
  };
}

const OPTION_BUILDERS: Record<
  SeriesKind,
  (pts: EvalSeriesPoint[], key: string) => Record<string, unknown>
> = {
  ic_line: icLineOption,
  decile_bar: decileOption,
  decay_bar: decayOption,
  segment_bar: segmentOption,
  turnover_line: turnoverOption,
  corr_bar: corrOption,
  equity_line: equityLineOption,
  drawdown_area: drawdownAreaOption,
  signed_bar: signedBarOption,
  count_bar: countBarOption,
  rate_line: rateLineOption,
};

/**
 * 单条序列 → ECharts option；无数据 → null（不给空图）。
 *
 * `key` 是后端序列键（缺省取图形的惯例键）——同一图形会挂到不同序列上：
 * `signed_bar` 在策略上是 `monthly_return`、在账户上是 `daily_pnl`。
 */
export function seriesToOption(
  kind: SeriesKind,
  data: EvalSeriesData | null | undefined,
  key?: string
): Record<string, unknown> | null {
  const seriesKey = key || SERIES_KEYS[kind];
  const pts = points(data, seriesKey);
  if (pts.length === 0) return null;
  return OPTION_BUILDERS[kind](pts, seriesKey);
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

function equityAnswer(pts: EvalSeriesPoint[]): string {
  if (pts.length < 2) return `只有 ${pts.length} 个净值点，画不出曲线`;
  const first = Number(pts[0].value);
  const last = Number(pts[pts.length - 1].value);
  const head = `窗口净值 ${formatNumber(first, 0)} → ${formatNumber(last, 0)}（${pts.length} 点）`;
  if (first <= 0) return `${head}；首值为 0，区间涨跌幅无从算`;
  return `${head}，区间 ${formatSignedPct(last / first - 1, 2)}`;
}

function drawdownAnswer(pts: EvalSeriesPoint[]): string {
  if (pts.length < 2) return `只有 ${pts.length} 个回撤点，画不出曲线`;
  const deepest = pts.reduce((acc, p) => (Number(p.value) < Number(acc.value) ? p : acc), pts[0]);
  const flat = valuesOf(pts).filter((value) => value === 0).length;
  return `图上最深回撤 ${formatPercent(deepest.value, 2)}（${deepest.date || '—'}）；${flat}/${
    pts.length
  } 个交易日无回撤`;
}

function signedBarAnswer(pts: EvalSeriesPoint[], key: string): string {
  const unit = unitOf(key);
  if (pts.length < 2) return `只有 ${pts.length} 期 ${labelOf(key)}，看不出正负节奏`;
  const values = valuesOf(pts);
  const positive = values.filter((value) => value > 0).length;
  const mean = meanOf(values);
  const meanText = unit === 'pct' ? formatSignedPct(mean, 2) : formatNumber(mean, 2);
  return `${pts.length} 期里 ${positive} 期为正（${formatPercent(
    positive / pts.length,
    1
  )}），均值 ${meanText}`;
}

function countBarAnswer(pts: EvalSeriesPoint[], key: string): string {
  const values = valuesOf(pts);
  const label = labelOf(key);
  if (pts.length < 2) return `只有 ${pts.length} 天有${label}（${formatNumber(values[0], 0)} 只），看不出节奏`;
  const empty = values.filter((value) => value === 0).length;
  const tail = empty ? `，${empty} 天空仓` : '';
  return `${pts.length} 天日均 ${meanOf(values).toFixed(1)} 只（最少 ${formatNumber(
    Math.min(...values),
    0
  )}、最多 ${formatNumber(Math.max(...values), 0)}${tail}）`;
}

function rateLineAnswer(pts: EvalSeriesPoint[]): string {
  if (pts.length < 2) return `只有 ${pts.length} 天有命中率，看不出水平`;
  const values = valuesOf(pts);
  const above = values.filter((value) => value > HIT_BASE_LINE).length;
  return `${pts.length} 天命中率均值 ${formatPercent(meanOf(values), 1)}（${above} 天在 50% 抛硬币线之上）`;
}

const ANSWER_BUILDERS: Record<
  SeriesKind,
  (pts: EvalSeriesPoint[], key: string) => string
> = {
  ic_line: icLineAnswer,
  decile_bar: decileAnswer,
  decay_bar: decayAnswer,
  segment_bar: segmentAnswer,
  turnover_line: turnoverAnswer,
  corr_bar: corrAnswer,
  equity_line: equityAnswer,
  drawdown_area: drawdownAnswer,
  signed_bar: signedBarAnswer,
  count_bar: countBarAnswer,
  rate_line: rateLineAnswer,
};

/** 每张图的一句话结论（数字全部来自所画的那条序列） */
export function seriesAnswer(
  kind: SeriesKind,
  data: EvalSeriesData | null | undefined,
  key?: string
): string {
  const seriesKey = key || SERIES_KEYS[kind];
  const pts = points(data, seriesKey);
  // 一点数据都没有 → 原因来自后端 notes（不是「0 个视界」这种自己编的说法）
  if (pts.length === 0) return noteFor(data, seriesKey);
  return ANSWER_BUILDERS[kind](pts, seriesKey);
}

export interface SeriesChart {
  kind: SeriesKind;
  /** 后端序列键（同一图形在不同对象上挂不同序列） */
  key: string;
  question: string;
  answer: string;
  /** 后端 notes 原文（截断/缺测/样本量）；无数据的图这里是空串（原因已在 answer） */
  note: string;
  /** null = 这项没有序列（answer 说明原因），调用方渲染说明而不是空图 */
  option: Record<string, unknown> | null;
}

/**
 * 该类对象的全部一问一图。
 *
 * **侧车整个不存在**（`data` 为 null 或没有 `series` 键）→ 空数组，调用方改渲染
 * `/eval/series` 的 `meta.note`；**侧车在但某些序列为空** → 照常排图位，
 * 缺的那张用 `answer` 说明原因（缺一个序列不该让另外几张跟着消失）。
 */
export function buildSeriesCharts(
  objectType: string,
  data: EvalSeriesData | null | undefined
): SeriesChart[] {
  if (!data?.series || Object.keys(data.series).length === 0) return [];
  const slots = SERIES_SLOTS[String(objectType || '')] || DEFAULT_SLOTS;
  return slots.map((item) => {
    const option = seriesToOption(item.kind, data, item.key);
    return {
      kind: item.kind,
      key: item.key,
      question: item.question,
      answer: seriesAnswer(item.kind, data, item.key),
      // 有数据才贴后端 note：没数据时同一句话已经在 answer 里，贴两遍是噪音
      note: option ? backendNote(data, item.key) : '',
      option,
    };
  });
}
