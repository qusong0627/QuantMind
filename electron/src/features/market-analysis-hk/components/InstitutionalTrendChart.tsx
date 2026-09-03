/** 机构持仓趋势图 —— 分类持仓量堆叠面积（原生 echarts） */

import React, { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import type { InstitutionalTrendSeries } from '../types';
import { fmtInt } from './shared/ui';

export const INST_CATEGORY_COLORS: Record<string, string> = {
  cn_broker: '#ef4444', // 内资·中資券商（红）
  southbound: '#f97316', // 内资·港股通（橙）
  hk: '#3b82f6', // 港资（蓝）
  us_eu: '#8b5cf6', // 外资·欧美（紫）
  apac: '#14b8a6', // 外资·亚太（青）
  other: '#94a3b8', // 其他（灰）
};

interface InstitutionalTrendChartProps {
  dates: string[];
  series: InstitutionalTrendSeries[];
  height?: number;
}

export const InstitutionalTrendChart: React.FC<InstitutionalTrendChartProps> = ({
  dates,
  series,
  height = 230,
}) => {
  const chartRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!chartRef.current || !dates.length || !series.length) return;
    if (!instanceRef.current) {
      instanceRef.current = echarts.init(chartRef.current);
    }
    const chart = instanceRef.current;

    const option: echarts.EChartsOption = {
      animationDuration: 400,
      grid: { left: 6, right: 8, top: 30, bottom: 4, containLabel: true },
      legend: {
        top: 0,
        right: 0,
        itemWidth: 10,
        itemHeight: 8,
        textStyle: { fontSize: 9, color: '#64748b', fontWeight: 700 },
        data: series.map((s) => s.label),
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: '#cbd5e1' } },
        backgroundColor: 'rgba(255,255,255,0.96)',
        borderColor: '#e2e8f0',
        textStyle: { fontSize: 10, color: '#334155' },
        formatter: (params) => {
          const p = params as Array<{ seriesName: string; dataIndex: number; value: number }>;
          const d = p[0];
          const lines = p
            .map((it) => `${it.seriesName}: ${fmtInt(it.value)} 股`)
            .join('<br/>');
          return `<b>${dates[d.dataIndex]}</b><br/>${lines}`;
        },
      },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisTick: { show: false },
        axisLabel: {
          fontSize: 9,
          color: '#94a3b8',
          interval: (index: number) => index % 10 === 0 || index === dates.length - 1,
        },
      },
      yAxis: {
        type: 'value',
        axisLabel: {
          fontSize: 9,
          color: '#94a3b8',
          formatter: (v: number) => `${(v / 1e8).toFixed(v >= 1e9 ? 0 : 1)}亿`,
        },
        splitLine: { lineStyle: { color: '#f1f5f9' } },
      },
      series: series.map((s) => ({
        name: s.label,
        type: 'line',
        stack: 'inst',
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 0 },
        areaStyle: { opacity: 0.3 },
        itemStyle: { color: INST_CATEGORY_COLORS[s.category] || '#94a3b8' },
        emphasis: { focus: 'series' },
        data: s.values,
      })),
    };
    chart.setOption(option, true);

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(chartRef.current);
    return () => ro.disconnect();
  }, [dates, series]);

  useEffect(() => {
    return () => {
      instanceRef.current?.dispose();
      instanceRef.current = null;
    };
  }, []);

  if (!dates.length || !series.length) return null;
  return <div ref={chartRef} style={{ width: '100%', height }} />;
};