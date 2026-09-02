/** AH 溢价榜 —— 同一资产两地比价（H 折价 / 倒挂两角） */

import React, { useEffect, useState } from 'react';
import { Scale } from 'lucide-react';
import type { HkAhPremium } from '../types';
import { getAhPremium } from '../services/api';
import { EmptyHint, RankRow, SectionCard } from './shared/ui';

export const HkAhPremiumPanel: React.FC = () => {
  const [data, setData] = useState<HkAhPremium | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getAhPremium(20)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const renderList = (items: typeof data.premium, isPremium: boolean) =>
    items.slice(0, 10).map((it, i) => (
      <RankRow
        key={it.h_symbol + String(isPremium)}
        rank={i + 1}
        name={it.h_name}
        nameSub={it.h_symbol}
        main={
          <span className="text-[9px] font-mono text-slate-400 truncate">
            {it.a_symbol}
          </span>
        }
        right={
          <span className={`text-[11px] font-extrabold font-mono flex-shrink-0 ${isPremium ? 'text-amber-600' : 'text-sky-600'}`}>
            {isPremium ? 'A贵+H' : '倒挂'}{it.premium_pct.toFixed(0)}%
          </span>
        }
      />
    ));

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <Scale className="w-3.5 h-3.5 text-purple-600" />
          AH 溢价 · 两地比价
        </span>
      }
      extra={
        <div className="flex items-center gap-1">
          {data && <span className="text-[9px] font-mono text-slate-400">{data.trade_date}</span>}
        </div>
      }
    >
      {loading && !data ? (
        <EmptyHint loading />
      ) : data ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between px-1">
            <span className="text-[10px] font-extrabold text-amber-600 flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
              H 折价（A 贵，买 H 划算）
            </span>
            <span className="text-[9px] text-slate-400">溢价↑</span>
          </div>
          <div className="rounded-xl border border-amber-100/70 bg-amber-50/30 p-1">
            {data.premium.length ? renderList(data.premium, true) : <EmptyHint text="暂无数据" />}
          </div>
          <div className="flex items-center justify-between px-1 pt-1">
            <span className="text-[10px] font-extrabold text-sky-600 flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-sky-500" />
              倒挂（H 贵，A 折价）
            </span>
            <span className="text-[9px] text-slate-400">溢价↓</span>
          </div>
          <div className="rounded-xl border border-sky-100/70 bg-sky-50/30 p-1">
            {data.discount.length ? renderList(data.discount, false) : <EmptyHint text="暂无数据" />}
          </div>
        </div>
      ) : (
        <EmptyHint text="AH 溢价数据不可用" />
      )}
    </SectionCard>
  );
};