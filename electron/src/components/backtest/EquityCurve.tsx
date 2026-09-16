/**
 * 权益曲线组件（T-FE-09 重写：lightweight-charts v5 API）
 *
 * 修复记录：原实现按 v4 API（`chart.addLineSeries`）编写，库升级到 v5.1 后该方法已移除
 * （`as any` 强转吞掉了报错）——曲线长期静默空白。本版按 v5 `addSeries(LineSeries/AreaSeries)`
 * + pane 机制重写，并补齐：**回撤水下图**（第二 pane）+ **区间统计**（全区间口径）。
 */

import React, { useEffect, useMemo, useRef } from 'react';
import {
  AreaSeries,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts';
import type { EquityCurve } from '../../types/backtest';
import { TermTooltip } from '../../features/shared/TermTooltip';
import { computeDrawdownSeries, computeRangeStats, formatPercent } from './equityStats';

interface EquityCurveProps {
  equityCurve: EquityCurve;
  initialCapital: number;
  benchmarkData?: { timestamp: number; value: number }[];
}

function StatChip({
  label,
  value,
  tone = 'neutral',
  term,
}: {
  label: string;
  value: string;
  tone?: 'up' | 'down' | 'neutral';
  term?: string;
}) {
  const toneClass =
    tone === 'up' ? 'text-red-600' : tone === 'down' ? 'text-emerald-600' : 'text-slate-800';
  return (
    <div className="rounded-xl border border-gray-100 bg-white px-3 py-2 min-w-[104px]">
      <div className="text-[11px] text-slate-400">
        {term ? <TermTooltip term={term}>{label}</TermTooltip> : label}
      </div>
      <div className={`text-sm font-bold ${toneClass}`}>{value}</div>
    </div>
  );
}

export const EquityCurveChart: React.FC<EquityCurveProps> = ({
  equityCurve,
  initialCapital,
  benchmarkData,
}) => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const equitySeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const benchmarkSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const drawdownSeriesRef = useRef<ISeriesApi<'Area'> | null>(null);

  const stats = useMemo(
    () => computeRangeStats(equityCurve.values, benchmarkData?.map((d) => d.value)),
    [equityCurve.values, benchmarkData]
  );

  useEffect(() => {
    if (!chartContainerRef.current) return;
    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth,
      height: 400,
      layout: {
        background: { color: '#ffffff' },
        textColor: '#64748b',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: '#f1f5f9' },
        horzLines: { color: '#f1f5f9' },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: '#e2e8f0' },
      timeScale: { borderColor: '#e2e8f0', timeVisible: true, secondsVisible: false },
    });
    chartRef.current = chart;

    equitySeriesRef.current = chart.addSeries(LineSeries, {
      color: '#2563eb',
      lineWidth: 2,
      title: '策略权益',
      priceLineVisible: false,
    });

    if (benchmarkData) {
      benchmarkSeriesRef.current = chart.addSeries(LineSeries, {
        color: '#94a3b8',
        lineWidth: 1,
        lineStyle: 2, // 虚线
        title: '基准',
        priceLineVisible: false,
      });
    }

    // 回撤水下图：第二 pane（负值区域，红）
    drawdownSeriesRef.current = chart.addSeries(
      AreaSeries,
      {
        lineColor: '#fca5a5',
        topColor: 'rgba(248,113,113,0.05)',
        bottomColor: 'rgba(248,113,113,0.35)',
        lineWidth: 1,
        priceFormat: { type: 'custom', formatter: (p: number) => `${(p * 100).toFixed(1)}%` },
        priceLineVisible: false,
        title: '回撤',
      },
      1
    );
    try {
      const panes = chart.panes();
      panes[1]?.setHeight(110);
    } catch {
      // pane 调整属外观优化，失败不影响主图
    }

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({ width: chartContainerRef.current.clientWidth });
      }
    };
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      chart.remove();
      chartRef.current = null;
      equitySeriesRef.current = null;
      benchmarkSeriesRef.current = null;
      drawdownSeriesRef.current = null;
    };
    // 基准存在与否决定 series 结构 → 依赖重建
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Boolean(benchmarkData)]);

  useEffect(() => {
    if (!equitySeriesRef.current) return;
    const eqData = equityCurve.timestamps
      .map((ts, i) => ({
        time: Math.floor(ts / 1000) as UTCTimestamp,
        value: equityCurve.values[i],
      }))
      .filter((d) => Number.isFinite(d.value));
    equitySeriesRef.current.setData(eqData);

    if (benchmarkSeriesRef.current && benchmarkData) {
      benchmarkSeriesRef.current.setData(
        benchmarkData
          .map((d) => ({ time: Math.floor(d.timestamp / 1000) as UTCTimestamp, value: d.value }))
          .filter((d) => Number.isFinite(d.value))
      );
    }

    if (drawdownSeriesRef.current) {
      const dd = computeDrawdownSeries(equityCurve.values);
      drawdownSeriesRef.current.setData(
        equityCurve.timestamps
          .map((ts, i) => ({ time: Math.floor(ts / 1000) as UTCTimestamp, value: dd[i] }))
          .filter((d) => Number.isFinite(d.value))
      );
    }

    chartRef.current?.timeScale().fitContent();
  }, [equityCurve, benchmarkData]);

  const finalEquity = equityCurve.values[equityCurve.values.length - 1];

  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <h3 className="text-sm font-semibold text-slate-800">资金曲线</h3>
        <div className="flex flex-wrap gap-2">
          <StatChip label="初始资金" value={initialCapital.toLocaleString()} />
          <StatChip label="最终权益" value={Number(finalEquity || 0).toLocaleString()} />
          <StatChip
            label="区间收益"
            value={formatPercent(stats.totalReturn)}
            tone={(stats.totalReturn ?? 0) >= 0 ? 'up' : 'down'}
          />
          <StatChip label="年化" value={formatPercent(stats.annualized)} tone={(stats.annualized ?? 0) >= 0 ? 'up' : 'down'} />
          <StatChip label="最大回撤" value={formatPercent(stats.maxDrawdown)} tone="down" term="max_drawdown" />
          <StatChip label="波动率" value={formatPercent(stats.volatility)} />
          <StatChip label="夏普" value={stats.sharpe === null ? '—' : stats.sharpe.toFixed(2)} term="sharpe" />
          {stats.benchmarkReturn !== null && (
            <StatChip
              label="超额（vs 基准）"
              value={formatPercent(stats.excess)}
              tone={(stats.excess ?? 0) >= 0 ? 'up' : 'down'}
            />
          )}
        </div>
      </div>

      <div ref={chartContainerRef} className="w-full" />

      <p className="text-[10px] text-slate-400 mt-2">
        上：策略权益 vs 基准（虚线）；下：回撤水下曲线。统计为全区间口径（夏普按无风险利率 = 0），
        样本 &lt; 2 点或数据缺失如实显示「—」，不画假线。
      </p>
    </div>
  );
};
