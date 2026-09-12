/** 板块资金流 —— 成交额占比相对 20 日基准的变化（百分点）
 *
 * 单看今日成交额只能看出谁体量大（信息技术永远最大），
 * 要看**占比相对自身近期基准的变化**才能识别资金迁移方向。
 */

import React, { useEffect, useState } from 'react';
import { Waves } from 'lucide-react';
import { getSectorFundFlow } from '../services/api';
import type { UsSectorFundFlow } from '../types';
import { SectionCard, EmptyHint, DateBadge, fmtInt } from '../../market-analysis-shared/ui';

export const UsSectorFundFlowPanel: React.FC<{ limit?: number; className?: string }> = ({
  limit = 14,
  className = '',
}) => {
  const [data, setData] = useState<UsSectorFundFlow | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    getSectorFundFlow(24)
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

  const sectors = (data?.sectors ?? []).slice(0, limit);
  // 以最大绝对变化定标，让条形宽度可比
  const maxAbs = Math.max(
    0.5,
    ...sectors.map((s) => Math.abs(s.share_change_pp ?? 0)),
  );

  return (
    <SectionCard
      className={`!p-2.5 ${className}`}
      title={
        <span className="flex items-center gap-1.5">
          <Waves className="w-3.5 h-3.5 text-blue-600" />
          板块资金流
          <span className="text-[9px] font-normal text-slate-400">
            成交额占比 vs 前{data?.base_days || 20}日基准
          </span>
        </span>
      }
      extra={<DateBadge date={data?.trade_date} />}
    >
      {sectors.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div className="flex flex-col flex-1 min-h-0">
          <div className="grid grid-cols-[64px_58px_46px_1fr_46px] gap-1.5 px-1 py-[3px] text-[9px] font-bold text-slate-400 border-b border-slate-200">
            <span>板块</span>
            <span className="text-right">成交额</span>
            <span className="text-right">占比</span>
            <span className="text-center">资金迁移（Δ百分点）</span>
            <span className="text-right">变化</span>
          </div>
          {sectors.map((s) => {
            const d = s.share_change_pp;
            const inflow = (d ?? 0) >= 0;
            const w = d === null ? 0 : (Math.abs(d) / maxAbs) * 50;
            return (
              <div
                key={s.sector}
                className="grid grid-cols-[64px_58px_46px_1fr_46px] gap-1.5 px-1 py-[3px] text-[10px] items-center border-b border-slate-50 last:border-0 hover:bg-slate-50/80 flex-1 min-h-[18px]"
              >
                <span className="font-bold text-slate-700 truncate" title={s.sector}>
                  {s.name}
                </span>
                <span className="text-right font-mono text-slate-600">
                  {fmtInt(s.amount_yi)}
                  <span className="text-[9px] text-slate-400">亿</span>
                </span>
                <span className="text-right font-mono text-slate-500">
                  {s.share?.toFixed(1) ?? '--'}%
                </span>
                {/* 双向条：零线居中，右=流入（红）左=流出（绿） */}
                <span className="relative h-2.5 bg-slate-100 rounded-sm overflow-hidden">
                  <span className="absolute inset-y-0 left-1/2 w-px bg-slate-300" />
                  {d !== null && (
                    <span
                      className={`absolute inset-y-0 ${inflow ? 'bg-red-400' : 'bg-green-400'}`}
                      style={
                        inflow
                          ? { left: '50%', width: `${Math.max(w, 1)}%` }
                          : { right: '50%', width: `${Math.max(w, 1)}%` }
                      }
                    />
                  )}
                </span>
                <span
                  className={`text-right font-mono font-extrabold ${
                    d === null
                      ? 'text-slate-300'
                      : d > 0
                      ? 'text-red-600'
                      : d < 0
                      ? 'text-green-600'
                      : 'text-slate-400'
                  }`}
                >
                  {d === null ? '--' : `${d > 0 ? '+' : ''}${d.toFixed(2)}`}
                </span>
              </div>
            );
          })}
          <p className="text-[9px] text-slate-400 mt-1 leading-tight">
            占比 = 该板块成交额 / 全市场成交额；右移（红）= 资金正在往该板块集中。
          </p>
        </div>
      )}
    </SectionCard>
  );
};
