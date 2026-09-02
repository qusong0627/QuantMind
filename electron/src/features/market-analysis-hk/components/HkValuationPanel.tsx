/** 港股估值主题面板 —— 高股息 / 低 PE / 低 PB（港股特色主题） */

import React, { useEffect, useState } from 'react';
import { Coins, PiggyBank, Percent, Receipt, Banknote } from 'lucide-react';
import type { HkValuationRanking } from '../types';
import { getValuationRankings } from '../services/api';
import { EmptyHint, RankRow, SectionCard, fmtInt } from './shared/ui';

const TABS = [
  { id: 'dividend', label: '高股息', icon: Coins },
  { id: 'pe', label: '低PE', icon: Percent },
  { id: 'pb', label: '低PB', icon: PiggyBank },
  { id: 'ps', label: '低PS', icon: Receipt },
  { id: 'pcf', label: '低PCF', icon: Banknote },
] as const;

type TabId = (typeof TABS)[number]['id'];

export const HkValuationPanel: React.FC = () => {
  const [tab, setTab] = useState<TabId>('dividend');
  const [data, setData] = useState<HkValuationRanking | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getValuationRankings(tab)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [tab]);

  const items = data?.items ?? [];

  const valueLabel = (it: (typeof items)[number]) => {
    if (tab === 'dividend') return { text: `${it.value.toFixed(2)}%`, sub: it.pe !== undefined ? `PE ${it.pe.toFixed(1)}` : '' };
    if (tab === 'pe') return { text: it.value.toFixed(2), sub: it.pb !== undefined ? `PB ${it.pb.toFixed(2)}` : '' };
    return { text: it.value.toFixed(2), sub: it.pe !== undefined ? `PE ${it.pe.toFixed(1)}` : '' };
  };

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <Coins className="w-3.5 h-3.5 text-purple-600" />
          港股估值主题选股（股息 / 估值双维）
        </span>
      }
      extra={
        <div className="flex items-center gap-2">
          {data?.published_at && (
            <span className="text-[10px] font-mono text-slate-400">数据快照 {data.published_at}</span>
          )}
          <div className="flex items-center gap-1 rounded-full bg-slate-100 p-0.5">
            {TABS.map((t) => {
              const Icon = t.icon;
              return (
                <button
                  key={t.id}
                  onClick={() => setTab(t.id)}
                  className={`flex items-center gap-1 px-3 py-1 rounded-full text-[11px] font-extrabold transition-all ${
                    tab === t.id
                      ? 'bg-white text-purple-700 shadow-2xs border border-purple-200'
                      : 'text-slate-500 hover:text-slate-800'
                  }`}
                >
                  <Icon className="w-3 h-3" />
                  {t.label}
                </button>
              );
            })}
          </div>
        </div>
      }
    >
      {loading && !data ? (
        <EmptyHint loading />
      ) : items.length ? (
        <div className="flex flex-col">
          {items.map((it, i) => {
            const v = valueLabel(it);
            return (
              <RankRow
                key={it.symbol}
                rank={i + 1}
                name={it.name}
                nameSub={it.symbol}
                main={
                  it.total_market_cap_yi !== undefined && (
                    <span className="text-[10px] font-mono text-slate-400">
                      市值 {fmtInt(it.total_market_cap_yi)} 亿
                    </span>
                  )
                }
                right={
                  <div className="flex items-center gap-3 flex-shrink-0">
                    {v.sub && <span className="text-[10px] font-mono text-slate-400">{v.sub}</span>}
                    <span className="w-20 text-right text-xs font-extrabold font-mono text-purple-700">
                      {v.text}
                    </span>
                  </div>
                }
              />
            );
          })}
        </div>
      ) : (
        <EmptyHint text="估值数据不可用" />
      )}
    </SectionCard>
  );
};