/** 单因子明细图表：分位平均收益 / 分位净值 / IC 时序 / 换手序列 */

import React from 'react';
import { EChartsChart } from '../../../../components/common/EChartsChart';
import type { FactorDetail } from '../../types/factorReport';

interface Props {
  detail: FactorDetail | null;
  loading: boolean;
}

const fmtDate = (d: string) => (d && d.length === 8 ? `${d.slice(4, 6)}-${d.slice(6, 8)}` : d);

/** 分位色阶：Q1 绿（因子值最小）→ Q10 红，形成可读的谱系 */
function quantileColor(i: number, total: number): string {
  const t = total > 1 ? i / (total - 1) : 0;
  const hue = 152 - 152 * t; // 绿(152) → 红(0)
  return `hsl(${hue}, 62%, ${t > 0.5 ? 48 : 42}%)`;
}

function ChartShell({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-3 flex flex-col min-h-0">
      <div className="flex items-baseline justify-between mb-1">
        <h4 className="text-xs font-extrabold text-slate-800">{title}</h4>
        {hint && <span className="text-[10px] text-slate-400">{hint}</span>}
      </div>
      <div className="flex-1 min-h-0">{children}</div>
    </div>
  );
}

export const FactorDetailCharts: React.FC<Props> = ({ detail, loading }) => {
  if (loading && !detail) {
    return (
      <div className="grid grid-cols-2 gap-3 flex-1 min-h-0">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="rounded-2xl border border-slate-200/80 bg-white animate-pulse" />
        ))}
      </div>
    );
  }
  if (!detail || detail.empty) {
    return (
      <div className="flex-1 min-h-0 flex items-center justify-center rounded-2xl border border-dashed border-slate-200 bg-white/60">
        <span className="text-xs text-slate-400">{detail?.reason || '请选择一个因子查看报告'}</span>
      </div>
    );
  }

  const qMean = detail.quantile_mean.map((v) => +(v * 100).toFixed(3)); // 转 %
  const qLabels = qMean.map((_, i) => `Q${i + 1}`);
  const lsPct = +((detail.quantile_mean[9] - detail.quantile_mean[0]) * 100).toFixed(3);

  const quantileBarOption: any = {
    grid: { left: 42, right: 12, top: 18, bottom: 24 },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: number) => `${v > 0 ? '+' : ''}${v}%`,
    },
    xAxis: { type: 'category', data: qLabels, axisLabel: { fontSize: 10, color: '#94a3b8' }, axisTick: { show: false } },
    yAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#94a3b8', formatter: '{value}%' },
      splitLine: { lineStyle: { color: '#f1f5f9' } },
    },
    series: [
      {
        type: 'bar',
        data: qMean.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? '#e11d48' : '#059669', borderRadius: 3 } })),
        barMaxWidth: 22,
        label: { show: true, position: 'top', fontSize: 9, color: '#64748b' },
      },
    ],
  };

  const dateLabels = detail.dates.map(fmtDate);
  const curveSeries: any[] = detail.quantile_curves.map((curve, i) => ({
    name: qLabels[i],
    type: 'line',
    data: curve.map((v) => +(v * 100 - 100).toFixed(2)),
    showSymbol: false,
    lineStyle: { width: i === 0 || i === 9 ? 2 : 1, color: quantileColor(i, 10) },
    itemStyle: { color: quantileColor(i, 10) },
  }));
  curveSeries.push({
    name: '多空(Q10-Q1)',
    type: 'line',
    data: detail.ls_curve.map((v) => +(v * 100 - 100).toFixed(2)),
    showSymbol: false,
    lineStyle: { width: 2, type: 'dashed', color: '#4f46e5' },
    itemStyle: { color: '#4f46e5' },
  });

  const curveOption: any = {
    grid: { left: 46, right: 14, top: 22, bottom: 26 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${v > 0 ? '+' : ''}${v}%` },
    legend: { show: false },
    xAxis: { type: 'category', data: dateLabels, axisLabel: { fontSize: 10, color: '#94a3b8', interval: Math.max(1, Math.floor(dateLabels.length / 6)) }, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#94a3b8', formatter: '{value}%' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: curveSeries,
  };

  const icOption: any = {
    grid: { left: 46, right: 14, top: 18, bottom: 26 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: dateLabels, axisLabel: { fontSize: 10, color: '#94a3b8', interval: Math.max(1, Math.floor(dateLabels.length / 6)) }, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#94a3b8' }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: [
      {
        name: '日 IC',
        type: 'bar',
        data: detail.ic_series.map((v) => (v === null ? null : +v.toFixed(3))),
        itemStyle: { color: 'rgba(99, 102, 241, 0.35)' },
        barMaxWidth: 4,
      },
      {
        name: '20 日均值',
        type: 'line',
        data: detail.ic_rolling.map((v) => (v === null ? null : +v.toFixed(3))),
        showSymbol: false,
        lineStyle: { width: 2, color: '#e11d48' },
        itemStyle: { color: '#e11d48' },
      },
    ],
  };

  const turnoverOption: any = {
    grid: { left: 46, right: 14, top: 18, bottom: 26 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => `${(v * 100).toFixed(0)}%` },
    xAxis: { type: 'category', data: detail.turnover_dates.map(fmtDate), axisLabel: { fontSize: 10, color: '#94a3b8', interval: Math.max(1, Math.floor(detail.turnover_dates.length / 6)) }, axisTick: { show: false } },
    yAxis: { type: 'value', max: 1, axisLabel: { fontSize: 10, color: '#94a3b8', formatter: (v: number) => `${(v * 100).toFixed(0)}%` }, splitLine: { lineStyle: { color: '#f1f5f9' } } },
    series: [
      {
        name: '单边换手',
        type: 'line',
        data: detail.turnover_series.map((v) => +v.toFixed(3)),
        showSymbol: false,
        areaStyle: { color: 'rgba(245, 158, 11, 0.18)' },
        lineStyle: { width: 1.6, color: '#f59e0b' },
        itemStyle: { color: '#f59e0b' },
      },
    ],
  };

  return (
    <div className="grid grid-cols-2 grid-rows-2 gap-3 flex-1 min-h-0">
      <ChartShell title="分位平均前瞻收益" hint={`Q10−Q1 = ${lsPct > 0 ? '+' : ''}${lsPct}%`}>
        <EChartsChart option={quantileBarOption} />
      </ChartShell>
      <ChartShell title="分位净值曲线" hint="按日折算 · 绿=因子值低 红=因子值高">
        <EChartsChart option={curveOption} />
      </ChartShell>
      <ChartShell title="IC 时序" hint={`IC ${detail.ic_mean?.toFixed(4) ?? '—'} / 20 日均值`}>
        <EChartsChart option={icOption} />
      </ChartShell>
      <ChartShell title="换手率" hint={`均值 ${detail.turnover_mean ? (detail.turnover_mean * 100).toFixed(0) : '—'}%`}>
        <EChartsChart option={turnoverOption} />
      </ChartShell>
    </div>
  );
};
