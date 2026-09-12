/** 全市场涨跌幅分布 —— 直方图 + 分位数
 *
 * 分布形状比单一均值更能说明市场状态：
 * 双峰（大涨大跌都多）= 分化行情；集中在 0 附近 = 窄幅震荡；整体左移 = 普跌。
 * 用 CSS 条而非 echarts：10 个分桶用图表库属于杀鸡用牛刀，还占地方。
 */

import React, { useEffect, useState } from 'react';
import { BarChart3 } from 'lucide-react';
import { getMarketDistribution } from '../services/api';
import type { UsMarketDistribution } from '../types';
import { SectionCard, EmptyHint, DateBadge } from '../../market-analysis-shared/ui';

export const UsMarketDistributionPanel: React.FC<{ className?: string }> = ({ className = '' }) => {
  const [data, setData] = useState<UsMarketDistribution | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    getMarketDistribution()
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

  const buckets = Array.isArray(data?.buckets) ? data.buckets : [];
  const max = Math.max(1, ...buckets.map((b) => b.count));
  const q = data?.quantiles ?? {};

  return (
    <SectionCard
      className={`!p-2.5 ${className}`}
      title={
        <span className="flex items-center gap-1.5">
          <BarChart3 className="w-3.5 h-3.5 text-blue-600" />
          涨跌幅分布
          <span className="text-[9px] font-normal text-slate-400">
            {data?.total ? `全池 ${data.total} 只` : ''}
          </span>
        </span>
      }
      extra={<DateBadge date={data?.trade_date} />}
    >
      {buckets.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div className="flex flex-col gap-1.5 flex-1 min-h-0">
          {/* 横向柱状：左绿（跌）右红（涨）。容器 flex-1 + 柱高用百分比，
              使卡片被拉高到与同排左卡片等高时柱子随之长高（而非留白）。 */}
          <div className="flex items-end gap-[3px] flex-1 min-h-[72px]">
            {buckets.map((b, i) => {
              const down = i < 5;
              const pct = Math.max(3, (b.count / max) * 100);
              return (
                <div
                  key={b.label}
                  className="flex-1 h-full flex flex-col items-center justify-end gap-0.5 group"
                  title={`${b.label}: ${b.count} 只`}
                >
                  <span className="text-[8px] font-mono text-slate-400 opacity-0 group-hover:opacity-100">
                    {b.count}
                  </span>
                  <span
                    className={`w-full rounded-t-sm ${down ? 'bg-green-400' : 'bg-red-400'} group-hover:opacity-80`}
                    style={{ height: `${pct}%` }}
                  />
                </div>
              );
            })}
          </div>
          <div className="flex gap-[3px] -mt-1">
            {buckets.map((b) => (
              <span
                key={b.label}
                className="flex-1 text-center text-[8px] font-mono text-slate-400 leading-tight scale-90"
              >
                {b.label.replace('%', '')}
              </span>
            ))}
          </div>
          {/* 分位数条 */}
          <div className="grid grid-cols-5 gap-1 pt-0.5 border-t border-slate-100">
            {(
              [
                ['p10', q.p10],
                ['p25', q.p25],
                ['中位', q.median],
                ['p75', q.p75],
                ['p90', q.p90],
              ] as const
            ).map(([label, v]) => (
              <span key={label} className="flex flex-col items-center">
                <span className="text-[9px] font-bold text-slate-400">{label}</span>
                <span
                  className={`text-[11px] font-extrabold font-mono ${
                    (v ?? 0) > 0 ? 'text-red-600' : (v ?? 0) < 0 ? 'text-green-600' : 'text-slate-500'
                  }`}
                >
                  {v === undefined || v === null ? '--' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}
    </SectionCard>
  );
};
