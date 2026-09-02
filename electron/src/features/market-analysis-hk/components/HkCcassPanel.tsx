/** CCASS 席位穿透面板 —— 港股独有最强数据：中央结算系统机构动向
 *
 * 三大块：全市场集中度榜 / 个股席位下钻（托管行·券商持仓明细）/ 席位异动
 */

import React, { useEffect, useState } from 'react';
import { Building2, Search, UserSearch, LogIn, LogOut } from 'lucide-react';
import { Input } from 'antd';
import type { HkCcassHolding, HkCcassMovers, HkCcassRankings } from '../types';
import { getCcassHolding, getCcassMovers, getCcassRankings } from '../services/api';
import { EmptyHint, RankRow, SectionCard, fmtInt } from './shared/ui';

/** 银行席位（托管行）vs 券商席位 区分 */
function isBankSeeder(id: string): boolean {
  return id.startsWith('C0');
}

export const HkCcassPanel: React.FC = () => {
  const [rankings, setRankings] = useState<HkCcassRankings | null>(null);
  const [movers, setMovers] = useState<HkCcassMovers | null>(null);
  const [holding, setHolding] = useState<HkCcassHolding | null>(null);
  const [query, setQuery] = useState('');
  const [searched, setSearched] = useState('');
  const [loading, setLoading] = useState(false);
  const [holdingLoading, setHoldingLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([getCcassRankings(30), getCcassMovers(20)])
      .then(([rk, mv]) => {
        if (!alive) return;
        setRankings(rk);
        setMovers(mv);
      })
      .catch(() => undefined)
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const drill = (symbol: string) => {
    setSearched(symbol);
    setHoldingLoading(true);
    getCcassHolding(symbol)
      .then((h) => setHolding(h))
      .catch(() => setHolding(null))
      .finally(() => setHoldingLoading(false));
  };

  const onSearch = () => {
    const sym = query.trim();
    if (!sym) return;
    drill(sym);
  };

  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-2.5 items-start">
      {/* 全市场集中度榜 */}
      <SectionCard
        className="xl:col-span-1"
        title={
          <span className="flex items-center gap-1.5">
            <Building2 className="w-3.5 h-3.5 text-purple-600" />
            全市场集中度榜
          </span>
        }
        extra={
          rankings && <span className="text-[10px] font-mono text-slate-400">{rankings.trade_date}</span>
        }
      >
        {loading && !rankings ? (
          <EmptyHint loading />
        ) : rankings?.items.length ? (
          <div className="flex flex-col max-h-[480px] overflow-y-auto pr-0.5">
            <div className="grid grid-cols-[1fr_58px_56px_52px] gap-1.5 px-1 pb-1.5 text-[10px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur">
              <span>标的</span>
              <span className="text-right">前十席</span>
              <span className="text-right">南向席</span>
              <span className="text-right">HHI</span>
            </div>
            {rankings.items.slice(0, 25).map((it, i) => (
              <button
                key={it.symbol}
                onClick={() => drill(it.symbol)}
                className="w-full grid grid-cols-[1fr_58px_56px_52px] gap-1.5 px-1 py-1.5 border-b border-slate-50 last:border-0 hover:bg-purple-50/60 transition-colors text-left cursor-pointer"
              >
                <span className="flex items-center gap-1.5 min-w-0">
                  <span className="text-[10px] font-extrabold text-slate-300 w-4">{(i + 1).toString().padStart(2, '0')}</span>
                  <span className="text-[11px] font-bold text-slate-800 truncate">{it.name}</span>
                  <span className="text-[9px] font-mono text-slate-400 truncate">{it.symbol}</span>
                </span>
                <span className={`text-right text-[11px] font-extrabold font-mono ${it.top10_pct > 90 ? 'text-purple-700' : 'text-slate-700'}`}>
                  {it.top10_pct.toFixed(1)}%
                </span>
                <span className="text-right text-[10px] font-mono text-slate-500">{it.south_pct > 0 ? `${it.south_pct.toFixed(0)}%` : '--'}</span>
                <span className={`text-right text-[10px] font-mono ${it.hhi > 0.3 ? 'text-amber-600' : 'text-slate-500'}`}>
                  {it.hhi.toFixed(2)}
                </span>
              </button>
            ))}
          </div>
        ) : (
          <EmptyHint text="CCASS 数据不可用" />
        )}
      </SectionCard>

      {/* 个股席位下钻 */}
      <SectionCard
        className="xl:col-span-1"
        title={
          <span className="flex items-center gap-1.5">
            <UserSearch className="w-3.5 h-3.5 text-purple-600" />
            个股席位穿透
          </span>
        }
        extra={
          <div className="flex items-center gap-1.5">
            <Input
              placeholder="0700.HK / 9988"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && onSearch()}
              className="!w-28 !rounded-xl !text-[11px]" size="small"
              prefix={<Search className="w-3 h-3 text-slate-400" />}
            />
            <button
              onClick={onSearch}
              className="px-2.5 py-1 rounded-full text-[10px] font-extrabold bg-purple-600 text-white hover:bg-purple-500 transition-colors"
            >
              查询
            </button>
          </div>
        }
      >
        {holdingLoading ? (
          <EmptyHint loading />
        ) : holding ? (
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2">
              <span className="text-sm font-extrabold text-slate-900 flex items-center gap-1.5">
                {holding.name}
                <span className="text-[10px] font-mono text-slate-400">({holding.symbol})</span>
              </span>
              <span className="ml-auto text-[9px] font-mono text-slate-400 whitespace-nowrap">{holding.trade_date}</span>
            </div>
            <div className="grid grid-cols-[1fr_64px_56px] gap-1.5 px-1 pb-1 text-[10px] font-extrabold text-slate-400 border-b border-slate-100">
              <span>参与人</span>
              <span className="text-right">持股量</span>
              <span className="text-right">占比%</span>
            </div>
            <div className="flex flex-col max-h-[400px] overflow-y-auto">
              {holding.items.slice(0, 18).map((it, i) => (
                <div key={it.participant_id + i} className="grid grid-cols-[1fr_64px_56px] gap-1.5 px-1 py-1.5 border-b border-slate-50 last:border-0 items-center">
                  <span className="flex items-center gap-1.5 min-w-0">
                    <span
                      className={`w-10 text-center rounded-md px-1 py-0.5 text-[8px] font-extrabold flex-shrink-0 ${
                        isBankSeeder(it.participant_id)
                          ? 'bg-indigo-50 text-indigo-600 border border-indigo-100'
                          : 'bg-amber-50 text-amber-600 border border-amber-100'
                      }`}
                    >
                      {isBankSeeder(it.participant_id) ? '托管行' : '券商'}
                    </span>
                    <span className="text-[10px] font-bold text-slate-700 truncate">{it.participant_name}</span>
                  </span>
                  <span className="text-right text-[10px] font-mono text-slate-500">{fmtInt(it.holding_quantity)}</span>
                  <span className="text-right text-[10px] font-extrabold font-mono text-slate-800">
                    {it.holding_pct.toFixed(2)}%
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <EmptyHint text="输入港股代码查询其 CCASS 席位结构" />
        )}
      </SectionCard>

      {/* 席位异动（新进/退出合并一栏上下堆叠） */}
      <div className="xl:col-span-1 flex flex-col gap-2.5">
        <SectionCard
          title={<span className="flex items-center gap-1.5"><LogIn className="w-3.5 h-3.5 text-red-500" />席位新进</span>}
          extra={movers && <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">{movers.trade_date}</span>}
          className="flex-1"
        >
          {loading && !movers ? (
            <EmptyHint loading />
          ) : movers?.new_entrants.length ? (
            <div className="flex flex-col">
              {movers.new_entrants.slice(0, 6).map((it, i) => (
                <RankRow
                  key={it.symbol}
                  rank={i + 1}
                  name={it.name}
                  nameSub={it.symbol}
                  main={<span className="text-[9px] font-mono text-slate-400">+{it.count}席</span>}
                  right={<button onClick={() => drill(it.symbol)} className="text-[10px] font-bold text-purple-600 hover:text-purple-800">下钻</button>}
                />
              ))}
            </div>
          ) : (
            <EmptyHint text="无席位新进" />
          )}
        </SectionCard>
        <SectionCard
          title={<span className="flex items-center gap-1.5"><LogOut className="w-3.5 h-3.5 text-green-500" />席位退出</span>}
          extra={movers && <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">{movers.trade_date}</span>}
          className="flex-1"
        >
          {loading && !movers ? (
            <EmptyHint loading />
          ) : movers?.exits.length ? (
            <div className="flex flex-col">
              {movers.exits.slice(0, 6).map((it, i) => (
                <RankRow
                  key={it.symbol}
                  rank={i + 1}
                  name={it.name}
                  nameSub={it.symbol}
                  main={<span className="text-[9px] font-mono text-slate-400">-{it.count}席</span>}
                  right={<button onClick={() => drill(it.symbol)} className="text-[10px] font-bold text-purple-600 hover:text-purple-800">下钻</button>}
                />
              ))}
            </div>
          ) : (
            <EmptyHint text="无席位退出" />
          )}
        </SectionCard>
      </div>
    </div>
  );
};