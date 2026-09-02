/** AH 对应股比价面板 —— A/H 两地联动对照（窄栏紧凑版） */

import React, { useEffect, useState } from 'react';
import { GitCompareArrows } from 'lucide-react';
import type { HkAhPairItem } from '../types';
import { getAhPairs } from '../services/api';
import { EmptyHint, SectionCard } from './shared/ui';

export const HkAhPanel: React.FC = () => {
  const [items, setItems] = useState<HkAhPairItem[]>([]);
  const [date, setDate] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getAhPairs(100)
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
          <GitCompareArrows className="w-3.5 h-3.5 text-purple-600" />
          AH 对应股联动
        </span>
      }
      extra={<span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">{date}</span>}
    >
      {loading && !items.length ? (
        <EmptyHint loading />
      ) : (
        <div className="flex flex-col max-h-[560px] overflow-y-auto">
          <div className="grid grid-cols-[1fr_52px_1fr] gap-1.5 px-1 pb-1.5 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur">
            <span>港股</span>
            <span className="text-right">溢价</span>
            <span>A股对照</span>
          </div>
          {items.map((it, i) => (
            <div key={it.h_symbol} className="grid grid-cols-[1fr_52px_1fr] gap-1.5 px-1 py-1.5 border-b border-slate-50 last:border-0 items-center">
              <span className="flex items-center gap-1.5 min-w-0">
                <span className="text-[9px] font-extrabold text-slate-300 w-4">{String(i + 1).padStart(2, '0')}</span>
                <span className="text-[10px] font-bold text-slate-800 truncate">{it.h_name}</span>
                <span className="text-[9px] font-mono text-slate-300 flex-shrink-0">{it.h_pct_change >= 0 ? '+' : ''}{it.h_pct_change.toFixed(1)}%</span>
              </span>
              <span className={`text-right text-[10px] font-extrabold font-mono ${it.premium_pct === null || it.premium_pct === undefined ? 'text-slate-300' : it.premium_pct >= 0 ? 'text-amber-600' : 'text-sky-600'}`} title="AH 溢价：>0=A贵（H折价）；<0=倒挂（A折价）">
                {it.premium_pct === null || it.premium_pct === undefined ? '--' : `${it.premium_pct >= 0 ? '+' : ''}${it.premium_pct.toFixed(0)}%`}
              </span>
              <span className="flex items-center gap-1 min-w-0">
                <span className="text-[10px] text-slate-600 truncate">{it.cn_name}</span>
                <span className="text-[9px] font-mono text-slate-400 truncate flex-shrink-0">{it.a_symbol}</span>
              </span>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
};