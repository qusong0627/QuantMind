/** 聪明钱 · 选股榜图表层（v3：图表优先，明细可切回表格）。
 *
 * 三张图分工：
 *  - 综合分横条：先看谁排前面（名字 + 分数）；
 *  - 六柱热力图：看每只票的分位构成，哪一柱强/弱一眼扫完；
 *  - 综合分 × 机构净买散点：找“分数高且机构真金白银买”的右上象限。
 * 缺失分位保留「—」灰格，绝不补 0；点击图元打开个股档案抽屉。
 */

import React, { useMemo } from 'react';
import type { SmartPillarMeta, SmartStockItem } from '../../services/smartMoneyService';
import { MISSING, eventTypeLabel, fmtScore, fmtYiFromYuan, pillarShort } from '../../smartMoneyVocabulary';
import { AXIS_LABEL, CHART_COLORS, SmartChart } from './charts';

const TOP_N = 15;
const STRIP_N = 5;

const EVENT_COLORS: Record<string, string> = {
  lhb: '#7c3aed',
  inst: '#2563eb',
  block_trade: '#d97706',
  survey: '#0891b2',
};

const scoreColor = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return CHART_COLORS.axis;
  return Number(v) >= 80 ? '#6d28d9' : Number(v) >= 60 ? '#7c3aed' : '#a78bfa';
};

const clickOf = (onPick?: (symbol: string) => void) => ({
  click: (params: unknown) => {
    const d = (params as { data?: { symbol?: string } })?.data;
    if (d?.symbol) onPick?.(d.symbol);
  },
});

/** 今日前 5：小而快的“先看谁”条，点击直接开档案 */
export const TopStockStrip: React.FC<{
  items: SmartStockItem[];
  onPick?: (symbol: string) => void;
}> = ({ items, onPick }) => {
  const top = items.slice(0, STRIP_N);
  if (!top.length) return null;
  return (
    <div className="grid grid-cols-2 xl:grid-cols-5 gap-1.5">
      {top.map((it, i) => {
        const score = it.score === null || it.score === undefined ? null : Number(it.score);
        return (
          <button
            key={it.symbol}
            type="button"
            onClick={() => onPick?.(it.symbol)}
            className="min-w-0 rounded-xl border border-slate-200 bg-white px-2 py-1.5 text-left hover:border-purple-300 hover:bg-purple-50/40 transition-colors"
          >
            <div className="flex items-center gap-1 min-w-0">
              <span
                className={`w-4 h-4 rounded grid place-items-center text-[9px] font-extrabold ${
                  i < 3 ? 'bg-purple-600 text-white' : 'bg-slate-100 text-slate-500'
                }`}
              >
                {i + 1}
              </span>
              <span className="text-[11px] font-bold text-slate-800 truncate">{it.name ?? it.symbol}</span>
              <span className="ml-auto text-[11px] font-mono font-extrabold text-purple-700">
                {fmtScore(it.score)}
              </span>
            </div>
            <div className="mt-0.5 flex items-center gap-1 text-[9px] text-slate-400">
              <span className="truncate">{it.industry ?? MISSING}</span>
              <span className="ml-auto shrink-0">
                {it.latest_event ? eventTypeLabel(it.latest_event.type) : MISSING}
              </span>
            </div>
            <div className="mt-1 h-1 rounded-full bg-slate-100 overflow-hidden">
              <div
                className="h-full rounded-full bg-purple-500"
                style={{ width: `${Math.max(2, Math.min(100, score ?? 0))}%` }}
              />
            </div>
          </button>
        );
      })}
    </div>
  );
};

