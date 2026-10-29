/**
 * 市场分析 · 热力矩形图谱着色口径（板块层与个股下钻层共用的纯函数）
 *
 * A 股口径：**涨红跌绿**（与全站一致，见 moneyFlowService.flowTextClass）。
 * - 「涨跌幅」按档位着色：±10（涨/跌停）、±5、±2、平盘，机构图谱惯例；
 * - 「主力净额」按符号 + 幅度分档着色：净流入红、净流出绿。
 *
 * 单位约定：与后端 /heatmap、/heatmap/drill 契约一致 —— `pct_change` 为 %，
 * `flow_net` 与 `value`（面积规模）均为**亿元**。本文件只贡献颜色与文本，
 * 不做金额换算，避免与后端口径重复换算（DRY）。
 */

export type HeatmapColorMode = 'pct' | 'flow' | 'accel';

/** 涨跌幅档位色（A 股涨红跌绿；阈值与「涨/跌停=±10%」口径对齐）。 */
export function pctToColor(pct: number | null | undefined): string {
  const v = typeof pct === 'number' && Number.isFinite(pct) ? pct : 0;
  if (v >= 10) return '#dc2626';
  if (v >= 5) return '#ef4444';
  if (v > 2) return '#f87171';
  if (v >= 0) return '#94a3b8';
  if (v > -5) return '#34d399';
  if (v > -10) return '#10b981';
  return '#059669';
}

/** 主力净额档位色（单位：亿元）。幅度越大颜色越深。 */
export function flowToColor(flowYi: number | null | undefined): string {
  const v = typeof flowYi === 'number' && Number.isFinite(flowYi) ? flowYi : 0;
  if (v > 1) return '#dc2626'; // 净流入 > 1 亿
  if (v > 0) return '#f87171';
  if (v === 0) return '#94a3b8';
  if (v > -1) return '#34d399';
  return '#059669'; // 净流出 <= -1 亿
}

/** 加速度档位色（单位：亿/日）：越快越深，正=加速流入，负=加速流出。 */
export function accelToColor(accelYi: number | null | undefined): string {
  const v = typeof accelYi === 'number' && Number.isFinite(accelYi) ? accelYi : 0;
  if (v >= 5) return '#dc2626';
  if (v >= 1) return '#ef4444';
  if (v > 0) return '#f87171';
  if (v === 0) return '#94a3b8';
  if (v > -1) return '#34d399';
  if (v > -5) return '#10b981';
  return '#059669';
}

export interface HeatmapCellMetric {
  pct_change?: number | null;
  flow_net?: number | null;
  flow_accel?: number | null;
}

/** 统一取色入口：按当前着色维度返回色值。 */
export function heatmapCellColor(item: HeatmapCellMetric, mode: HeatmapColorMode): string {
  if (mode === 'accel') return accelToColor(item.flow_accel);
  return mode === 'flow' ? flowToColor(item.flow_net) : pctToColor(item.pct_change);
}

/**
 * 矩形标签上的数值文本（带符号）。
 * 缺失一律显示「—」（与全站「缺失不显示成 0」口径一致）。
 */
export function heatmapLabelValue(item: HeatmapCellMetric, mode: HeatmapColorMode): string {
  if (mode === 'accel') {
    const v = item.flow_accel;
    if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}亿/日`;
  }
  if (mode === 'flow') {
    const v = item.flow_net;
    if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}亿`;
  }
  const p = item.pct_change;
  if (typeof p !== 'number' || !Number.isFinite(p)) return '—';
  return `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`;
}

/** 着色维度切换按钮文案（板块层与下钻层共用）。 */
export const HEATMAP_COLOR_LABEL: Record<HeatmapColorMode, string> = {
  pct: '涨跌幅',
  flow: '主力净额',
  accel: '加速度',
};
