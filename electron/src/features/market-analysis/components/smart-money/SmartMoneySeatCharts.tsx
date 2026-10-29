/** 聪明钱 · 席位图表层（v3：先看“谁又准又敢买”，明细可切回表格）。
 *
 * 散点：x=窗口净买（亿），y=胜率（%），气泡=样本数，颜色=席位类型；
 * 虚线=池内基线胜率。右上象限 = 净买大且胜率高，比逐行读表快。
 * 缺失值不入散点（不补 0），顶部前 5 卡仍展示名次与数字。
 */

import React, { useMemo } from 'react';
import type { SmartSeatRow } from '../../services/smartMoneyService';
import { MISSING, fmtBp, fmtPct, fmtSigned, seatClassLabel } from '../../smartMoneyVocabulary';
import { AXIS_LABEL, CHART_COLORS, SmartChart } from './charts';

const CLASS_COLORS: Record<string, string> = {
  inst: '#7c3aed',
  north: '#2563eb',
  branch: '#64748b',
  other: '#94a3b8',
};

const TOP_STRIP = 5;

const winOf = (row: SmartSeatRow, window: string): number | null => {
  const w = row.windows?.[window];
  return w?.win_rate === null || w?.win_rate === undefined ? null : Number(w.win_rate);
};

/** 前 5 席位卡：名次 + 净买 + 胜率，先把“谁”亮出来 */
export const TopSeatStrip: React.FC<{
  items: SmartSeatRow[];
  window: string;
}> = ({ items, window }) => {
  const top = items.slice(0, TOP_STRIP);
  if (!top.length) return null;
  return (
    <div className="grid grid-cols-2 xl:grid-cols-5 gap-1.5">
      {top.map((row, i) => {
        const w = row.windows?.[window];
        return (
          <div key={`${row.class}-${row.seat_name}`} className="min-w-0 rounded-xl border border-slate-200 bg-white px-2 py-1.5">
            <div className="flex items-center gap-1 min-w-0">
              <span
                className={`w-4 h-4 rounded grid place-items-center text-[9px] font-extrabold ${
                  i < 3 ? 'bg-purple-600 text-white' : 'bg-slate-100 text-slate-500'
                }`}
              >
                {i + 1}
              </span>
              <span className="text-[10px] font-bold text-slate-700 truncate" title={row.seat_name}>
                {row.seat_name}
              </span>
            </div>
            <div className="mt-0.5 flex items-center gap-1 text-[9px] text-slate-400">
              <span
                className="rounded px-1 border"
                style={{
                  color: CLASS_COLORS[row.class] ?? CLASS_COLORS.other,
                  borderColor: `${CLASS_COLORS[row.class] ?? CLASS_COLORS.other}33`,
                  background: `${CLASS_COLORS[row.class] ?? CLASS_COLORS.other}0d`,
                }}
              >
                {seatClassLabel(row.class, row.class_label)}
              </span>
              <span className="ml-auto font-mono text-slate-600">胜率 {fmtPct(w?.win_rate, 0)}</span>
            </div>
            <div className="mt-1 flex items-baseline justify-between gap-1">
              <span className={`text-[11px] font-mono font-extrabold ${Number(row.total_net_yi ?? 0) >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                {fmtSigned(row.total_net_yi, 1)} 亿
              </span>
              <span className="text-[9px] font-mono text-slate-400">n={w?.n ?? MISSING}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
};

/** 净买 × 胜率散点：右上=又准又敢买；虚线=池内基线 */
export const SeatScatter: React.FC<{
  items: SmartSeatRow[];
  window: string;
  baseline: number | null;
}> = ({ items, window, baseline }) => {
  const points = useMemo(
    () =>
      items
        .map((row) => {
          const w = row.windows?.[window];
          const win = winOf(row, window);
          if (win === null || row.total_net_yi === null || row.total_net_yi === undefined) return null;
          return {
            value: [Number(row.total_net_yi), win * 100, Number(w?.n ?? 0)] as [number, number, number],
            name: row.seat_name,
            cls: row.class,
            clsLabel: seatClassLabel(row.class, row.class_label),
            excess: w?.avg_excess_bp ?? null,
            n: w?.n ?? null,
            last: row.last_seen,
          };
        })
        .filter((p): p is NonNullable<typeof p> => p !== null),
    [items, window],
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
            `${d.name}`,
            `${d.clsLabel} · n=${d.n ?? MISSING}`,
            `窗口净买 ${d.value[0].toFixed(2)} 亿`,
            `胜率 ${d.value[1].toFixed(1)}% · 平均超额 ${fmtBp(d.excess)}`,
            d.last ? `最近 ${d.last}` : '',
          ]
            .filter(Boolean)
            .join('<br/>');
        },
      },
      grid: { left: 44, right: 18, top: 18, bottom: 28 },
      xAxis: {
        type: 'value',
        name: '窗口净买（亿）',
        nameTextStyle: { fontSize: 9, color: CHART_COLORS.sub },
        axisLabel: { ...AXIS_LABEL },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      yAxis: {
        type: 'value',
        name: '胜率（%）',
        nameTextStyle: { fontSize: 9, color: CHART_COLORS.sub },
        min: 0,
        max: 100,
        axisLabel: { ...AXIS_LABEL, formatter: (v: number) => `${v}%` },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      series: [
        {
          type: 'scatter',
          data: points.map((p) => ({
            ...p,
            itemStyle: { color: CLASS_COLORS[p.cls] ?? CLASS_COLORS.other, opacity: 0.82 },
          })),
          symbolSize: (v: [number, number, number]) => 7 + Math.min(12, Math.max(0, v[2]) * 0.5),
          emphasis: { scale: 1.35 },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: CHART_COLORS.axis, type: 'dashed' },
            label: { fontSize: 8, color: CHART_COLORS.sub },
            data: [
              { xAxis: 0 },
              ...(baseline === null
                ? []
                : [{ yAxis: baseline * 100, label: { formatter: `基线 ${(baseline * 100).toFixed(0)}%` } }]),
            ],
          },
        },
      ],
    };
  }, [points, baseline]);

  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2 px-1 pb-1 text-[10px] font-bold text-slate-500">
        净买 × 胜率
        <span className="font-normal text-slate-400">气泡=样本数 · 虚线=池内基线 · 右上=又准又敢买</span>
        <span className="ml-auto flex items-center gap-1.5 font-normal text-[9px] text-slate-400">
          {Object.entries(CLASS_COLORS).map(([k, c]) => (
            <span key={k} className="inline-flex items-center gap-0.5">
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: c }} />
              {seatClassLabel(k)}
            </span>
          ))}
        </span>
      </div>
      <SmartChart option={option} height={300} />
    </div>
  );
};
