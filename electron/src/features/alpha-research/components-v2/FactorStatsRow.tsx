/**
 * FactorStatsRow — 结果区统计瓦片 + 一键回测入口。
 *
 * 「一键回测」是真按钮：把全部可回测因子（有表达式、非只读）交给宿主页入队
 * （旧实现只导航不动 API，点了没有任何回测发生）。
 */

import React, { useMemo } from 'react';
import { Layers, TrendingUp, BarChart3 } from 'lucide-react';
import { RealtimeMetrics } from '../types-v2';

interface FactorStatsRowProps {
  metrics: RealtimeMetrics | null;
  /** 一键回测：对全部可回测因子入队（并发 2，行内状态在结果表里看） */
  onQuickBacktest?: (factorIds: string[]) => void;
}

export const FactorStatsRow: React.FC<FactorStatsRowProps> = ({ metrics, onQuickBacktest }) => {
  const qualityData = useMemo(() => {
    if (!metrics) return [];
    return [
      { name: '高质量', value: metrics.highQualityFactors || 0, fill: '#10B981' },
      { name: '中等', value: metrics.mediumQualityFactors || 0, fill: '#F59E0B' },
      { name: '低质量', value: metrics.lowQualityFactors || 0, fill: '#EF4444' },
    ];
  }, [metrics]);

  // 可回测 = 有表达式（无代码后端 400）且非只读（ownerless/工厂因子不可操作）
  const backtestableIds = useMemo(() => {
    const factors = metrics?.factors ?? [];
    return factors
      .filter((f) => !f.ownerless && !f.readOnly && f.factorExpression)
      .map((f) => f.factorId);
  }, [metrics]);

  const StatCard = ({ icon: Icon, label, value, color, className }: any) => (
    <div
      className={`glass rounded-xl p-3 card-hover h-[96px] flex flex-col items-center justify-center text-center gap-0.5 ${className}`}
    >
      <div className={`p-1.5 rounded-lg ${color} bg-opacity-20`}>
        <Icon className={`h-4 w-4 ${color}`} />
      </div>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="text-lg font-bold leading-tight">{value}</div>
    </div>
  );

  const canQuickBacktest = backtestableIds.length > 0 && Boolean(onQuickBacktest);

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4 animate-fade-in-up w-full">
      <StatCard
        icon={Layers}
        label="因子总数量"
        value={metrics ? metrics.totalFactors : 0}
        color="text-primary"
        className="shadow-lg border-primary/10"
      />

      {qualityData.map((item) => (
        <StatCard
          key={item.name}
          icon={TrendingUp}
          label={`${item.name}因子数量`}
          value={item.value}
          color={
            item.name === '高质量'
              ? 'text-success'
              : item.name === '中等'
                ? 'text-warning'
                : 'text-destructive'
          }
          className="shadow-lg border-primary/10"
        />
      ))}

      {/* 一键回测：真入队（并发 2），行内回测状态在下方结果表逐行可见 */}
      <button
        type="button"
        onClick={() => onQuickBacktest?.(backtestableIds)}
        disabled={!canQuickBacktest}
        title={
          canQuickBacktest
            ? `对 ${backtestableIds.length} 个可回测因子逐个发起回测（并发 2）`
            : '暂无可回测因子（需要已产出因子且带表达式）'
        }
        className="glass card-hover flex h-[96px] flex-col items-center justify-center gap-0.5 rounded-xl border border-primary/10 shadow-lg transition-all duration-300 hover:border-primary/30 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-primary/10"
      >
        <div className="rounded-full bg-primary p-2 text-primary-foreground shadow-md transition-all duration-300 group-hover:bg-primary/90">
          <BarChart3 className="h-4 w-4" />
        </div>
        <div className="text-xs font-bold text-foreground/80">一键回测（{backtestableIds.length}）</div>
        <div className="text-[10px] text-muted-foreground">逐个入队 · 并发 2</div>
      </button>
    </div>
  );
};

export default FactorStatsRow;
