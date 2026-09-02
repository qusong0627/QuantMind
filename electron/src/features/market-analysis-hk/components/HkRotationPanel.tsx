/** 恒生行业轮动面板 —— 1/5/20 日多周期强弱 + 成交额（窄栏紧凑版） */

import React, { useEffect, useState } from 'react';
import { RefreshCcw } from 'lucide-react';
import type { HkRotationItem } from '../types';
import { getSectorRotation } from '../services/api';
import { EmptyHint, PctText, SectionCard, fmtInt } from './shared/ui';

export const HkRotationPanel: React.FC = () => {
  const [items, setItems] = useState<HkRotationItem[]>([]);
  const [date, setDate] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getSectorRotation(30)
      .then((d) => {
        if (!alive) return;
        setItems(d.items);
        setDate(d.trade_date);
      })
      .catch(() => undefined)
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <SectionCard
      className="h-full flex flex-col"
      title={
        <span className="flex items-center gap-1.5">
          <RefreshCcw className="w-3.5 h-3.5 text-purple-600" />
          恒生行业轮动（1/5/20日）
        </span>
      }
      extra={<span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">截至 {date}</span>}
    >
      {loading && !items.length ? (
        <EmptyHint loading />
      ) : (
        <div className="flex flex-col max-h-[560px] overflow-y-auto">
          <div className="grid grid-cols-[1fr_44px_52px_44px] gap-1.5 px-1 pb-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur z-10">
            <span>行业 · 1日</span>
            <span className="text-right">5日</span>
            <span className="text-right">20日</span>
            <span className="text-right">成交</span>
          </div>
          {items.map((it, i) => (
            <div key={it.name} className="grid grid-cols-[1fr_44px_52px_44px] gap-1.5 px-1 py-1.5 border-b border-slate-50 last:border-0 items-center">
              <span className="flex items-center gap-1.5 min-w-0">
                <span className="text-[9px] font-extrabold text-slate-300 w-4 flex-shrink-0">{String(i + 1).padStart(2, '0')}</span>
                <span className="text-[10px] font-bold text-slate-800 truncate">{it.name}</span>
                {it.ret_1d !== undefined && (
                  <span className="text-[9px] font-mono text-slate-400 flex-shrink-0">
                    {it.ret_1d >= 0 ? '+' : ''}{it.ret_1d.toFixed(1)}%
                  </span>
                )}
              </span>
              <span className="text-right"><PctText value={it.ret_5d} className="text-[10px]" /></span>
              <span className="text-right"><PctText value={it.ret_20d} className="text-[10px]" /></span>
              <span className="text-right text-[9px] font-mono text-slate-500 whitespace-nowrap">{fmtInt(it.turnover_yi)}亿</span>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
};