/** 指标副图的纯逻辑：对齐、分组、配色。
 *
 *  指标数组是**全历史**跑出来的（limit=0），而 K 线可能只取最近 600 根 →
 *  必须按尾部对齐，否则指标会整体错位（画出来完全不对，且不报错）。
 */
import type { Time } from 'lightweight-charts';
import type { IndicatorSet } from '../../api/client';

/** 与 lightweight-charts 的 LineData / WhitespaceData 结构兼容：
 *  有值就是点，没值（预热期 NA）只给 time → 画成断口，而不是连一条假线过去。 */
export interface IndPoint {
  time: Time;
  value?: number;
}

/** A股口径：红涨绿跌（K线、成交量、MACD 柱共用，图例也按这个取色） */
export const UP_COLOR = '#e2373b';
export const DOWN_COLOR = '#1a9e5c';

/** 与价格同量纲的叠加线（EMA/布林带/通道…） */
export const OVERLAY_COLORS = [
  '#7c3aed', '#0891b2', '#d97706', '#059669', '#db2777', '#475569', '#9333ea', '#0d9488',
];

/** 副图线（MACD/RSI/ATR…） */
export const PANE_COLORS = ['#2563eb', '#ea580c', '#a855f7', '#0f766e', '#b91c1c', '#4d7c0f'];

/** 全历史指标 → 当前 K 线窗口：K 线是尾部切片，指标也跟着取尾部。 */
export const alignSeries = (
  values: (number | null)[],
  bars: number,
): (number | null)[] => {
  if (bars <= 0 || values.length <= bars) return values;
  return values.slice(-bars);
};

/** 副图分组（同一条推导链的多条线共用一个副图），顺序即副图顺序。 */
export const paneGroups = (set: IndicatorSet | null | undefined): string[] => {
  const out: string[] = [];
  for (const s of set?.panes ?? []) {
    if (s.group && !out.includes(s.group)) out.push(s.group);
  }
  return out;
};

/** 某条序列的颜色：按它在同类里的出现次序轮取调色板（组内不重色、组间也不撞）。 */
export const seriesColor = (overlay: boolean, index: number): string => {
  const palette = overlay ? OVERLAY_COLORS : PANE_COLORS;
  return palette[index % palette.length];
};

/** 指标值 → 图表点（按 K 线时间轴对齐）。
 *
 *  指标比 K 线短说明两者不同源（换了标的/周期），此时宁可整条不画，也不要画一条错位的线。
 */
export const toPoints = (
  bars: { date: string }[],
  values: (number | null)[],
): IndPoint[] => {
  const aligned = alignSeries(values, bars.length);
  if (aligned.length !== bars.length) return [];
  return aligned.map((v, i) => (v == null
    ? { time: bars[i].date as unknown as Time }
    : { time: bars[i].date as unknown as Time, value: v }));
};