/** 综合分横条：Top 20，按分数降序（图内自上而下） */
export const StockScoreBar: React.FC<{
  items: SmartStockItem[];
  onPick?: (symbol: string) => void;
}> = ({ items, onPick }) => {
  const top = useMemo(
    () =>
      items
        .filter((it) => it.score !== null && it.score !== undefined)
        .slice(0, TOP_N),
    [items],
  );

  const option = useMemo(() => {
    if (!top.length) return null;
    const ordered = [...top].reverse();
    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        textStyle: { fontSize: 10 },
        formatter: (ps: Array<{ dataIndex: number }>) => {
          const it = ordered[ps[0]?.dataIndex ?? 0];
          if (!it) return '';
          return [
            `${it.name ?? it.symbol} <span style="color:#94a3b8">${it.symbol}</span>`,
            `综合分 ${fmtScore(it.score)}`,
            `机构净买·5日 ${fmtYiFromYuan(it.inst_net_5d)} 亿`,
            `覆盖 ${it.covered ?? MISSING}/${it.n_pillars ?? MISSING} 柱`,
            it.latest_event ? `最近事件 ${eventTypeLabel(it.latest_event.type)} ${it.latest_event.date ?? ''}` : '',
          ]
            .filter(Boolean)
            .join('<br/>');
        },
      },
      grid: { left: 98, right: 42, top: 8, bottom: 20 },
      xAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { ...AXIS_LABEL },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      yAxis: {
        type: 'category',
        data: ordered.map((it) => it.name ?? it.symbol),
        axisLabel: { fontSize: 9, color: CHART_COLORS.neutral, width: 90, overflow: 'truncate' },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: CHART_COLORS.axis } },
      },
      series: [
        {
          type: 'bar',
          barMaxWidth: 12,
          data: ordered.map((it) => ({
            value: it.score,
            symbol: it.symbol,
            name: it.name,
            itemStyle: { color: scoreColor(it.score), borderRadius: [0, 3, 3, 0] },
          })),
          label: {
            show: true,
            position: 'right',
            fontSize: 9,
            color: CHART_COLORS.neutral,
            formatter: (p: { value: number | null }) =>
              p.value === null || p.value === undefined ? MISSING : Number(p.value).toFixed(0),
          },
        },
      ],
    };
  }, [top]);

  return (
    <div className="min-w-0">
      <div className="text-[10px] font-bold text-slate-500 px-1 pb-1">综合分 Top {top.length}</div>
      <SmartChart option={option} onEvents={clickOf(onPick)} height={Math.max(200, top.length * 18 + 34)} />
    </div>
  );
};

/** 六柱分位热力图：行=标的，列=入柱；颜色越深分位越高，缺值灰格 */
export const StockPillarHeatmap: React.FC<{
  items: SmartStockItem[];
  pillars: SmartPillarMeta[];
  onPick?: (symbol: string) => void;
}> = ({ items, pillars, onPick }) => {
  const top = useMemo(() => items.slice(0, TOP_N), [items]);

  const option = useMemo(() => {
    if (!top.length || !pillars.length) return null;
    const data: Array<{
      value: [number, number, number];
      symbol: string;
      name: string;
      pillar: string;
      missing: boolean;
      itemStyle?: { color: string };
    }> = [];
    top.forEach((it, y) => {
      pillars.forEach((p, x) => {
        const raw = it.pct?.[p.name];
        const missing = raw === null || raw === undefined;
        data.push({
          value: [x, y, missing ? 0 : Number(raw)],
          symbol: it.symbol,
          name: it.name ?? it.symbol,
          pillar: p.label,
          missing,
          ...(missing ? { itemStyle: { color: '#f8fafc' } } : {}),
        });
      });
    });
    return {
      tooltip: {
        textStyle: { fontSize: 10 },
        formatter: (p: { data?: (typeof data)[number] }) => {
          const d = p.data;
          if (!d) return '';
          return [
            `${d.name}`,
            `${d.pillar}：${d.missing ? `${MISSING}（该柱当日无值）` : `分位 ${Math.round(d.value[2])}`}`,
          ].join('<br/>');
        },
      },
      grid: { left: 98, right: 8, top: 26, bottom: 26 },
      xAxis: {
        type: 'category',
        data: pillars.map((p) => pillarShort(p.name)),
        axisLabel: { fontSize: 9, color: CHART_COLORS.neutral },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: CHART_COLORS.axis } },
        splitArea: { show: false },
      },
      yAxis: {
        type: 'category',
        data: top.map((it) => it.name ?? it.symbol),
        inverse: true,
        axisLabel: { fontSize: 9, color: CHART_COLORS.neutral, width: 90, overflow: 'truncate' },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: CHART_COLORS.axis } },
        splitArea: { show: false },
      },
      visualMap: {
        min: 0,
        max: 100,
        show: false,
        inRange: { color: ['#f1f5f9', '#ddd6fe', '#7c3aed'] },
      },
      series: [
        {
          type: 'heatmap',
          data,
          label: {
            show: true,
            fontSize: 8,
            color: '#334155',
            formatter: (p: { data?: (typeof data)[number] }) =>
              p.data?.missing ? MISSING : String(Math.round(p.data?.value[2] ?? 0)),
          },
          itemStyle: { borderColor: '#ffffff', borderWidth: 1 },
          emphasis: { itemStyle: { borderColor: '#7c3aed', borderWidth: 1 } },
        },
      ],
    };
  }, [top, pillars]);

  return (
    <div className="min-w-0">
      <div className="text-[10px] font-bold text-slate-500 px-1 pb-1">
        六柱分位热力图 <span className="font-normal text-slate-400">0–100 · 灰=缺失</span>
      </div>
      <SmartChart option={option} onEvents={clickOf(onPick)} height={Math.max(200, top.length * 18 + 50)} />
    </div>
  );
};

