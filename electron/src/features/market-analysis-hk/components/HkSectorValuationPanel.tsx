/** 行业估值温度计 —— 恒生行业 × PE 中位数 / 平均股息率（价值洼地识别） */

import React, { useEffect, useState } from 'react';
import { ThermometerSun } from 'lucide-react';
import type { HkSectorValuationItem } from '../types';
import { getSectorValuation } from '../services/api';
import { EmptyHint, SectionCard } from './shared/ui';

export const HkSectorValuationPanel: React.FC = () => {
  const [items, setItems] = useState<HkSectorValuationItem[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getSectorValuation(24)
      .then((d) => alive && setItems(d))
      .catch(() => alive && setItems([]))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const maxDy = Math.max(...items.map((i) => i.dividend_yield ?? 0), 1);

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <ThermometerSun className="w-3.5 h-3.5 text-orange-500" />
          行业估值温度计（PE 中位 / 股息率）
        </span>
      }
      extra={<span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">按股息率降序</span>}
    >
      {loading && !items.length ? (
        <EmptyHint loading />
      ) : (
        <div className="flex flex-col max-h-[560px] overflow-y-auto">
          <div className="grid grid-cols-[1fr_46px_1fr] gap-1.5 px-1 pb-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur z-10">
            <span>行业</span>
            <span className="text-right">PE中位</span>
            <span>周期股息率</span>
          </div>
          {items.map((it) => (
            <div key={it.name} className="grid grid-cols-[1fr_46px_1fr] gap-1.5 px-1 py-1 border-b border-slate-50 last:border-0 items-center">
              <span className="flex items-center gap-1 min-w-0">
                <span className="text-[10px] font-bold text-slate-800 truncate">{it.name}</span>
                <span className="text-[8px] font-mono text-slate-300">{it.stock_count}</span>
              </span>
              <span className="text-right text-[10px] font-mono text-slate-600">
                {it.pe_median !== null ? it.pe_median.toFixed(1) : '--'}
              </span>
              <span className="flex items-center gap-1.5">
                <div className="flex-1 h-2 rounded-full bg-slate-100 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-gradient-to-r from-emerald-400 to-teal-500"
                    style={{ width: `${((it.dividend_yield ?? 0) / maxDy) * 100}%` }}
                  />
                </div>
                <span className="w-10 text-right text-[10px] font-extrabold font-mono text-emerald-600">
                  {it.dividend_yield !== null ? `${it.dividend_yield.toFixed(1)}%` : '--'}
                </span>
              </span>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
};