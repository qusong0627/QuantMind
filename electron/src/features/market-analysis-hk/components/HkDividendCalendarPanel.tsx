/** 派息日历 —— 未来除息（ex_date）的港股公司，提前发现除息窗口 */

import React, { useEffect, useState } from 'react';
import { CalendarClock } from 'lucide-react';
import type { HkDividendCalendar } from '../types';
import { getDividendCalendar } from '../services/api';
import { EmptyHint, SectionCard, fmtInt } from './shared/ui';

export const HkDividendCalendarPanel: React.FC = () => {
  const [data, setData] = useState<HkDividendCalendar | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getDividendCalendar(60, 40)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <CalendarClock className="w-3.5 h-3.5 text-purple-600" />
          派息日历（未来 60 天除息）
        </span>
      }
      extra={<span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">除息日近→远</span>}
    >
      {loading && !data ? (
        <EmptyHint loading />
      ) : data?.items.length ? (
        <div className="flex flex-col max-h-[420px] overflow-y-auto">
          <div className="grid grid-cols-[1fr_76px_56px] gap-1.5 px-1 pb-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur">
            <span>公司（除息日）</span>
            <span className="text-right">派息方案</span>
            <span className="text-right">派息日</span>
          </div>
          {data.items.map((it, i) => (
            <div key={it.symbol + it.ex_date} className="grid grid-cols-[1fr_76px_56px] gap-1.5 px-1 py-1.5 border-b border-slate-50 last:border-0 items-center">
              <span className="flex items-center gap-1.5 min-w-0">
                <span className="text-[9px] font-extrabold text-amber-500 bg-amber-50 border border-amber-100 rounded px-1 py-0.5 flex-shrink-0 whitespace-nowrap">
                  {it.ex_date.slice(5)}
                </span>
                <span className="text-[10px] font-bold text-slate-800 truncate">{it.name}</span>
              </span>
              <span className="text-right text-[9px] font-mono text-slate-500 truncate" title={it.plan}>
                {it.plan || (it.dividend !== null ? `每股 ${it.dividend}` : '--')}
              </span>
              <span className="text-right text-[9px] font-mono text-slate-400 whitespace-nowrap">
                {it.pay_date || '--'}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <EmptyHint text="未来 60 天内暂无披露除息" />
      )}
    </SectionCard>
  );
};