/**
 * 净值曲线（策略编辑结果坞专用）。
 *
 * 与 Strategy Lab 的 EquityChart 同数据口径，但重画了视觉：
 * - A 股口径：策略主线蓝、基准灰虚线、回撤绿色面积（亏损=绿）；
 * - 只保留横向网格，去掉默认图例与坐标轴线，贴合结果坞的低高度；
 * - 自适应父容器高度（100%），展开/最大化时不用改调用方。
 */
import React, { useMemo } from 'react';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { StrategyLabEquityPoint } from '../../../features/strategy-lab/types';

interface ChartRow {
  date: string;
  strategy: number;
  benchmark: number | null;
  drawdown: number;
}

const buildData = (equity: StrategyLabEquityPoint[]): ChartRow[] => {
  if (!equity.length) return [];
  const base = equity[0]?.value || 1;
  const benchBase = equity[0]?.benchmark ?? null;
  let peak = -Infinity;
  return equity.map((point) => {
    const norm = point.value / base;
    if (norm > peak) peak = norm;
    return {
      date: point.date,
      strategy: norm,
      benchmark: benchBase && point.benchmark ? point.benchmark / benchBase : null,
      drawdown: peak > 0 ? norm / peak - 1 : 0,
    };
  });
};

interface TooltipCardProps {
  active?: boolean;
  payload?: Array<{ payload?: ChartRow }>;
  label?: string;
}

const TooltipCard: React.FC<TooltipCardProps> = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  const row: ChartRow | undefined = payload[0]?.payload;
  if (!row) return null;
  const shape = (v: number | null, digits = 3) =>
    v === null || v === undefined ? '—' : v.toFixed(digits);
  return (
    <div className="rounded-lg border border-slate-200 bg-white/95 px-2.5 py-2 shadow-lg backdrop-blur">
      <div className="mb-1 font-mono text-[10px] text-slate-400">{String(label || '').slice(0, 10)}</div>
      <div className="space-y-0.5 text-[10.5px]">
        <div className="flex items-center justify-between gap-4">
          <span className="text-slate-500">策略净值</span>
          <span className="font-mono font-semibold text-blue-600">{shape(row.strategy, 4)}</span>
        </div>
        {row.benchmark !== null && (
          <div className="flex items-center justify-between gap-4">
            <span className="text-slate-500">基准净值</span>
            <span className="font-mono font-semibold text-slate-600">{shape(row.benchmark, 4)}</span>
          </div>
        )}
        <div className="flex items-center justify-between gap-4">
          <span className="text-slate-500">回撤</span>
          <span className="font-mono font-semibold text-emerald-600">
            {(row.drawdown * 100).toFixed(2)}%
          </span>
        </div>
      </div>
    </div>
  );
};

export const ResultEquityChart: React.FC<{ equity: StrategyLabEquityPoint[] }> = ({ equity }) => {
  const data = useMemo(() => buildData(equity), [equity]);
  if (!data.length) return null;
  const hasBenchmark = data.some((row) => row.benchmark !== null);

  return (
    <div className="h-full w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 12, right: 10, left: 2, bottom: 2 }}>
          <defs>
            <linearGradient id="se-equity-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#2563eb" stopOpacity={0.2} />
              <stop offset="100%" stopColor="#2563eb" stopOpacity={0.01} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#eef2f8" vertical={false} />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 9, fill: '#94a3b8', fontFamily: 'JetBrains Mono' }}
            tickLine={false}
            axisLine={false}
            minTickGap={48}
            tickFormatter={(value: string) => value.slice(2, 7)}
          />
          <YAxis
            yAxisId="equity"
            orientation="left"
            tick={{ fontSize: 9, fill: '#94a3b8', fontFamily: 'JetBrains Mono' }}
            tickLine={false}
            axisLine={false}
            width={40}
            domain={['auto', 'auto']}
            tickFormatter={(value: number) => value.toFixed(2)}
          />
          <YAxis
            yAxisId="drawdown"
            orientation="right"
            tick={{ fontSize: 9, fill: '#a7b5c8' }}
            tickLine={false}
            axisLine={false}
            width={38}
            domain={[(dataMin: number) => Math.min(dataMin, -0.05), 0]}
            tickFormatter={(value: number) => `${(value * 100).toFixed(0)}%`}
          />
          <Tooltip content={<TooltipCard />} cursor={{ stroke: '#cbd5e1', strokeDasharray: '3 3' }} />
          <ReferenceLine yAxisId="equity" y={1} stroke="#e2e8f0" strokeDasharray="2 4" />
          <Area
            yAxisId="drawdown"
            type="monotone"
            dataKey="drawdown"
            stroke="none"
            fill="#10b981"
            fillOpacity={0.08}
            isAnimationActive={false}
          />
          <Area
            yAxisId="equity"
            type="monotone"
            dataKey="strategy"
            stroke="#2563eb"
            strokeWidth={1.7}
            fill="url(#se-equity-fill)"
            isAnimationActive={false}
          />
          {hasBenchmark && (
            <Line
              yAxisId="equity"
              type="monotone"
              dataKey="benchmark"
              stroke="#94a3b8"
              strokeWidth={1.2}
              strokeDasharray="4 4"
              dot={false}
              isAnimationActive={false}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
};

export default ResultEquityChart;
