/** 美股市场宽度 —— 均线站位（% 站上 MA50 / MA200）与 A-D 线（累计涨跌家数）
 * 样本为标普500 + 纳指补充约 517 只（非全市场）；A-D 线在 60 日窗口起点归零，只反映窗口内累计强弱。
 * 后端字段在数据不足时可能为 null，一律先过 safeNum 再展示/绘图（禁止裸 .toFixed）。
 */

import React, { useEffect, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Activity } from 'lucide-react';
import { getBreadthHistory } from '../services/api';
import type { UsBreadthHistory, UsBreadthPoint } from '../types';
import { SectionCard, EmptyHint, DateBadge, fmtInt } from '../../market-analysis-shared/ui';

/** null / NaN / Infinity / 非数字 一律收敛为 null，由展示层决定降级文案 */
const safeNum = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null;

/** 百分比读数（1 位小数），无效值显示 '--' */
const fmtPct1 = (v: unknown): string => {
  const n = safeNum(v);
  return n === null ? '--' : `${n.toFixed(1)}%`;
};

const VIEWS = [{ id: 'ad', label: 'A-D 线' }, { id: 'ma', label: '均线站位' }];

const MA50_COLOR = '#2563eb'; // blue-600
const MA200_COLOR = '#7c3aed'; // purple-600

const TOOLTIP_BASE = {
  trigger: 'axis' as const,
  axisPointer: { type: 'line' as const, lineStyle: { color: '#cbd5e1' } },
  backgroundColor: 'rgba(255,255,255,0.96)',
  borderColor: '#e2e8f0',
  textStyle: { fontSize: 10, color: '#334155' },
};

const VALUE_AXIS = {
  type: 'value' as const,
  axisLabel: { fontSize: 9, color: '#94a3b8', formatter: (v: number) => fmtInt(v) },
  splitLine: { lineStyle: { color: '#f1f5f9' } },
};

const lineSeries = (name: string, color: string, data: Array<number | null>) => ({
  name,
  type: 'line' as const,
  smooth: true,
  symbol: 'none' as const,
  connectNulls: false,
  data,
  lineStyle: { width: 2, color },
  itemStyle: { color },
});

