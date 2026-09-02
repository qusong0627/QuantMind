/** 个股综合赚钱效应 Top N —— 涨幅 × 成交活跃度评分（港股特色口径） */

import React, { useEffect, useState } from 'react';
import { Trophy } from 'lucide-react';
import type { HkProfitLeaders } from '../types';
import { getProfitLeaders } from '../services/api';
import { EmptyHint, PctText, RankRow, SectionCard, fmtInt } from './shared/ui';

export const ProfitLeadersCard: React.FC<{ height?: number }> = ({ height = 360 }) => {
  const [data, setData] = useState<HkProfitLeaders | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getProfitLeaders(10)
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
          <Trophy className="w-3.5 h-3.5 text-amber-500" />
          个股综合赚钱效应 Top 10
        </span>
      }
      extra={
        data && <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">{data.trade_date}</span>
      }
    >
      {loading && !data ? (
        <EmptyHint loading />
      ) : data?.items.length ? (
        <div className="flex flex-col" style={{ maxHeight: height, overflowY: 'auto' }}>
          <div className="grid grid-cols-[1fr_44px_44px] gap-1.5 px-1 pb-1 text-[9px] font-extrabold text-slate-400 border-b border-slate-100 sticky top-0 bg-white/95 backdrop-blur">
            <span>个股（评分 = 涨幅 + 成交活跃）</span>
            <span className="text-right">涨跌</span>
            <span className="text-right">成交</span>
          </div>
          {data.items.map((it, i) => (
            <RankRow
              key={it.symbol}
              rank={i + 1}
              name={it.name}
              nameSub={it.symbol}
              main={
                <span className="text-[9px] font-mono text-amber-500 flex-shrink-0" title={`评分 = 涨跌幅 + 2×log10(成交额/1亿)`}>
                  {it.score.toFixed(1)}分
                </span>
              }
              right={
                <div className="flex items-center gap-2 flex-shrink-0">
                  <PctText value={it.pct_change} className="text-[11px]" />
                  <span className="w-11 text-right text-[9px] font-mono text-slate-400">
                    {fmtInt(it.turnover_yi)}亿
                  </span>
                </div>
              }
            />
          ))}
        </div>
      ) : (
        <EmptyHint text="暂无赚钱效应数据" />
      )}
    </SectionCard>
  );
};