/** 美股市场宽度 —— 均线站位（% 站上 MA50 / MA200）与 A-D 线（累计涨跌家数）
 * 样本为标普500 + 纳指补充约 517 只（非全市场）；A-D 线在 60 日窗口起点归零，只反映窗口内累计强弱。
 * 后端字段在数据不足时可能为 null，一律先过 safeNum 再展示/绘图（禁止裸 .toFixed）。
 * 排版按看盘密度：摘要压成一行指标条，图表内边距与切换按钮一并收窄。
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

const VIEWS = [
  { id: 'ad', label: 'A-D 线', hint: '累计涨跌家数，60 日窗口起点归零，只反映窗口内累计强弱' },
  { id: 'ma', label: '均线站位', hint: '收盘价站上 MA50 / MA200 的成分股占比' },
];

/** 图表高度：看盘页优先密度，够看趋势即可 */
const CHART_HEIGHT = 240;

const AD_COLOR = '#2563eb'; // blue-600
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

export const UsBreadthPanel: React.FC<{ className?: string }> = ({ className = '' }) => {
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
  const adLine = safeNum(summary?.ad_line);
  const ma50 = safeNum(summary?.pct_above_ma50);
  const ma200 = safeNum(summary?.pct_above_ma200);
  const newHighs = safeNum(summary?.new_highs);
  const newLows = safeNum(summary?.new_lows);

  // 站上均线占比 ≥50% 视为多数偏强（红），<50% 偏弱（绿）
  const pctTone = (n: number | null) =>
    n === null ? 'text-slate-400' : n >= 50 ? 'text-red-600' : 'text-green-600';
  const countTone = (n: number | null, tone = 'text-red-600') =>
    n === null ? 'text-slate-400' : tone;

  // 一行指标条：A-D 线 + 两条均线站位 + 新高新低家数（均为后端 summary 口径）
  const cells = [
    {
      label: 'A-D 线',
      value: fmtInt(adLine),
      tone:
        adLine === null ? 'text-slate-400' : adLine >= 0 ? 'text-red-600' : 'text-green-600',
      title: '60 日窗口内累计涨跌家数（窗口起点归零，只反映窗口内强弱）',
    },
    {
      label: '% 站上 MA50',
      value: fmtPct1(ma50),
      tone: pctTone(ma50),
      title: '收盘价高于 50 日均线的股票占比（≥50% 为多数偏强）',
    },
    {
      label: '% 站上 MA200',
      value: fmtPct1(ma200),
      tone: pctTone(ma200),
      title: '收盘价高于 200 日均线的股票占比（≥50% 为多数偏强）',
    },
    {
      label: '创新高',
      value: fmtInt(newHighs),
      tone: countTone(newHighs),
      title: '当日创 52 周新高的股票数',
    },
    {
      label: '创新低',
      value: fmtInt(newLows),
      tone: countTone(newLows, 'text-green-600'),
      title: '当日创 52 周新低的股票数',
    },
  ];

  useEffect(() => {
    if (!chartRef.current || points.length === 0) return;
    if (!instanceRef.current) {
      instanceRef.current = echarts.init(chartRef.current);
    }
    const chart = instanceRef.current;
    const dates = points.map((p) => p.date);

    const baseAxis = {
      grid: { left: 6, right: 10, top: 22, bottom: 2, containLabel: true },
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
                ...lineSeries('A-D 线', AD_COLOR, points.map((p) => safeNum(p.ad_line))),
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
                ...lineSeries('站上 MA50', AD_COLOR, points.map((p) => safeNum(p.pct_above_ma50))),
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
      className={`!p-2.5 !gap-1.5 ${className}`}
      title={
        <span className="flex items-center gap-1.5">
          <Activity className="w-3.5 h-3.5 text-blue-600" />
          市场宽度：均线站位与 A-D 线
          <span className="text-[9px] font-normal text-slate-400">
            标普500 + 纳指补充约 517 只
          </span>
        </span>
      }
      extra={<DateBadge label="数据日期" date={data?.trade_date} />}
    >
      {/* 摘要指标条：一行放完 5 个口径，替代原 4 张大卡 */}
      <div className="rounded-xl border border-blue-100/70 bg-gradient-to-b from-blue-50/40 to-white px-2.5 py-1.5 flex items-center gap-x-4 gap-y-1 flex-wrap">
        {cells.map((c) => (
          <span key={c.label} className="flex items-baseline gap-1 whitespace-nowrap" title={c.title}>
            <span className="text-[10px] font-bold text-slate-400">{c.label}</span>
            <span className={`text-[13px] font-extrabold font-mono ${c.tone}`}>{c.value}</span>
          </span>
        ))}
      </div>

      {/* 图表切换：紧凑按钮组 */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              onClick={() => setView(v.id)}
              title={v.hint}
              className={`px-2 py-[3px] rounded-md text-[10px] font-extrabold transition-colors ${
                view === v.id
                  ? 'bg-blue-600 text-white'
                  : 'bg-slate-100 text-slate-500 hover:bg-slate-200 hover:text-slate-700'
              }`}
            >
              {v.label}
            </button>
          ))}
          <span className="ml-1 text-[9px] font-mono text-slate-400 whitespace-nowrap">
            {view === 'ad' ? '窗口起点归零' : '0-100% 区间'}
          </span>
        </div>
        <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
          近 {points.length} 个交易日
        </span>
      </div>

      {points.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div ref={chartRef} style={{ width: '100%', height: CHART_HEIGHT }} />
      )}
    </SectionCard>
  );
};
