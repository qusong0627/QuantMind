/**
 * 升级前就有的四张图的 option 构造器（**只搬不改口径**）。
 *
 * 抽出来的唯一理由：概览页签与分组回测页签**都要用同一张图**。复制一份就会有
 * 两份 ECharts option，改一处漏一处 —— 这正是本次要消灭的重复模式。
 * 数值换算、颜色、分位色阶全部与 `FactorDetailCharts.tsx` 逐字一致，
 * 保证「原有图表一张不少且长得一样」。
 */

import type { FactorDetail } from '../../../types/factorReport';
import { quantileColor } from './chartKit';
import { fmtDate } from './chartKit';

/** 分位标签 Q1..Q10 */
export const qLabels = (n: number): string[] => Array.from({ length: n }, (_, i) => `Q${i + 1}`);

/** 分位平均前瞻收益（柱） */
export function quantileBarOption(detail: FactorDetail): any {
  const qMean = detail.quantile_mean.map((v) => +(v * 100).toFixed(3));
  return {
    grid: { left: 42, right: 12, top: 18, bottom: 24 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v > 0 ? '+' : ''}${v}%` },
    xAxis: { type: 'category', data: qLabels(qMean.length), axisLabel: { fontSize: 10, color: '#94a3b8' }, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#94a3b8', formatter: '{value}%' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: [{
      type: 'bar',
      data: qMean.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? '#e11d48' : '#059669', borderRadius: 3 } })),
      barMaxWidth: 22,
      label: { show: true, position: 'top', fontSize: 9, color: '#64748b' },
    }],
  };
}

/** Q10−Q1 分位价差（%） */
export function quantileSpread(detail: FactorDetail): number {
  const m = detail.quantile_mean;
  return +(((m[m.length - 1] ?? 0) - (m[0] ?? 0)) * 100).toFixed(3);
}

/**
 * 分位净值曲线（10 条 + 多空虚线）。
 * `withLegend` 供分组回测页签用（那里是主图，10 条线不标图例读不出来）；
 * 概览页签保持升级前的无图例形态。
 */
export function quantileCurveOption(detail: FactorDetail, withLegend = false): any {
  const labels = qLabels(detail.quantile_curves.length);
  const series: any[] = detail.quantile_curves.map((curve, i) => ({
    name: labels[i],
    type: 'line',
    data: curve.map((v) => +(v * 100 - 100).toFixed(2)),
    showSymbol: false,
    lineStyle: { width: i === 0 || i === 9 ? 2 : 1, color: quantileColor(i, detail.quantile_curves.length) },
    itemStyle: { color: quantileColor(i, detail.quantile_curves.length) },
  }));
  series.push({
    name: '多空(Q10-Q1)',
    type: 'line',
    data: detail.ls_curve.map((v) => +(v * 100 - 100).toFixed(2)),
    showSymbol: false,
    lineStyle: { width: 2, type: 'dashed', color: '#4f46e5' },
    itemStyle: { color: '#4f46e5' },
  });
  const dateLabels = detail.dates.map(fmtDate);
  return {
    grid: { left: 46, right: 14, top: withLegend ? 30 : 22, bottom: 26 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v > 0 ? '+' : ''}${v}%` },
    legend: withLegend
      ? {
          show: true, type: 'scroll', top: 0, left: 0, right: 0,
          itemWidth: 12, itemHeight: 6, itemGap: 8,
          textStyle: { fontSize: 9, color: '#94a3b8' },
        }
      : { show: false },
    xAxis: { type: 'category', data: dateLabels, axisLabel: { fontSize: 10, color: '#94a3b8', interval: Math.max(1, Math.floor(dateLabels.length / 6)) }, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#94a3b8', formatter: '{value}%' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series,
  };
}

/** 换手率时序（面积）—— 口径是截面分位成员迁移比例，非组合换手 */
export function turnoverOption(detail: FactorDetail): any {
  return {
    grid: { left: 46, right: 14, top: 18, bottom: 26 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${(v * 100).toFixed(0)}%` },
    xAxis: { type: 'category', data: detail.turnover_dates.map(fmtDate), axisLabel: { fontSize: 10, color: '#94a3b8', interval: Math.max(1, Math.floor(detail.turnover_dates.length / 6)) }, axisTick: { show: false } },
    yAxis: { type: 'value', max: 1, axisLabel: { fontSize: 10, color: '#94a3b8', formatter: (v: number) => `${(v * 100).toFixed(0)}%` }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: [{
      name: '单边换手',
      type: 'line',
      data: detail.turnover_series.map((v) => +v.toFixed(3)),
      showSymbol: false,
      areaStyle: { color: 'rgba(245, 158, 11, 0.18)' },
      lineStyle: { width: 1.6, color: '#f59e0b' },
      itemStyle: { color: '#f59e0b' },
    }],
  };
}
