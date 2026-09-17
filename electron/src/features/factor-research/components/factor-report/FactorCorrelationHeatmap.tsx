/** 因子相关性：热力图（当前因子 + 高相关因子）+ 相关因子清单 */

import React from 'react';
import { EChartsChart } from '../../../../components/common/EChartsChart';
import type { FactorCorrelation, FactorRelated } from '../../types/factorReport';

interface Props {
  correlation: FactorCorrelation | null;
  related: FactorRelated | null;
  loading: boolean;
  onPick: (name: string) => void;
}

/** 相关系数配色：正相关偏红、负相关偏绿（与站内涨跌色一致） */
function corrColor(v: number): string {
  const a = Math.min(Math.abs(v), 1);
  return v >= 0
    ? `rgba(225, 29, 72, ${0.08 + a * 0.7})`
    : `rgba(5, 150, 105, ${0.08 + a * 0.7})`;
}

export const FactorCorrelationHeatmap: React.FC<Props> = ({ correlation, related, loading, onPick }) => {
  const names = correlation?.factors || [];
  const matrix = correlation?.matrix || [];
  const hasData = correlation?.available && names.length >= 2;

  const heatOption = hasData
    ? {
        grid: { left: 78, right: 16, top: 12, bottom: 60 },
        tooltip: {
          formatter: (p: any) => `${names[p.data[1]]} × ${names[p.data[0]]}<br/>相关 ${p.data[2]}`,
        },
        xAxis: {
          type: 'category',
          data: names,
          axisLabel: { fontSize: 9, color: '#94a3b8', rotate: 45 },
          axisTick: { show: false },
        },
        yAxis: { type: 'category', data: names, axisLabel: { fontSize: 9, color: '#94a3b8' }, axisTick: { show: false } },
        visualMap: {
          show: false,
          min: -1,
          max: 1,
          inRange: { color: ['#047857', '#e2e8f0', '#be123c'] },
        },
        series: [
          {
            type: 'heatmap',
            data: matrix.flatMap((row, i) => row.map((v, j) => [i, j, v])),
            label: {
              show: names.length <= 8,
              fontSize: 9,
              color: '#0f172a',
              formatter: (p: any) => (p.data[2] === 1 ? '' : String(p.data[2])),
            },
            itemStyle: {
              borderColor: '#fff',
              borderWidth: 1,
            },
          },
        ],
      }
    : null;

  return (
    <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-3 flex flex-col min-h-0">
      <div className="flex items-baseline justify-between mb-1">
        <h4 className="text-xs font-extrabold text-slate-800">因子相关性</h4>
        <span className="text-[10px] text-slate-400">秩相关 · 近 5 年日均（红=正相关 绿=负相关）</span>
      </div>

      <div className="flex-1 min-h-0 flex gap-3">
        <div className="flex-1 min-w-0">
          {loading && !hasData ? (
            <div className="h-full rounded-xl bg-slate-50 animate-pulse" />
          ) : hasData && heatOption ? (
            <EChartsChart option={heatOption} />
          ) : (
            <div className="h-full flex items-center justify-center text-xs text-slate-400">
              {correlation?.reason || '暂无相关性数据'}
            </div>
          )}
        </div>

        {/* 高相关因子清单：一眼看出这只因子是不是别人的复制品 */}
        <div className="w-[190px] shrink-0 flex flex-col min-h-0">
          <span className="text-[10px] font-bold text-slate-400 mb-1">高相关因子（|ρ| 降序）</span>
          <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar space-y-0.5 pr-1">
            {(related?.related || []).map((r) => (
              <button
                key={r.name}
                onClick={() => onPick(r.name)}
                className="w-full flex items-center justify-between px-2 py-1 rounded-lg hover:bg-slate-50 transition-colors"
                title="点击切换查看该因子"
              >
                <span className="text-[11px] font-bold text-slate-600 truncate">{r.name}</span>
                <span
                  className="ml-2 shrink-0 rounded px-1.5 py-[1px] text-[10px] font-mono font-bold text-white"
                  style={{ backgroundColor: corrColor(r.corr) }}
                >
                  {r.corr > 0 ? '+' : ''}{r.corr.toFixed(2)}
                </span>
              </button>
            ))}
            {!loading && (related?.related || []).length === 0 && (
              <span className="text-[11px] text-slate-300 px-2">暂无数据</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
