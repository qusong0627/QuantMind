/** IC 衰减曲线（紧凑款）：T+1/2/5/10/20 的 IC，用来判断该多久调仓。 */

import React from 'react';
import { EChartsChart } from '../../../../components/common/EChartsChart';

interface Props {
  icByHorizon?: Record<string, number | null>;
}

const ORDER = ['fwd_ret_1', 'fwd_ret_2', 'fwd_ret_5', 'fwd_ret_10', 'fwd_ret_20'];

export const HorizonDecayChart: React.FC<Props> = ({ icByHorizon }) => {
  const labels: string[] = [];
  const values: (number | null)[] = [];
  for (const h of ORDER) {
    const v = icByHorizon?.[h];
    if (v === undefined) continue;
    labels.push(h.replace('fwd_ret_', 'T+'));
    values.push(v === null ? null : +v.toFixed(4));
  }
  if (labels.length < 2) {
    return (
      <div className="bg-white rounded-xl border border-slate-200/80 px-3 py-2 min-w-[150px] flex items-center justify-center">
        <span className="text-[10px] text-slate-300">无衰减数据（旧快照）</span>
      </div>
    );
  }
  const peakIdx = values.reduce((best, v, i) => (v !== null && (best < 0 || Math.abs(v) > Math.abs(values[best] as number)) ? i : best), -1);
  const option: any = {
    grid: { left: 6, right: 8, top: 14, bottom: 4 },
    tooltip: { trigger: 'axis', valueFormatter: (v: number) => (v === null ? '—' : v.toFixed(4)) },
    xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 8, color: '#94a3b8' }, axisTick: { show: false } },
    yAxis: { type: 'value', show: false },
    series: [
      {
        type: 'line',
        data: values,
        showSymbol: true,
        symbolSize: 4,
        lineStyle: { width: 1.6, color: '#6366f1' },
        itemStyle: {
          color: (p: any) => (p.dataIndex === peakIdx ? '#e11d48' : '#6366f1'),
        },
        areaStyle: { color: 'rgba(99,102,241,0.10)' },
      },
    ],
  };
  return (
    <div className="bg-white rounded-xl border border-slate-200/80 px-2 py-1.5 min-w-[168px]" title="各前瞻期的 IC；红点=峰值（该周期信号最强）">
      <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wide">
        IC 衰减 · 峰值 {peakIdx >= 0 ? labels[peakIdx] : '—'}
      </div>
      <div className="h-[42px]">
        <EChartsChart option={option} />
      </div>
    </div>
  );
};
