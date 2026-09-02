/** 通用时序图：多列折线，独立 y 轴分组，推理中心玻璃卡风格 */

import { useMemo } from 'react';
import ReactECharts from 'echarts-for-react';
import { SeriesResponse } from '../../services/stockTerminalService';

const PALETTE = ['#3b82f6', '#f59e0b', '#8b5cf6', '#10b981', '#e11d48', '#0ea5e9', '#f97316'];

export interface SeriesSpec {
  key: string;
  name: string;
  color: string;
}

/** 由列名+别名+颜色构造 series */
export function buildSeries(resp: SeriesResponse, specs: { key: string; name: string; color?: string }[]): SeriesSpec[] {
  return specs
    .filter(s => resp.columns[s.key]?.length)
    .map((s, i) => ({ key: s.key, name: s.name || s.key, color: s.color || PALETTE[i % PALETTE.length] }));
}

interface Props {
  resp: SeriesResponse;
  series: SeriesSpec[];
  height?: number;
  tooltipFmt?: (name: string, v: number | null) => string;
}

export function SeriesChart({ resp, series, height = 220, tooltipFmt }: Props) {
  const option = useMemo(() => {
    const dates = resp.dates;
    return {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(255,255,255,0.96)',
        borderColor: '#e2e8f0',
        textStyle: { color: '#334155', fontSize: 11 },
        valueFormatter: (v: any, dataIndex: number) => {
          if (tooltipFmt && series[dataIndex]) return tooltipFmt(series[dataIndex].name, v);
          return v == null ? '--' : String(Number(v).toFixed(3));
        },
      },
      grid: { left: 54, right: 16, top: 24, bottom: 20 },
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, color: '#94a3b8' } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10, color: '#64748b' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
      legend: { top: 0, right: 8, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 10, color: '#64748b' }, type: 'scroll' },
      series: series.map(s => ({
        name: s.name,
        type: 'line',
        symbol: 'none',
        lineStyle: { width: 1.4, color: s.color },
        itemStyle: { color: s.color },
        data: resp.columns[s.key] ?? [],
        emphasis: { disabled: true },
      })),
    };
  }, [resp, series, tooltipFmt]);

  return (
    <ReactECharts option={option} notMerge lazyUpdate style={{ width: '100%', height }} opts={{ renderer: 'canvas' }} />
  );
}

export { PALETTE };