export const UsBreadthPanel: React.FC = () => {
  const [data, setData] = useState<UsBreadthHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState('ad');
  const chartRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    let alive = true;
    getBreadthHistory(60)
      .then((d) => {
        if (alive) setData(d);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const points: UsBreadthPoint[] = data && Array.isArray(data.points) ? data.points : [];
  const summary = data?.summary;
  const ma50 = safeNum(summary?.pct_above_ma50);
  const ma200 = safeNum(summary?.pct_above_ma200);
  const newHighs = safeNum(summary?.new_highs);
  const newLows = safeNum(summary?.new_lows);

  // 摘要卡：站上均线占比 ≥50% 视为多数偏强（红），<50% 偏弱（绿）
  const pctCard = (label: string, n: number | null, title: string) => ({
    label,
    value: fmtPct1(n),
    color: n === null ? 'text-slate-400' : n >= 50 ? 'text-red-600' : 'text-green-600',
    title,
  });
  const cntCard = (label: string, n: number | null, color: string, title: string) => ({
    label,
    value: fmtInt(n),
    color: n === null ? 'text-slate-400' : color,
    title,
  });
  const cards = [
    pctCard('% 站上 MA50', ma50, '收盘价高于 50 日均线的股票占比（≥50% 为多数偏强）'),
    pctCard('% 站上 MA200', ma200, '收盘价高于 200 日均线的股票占比（≥50% 为多数偏强）'),
    cntCard('创新高家数', newHighs, 'text-red-600', '当日创 52 周新高的股票数'),
    cntCard('创新低家数', newLows, 'text-green-600', '当日创 52 周新低的股票数'),
  ];

  useEffect(() => {
    if (!chartRef.current || points.length === 0) return;
    if (!instanceRef.current) {
      instanceRef.current = echarts.init(chartRef.current);
    }
    const chart = instanceRef.current;
    const dates = points.map((p) => p.date);

    const baseAxis = {
      grid: { left: 6, right: 10, top: 28, bottom: 4, containLabel: true },
      animationDuration: 300,
      xAxis: {
        type: 'category' as const,
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
    };

    // A-D 视图 tooltip 带当日涨跌家数；均线视图 tooltip 按系列逐行列出
    const adTooltip = (params: any) => {
      const p = Array.isArray(params) ? params[0] : params;
      const pt = points[p?.dataIndex];
      return [
        `<b>${pt?.date ?? ''}</b>`,
        `A-D 线: ${fmtInt(safeNum(p?.value))}`,
        `<span style="color:#ef4444">上涨 ${fmtInt(safeNum(pt?.advancers))}</span> / <span style="color:#10b981">下跌 ${fmtInt(safeNum(pt?.decliners))}</span>`,
      ].join('<br/>');
    };
    const maTooltip = (params: any) => {
      const arr: any[] = Array.isArray(params) ? params : [params];
      if (!arr.length) return '';
      const lines = arr.map((p) => `${p.marker}${p.seriesName}: ${fmtPct1(p.value)}`).join('<br/>');
      return `<b>${points[arr[0]?.dataIndex]?.date ?? ''}</b><br/>${lines}`;
    };

    const option: echarts.EChartsOption =
      view === 'ad'
        ? {
            ...baseAxis,
            tooltip: { ...TOOLTIP_BASE, formatter: adTooltip },
            yAxis: VALUE_AXIS,
            series: [
              {
                ...lineSeries('A-D 线', MA50_COLOR, points.map((p) => safeNum(p.ad_line))),
                areaStyle: { opacity: 0.08, color: '#3b82f6' },
              },
            ],
          }
        : {
            ...baseAxis,
            legend: {
              top: 0,
              right: 0,
              itemWidth: 10,
              itemHeight: 8,
              textStyle: { fontSize: 9, color: '#64748b', fontWeight: 700 },
              data: ['站上 MA50', '站上 MA200'],
            },
            tooltip: { ...TOOLTIP_BASE, formatter: maTooltip },
            yAxis: {
              ...VALUE_AXIS,
              min: 0,
              max: 100,
              axisLabel: { ...VALUE_AXIS.axisLabel, formatter: (v: number) => `${v}%` },
            },
            series: [
              {
                ...lineSeries('站上 MA50', MA50_COLOR, points.map((p) => safeNum(p.pct_above_ma50))),
                markLine: {
                  silent: true,
                  symbol: 'none',
                  lineStyle: { color: '#cbd5e1', type: 'dashed' },
                  label: { formatter: '50%', fontSize: 9, color: '#94a3b8' },
                  data: [{ yAxis: 50 }],
                },
              },
              lineSeries('站上 MA200', MA200_COLOR, points.map((p) => safeNum(p.pct_above_ma200))),
            ],
          };
    chart.setOption(option, true);

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(chartRef.current);
    return () => ro.disconnect();
  }, [data, view]);

  useEffect(() => () => {
    instanceRef.current?.dispose();
    instanceRef.current = null;
  }, []);

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <Activity className="w-3.5 h-3.5 text-blue-600" />
          市场宽度：均线站位与 A-D 线
        </span>
      }
      extra={<DateBadge label="数据日期" date={data?.trade_date} />}
    >
      {/* 摘要条 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {cards.map((c) => (
          <div key={c.label} title={c.title}
            className="rounded-xl border border-blue-100/70 bg-gradient-to-b from-blue-50/50 to-white px-3 py-2 flex flex-col gap-0.5">
            <span className="text-[10px] font-bold text-slate-400">{c.label}</span>
            <span className={`text-lg font-extrabold font-mono ${c.color}`}>{c.value}</span>
          </div>
        ))}
      </div>

      {/* 图表切换 */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1 rounded-full bg-slate-100 p-0.5">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              onClick={() => setView(v.id)}
              className={`px-3 py-1 rounded-full text-[11px] font-extrabold transition-all ${view === v.id ? 'bg-white text-blue-700 shadow-2xs border border-blue-200' : 'text-slate-500 hover:text-slate-800'}`}
            >
              {v.label}
            </button>
          ))}
        </div>
        <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap">
          {view === 'ad' ? '窗口起点归零' : '0-100% 区间'}
        </span>
      </div>

      {points.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div ref={chartRef} style={{ width: '100%', height: 300 }} />
      )}
    </SectionCard>
  );
};