/** 综合分 × 机构净买散点：气泡=覆盖柱数，颜色=最近事件族 */
export const StockScoreScatter: React.FC<{
  items: SmartStockItem[];
  onPick?: (symbol: string) => void;
}> = ({ items, onPick }) => {
  const points = useMemo(
    () =>
      items
        .filter(
          (it) =>
            it.score !== null &&
            it.score !== undefined &&
            it.inst_net_5d !== null &&
            it.inst_net_5d !== undefined,
        )
        .map((it) => ({
          value: [Number(it.score), Number(it.inst_net_5d) / 1e8, Number(it.covered ?? 0)],
          symbol: it.symbol,
          name: it.name ?? it.symbol,
          event: it.latest_event?.type ?? '',
          industry: it.industry ?? MISSING,
        })),
    [items],
  );

  const option = useMemo(() => {
    if (!points.length) return null;
    return {
      tooltip: {
        textStyle: { fontSize: 10 },
        formatter: (p: { data?: (typeof points)[number] }) => {
          const d = p.data;
          if (!d) return '';
          return [
            `${d.name} <span style="color:#94a3b8">${d.symbol}</span>`,
            `综合分 ${fmtScore(d.value[0])}`,
            `机构净买·5日 ${d.value[1].toFixed(2)} 亿`,
            `覆盖 ${d.value[2]} 柱 · ${d.industry}`,
            d.event ? `最近事件 ${eventTypeLabel(d.event)}` : '',
          ]
            .filter(Boolean)
            .join('<br/>');
        },
      },
      grid: { left: 44, right: 18, top: 18, bottom: 28 },
      xAxis: {
        type: 'value',
        name: '综合分',
        nameTextStyle: { fontSize: 9, color: CHART_COLORS.sub },
        min: 0,
        max: 100,
        axisLabel: { ...AXIS_LABEL },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      yAxis: {
        type: 'value',
        name: '机构净买·5日（亿）',
        nameTextStyle: { fontSize: 9, color: CHART_COLORS.sub },
        axisLabel: { ...AXIS_LABEL },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      series: [
        {
          type: 'scatter',
          data: points.map((p) => ({
            ...p,
            itemStyle: { color: EVENT_COLORS[p.event] ?? CHART_COLORS.score, opacity: 0.82 },
          })),
          symbolSize: (v: [number, number, number]) => 7 + Math.min(10, Math.max(0, v[2]) * 1.8),
          emphasis: { scale: 1.35 },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: CHART_COLORS.axis, type: 'dashed' },
            label: { fontSize: 8, color: CHART_COLORS.sub },
            data: [{ xAxis: 60, label: { formatter: '分60' } }, { yAxis: 0 }],
          },
        },
      ],
    };
  }, [points]);

  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2 px-1 pb-1 text-[10px] font-bold text-slate-500">
        综合分 × 机构净买
        <span className="font-normal text-slate-400">气泡=覆盖柱数 · 颜色=事件族 · 右上=高分且真买</span>
        <span className="ml-auto flex items-center gap-1.5 font-normal text-[9px] text-slate-400">
          {Object.entries(EVENT_COLORS).map(([k, c]) => (
            <span key={k} className="inline-flex items-center gap-0.5">
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: c }} />
              {eventTypeLabel(k)}
            </span>
          ))}
        </span>
      </div>
      <SmartChart option={option} onEvents={clickOf(onPick)} height={240} />
    </div>
  );
};
