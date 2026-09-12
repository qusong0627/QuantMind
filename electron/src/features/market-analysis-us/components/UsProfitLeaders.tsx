/** 美股赚钱效应榜 —— 涨幅 × 成交额活跃度综合评分 Top N */

import React, { useEffect, useState } from 'react';
import { Trophy } from 'lucide-react';
import { getProfitLeaders } from '../services/api';
import type { UsProfitLeaders as UsProfitLeadersData } from '../types';
import { SectionCard, RankRow, EmptyHint, PctText, fmtInt } from '../../market-analysis-shared/ui';

export const UsProfitLeaders: React.FC<{ limit?: number }> = ({ limit = 10 }) => {
  const [data, setData] = useState<UsProfitLeadersData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    getProfitLeaders(limit)
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
  }, [limit]);

  const items = data?.items ?? [];

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <Trophy className="w-3.5 h-3.5 text-amber-500" />
          赚钱效应榜
        </span>
      }
      extra={
        data?.trade_date ? (
          <span className="text-[10px] font-mono text-slate-400">{data.trade_date}</span>
        ) : undefined
      }
    >
      {items.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div className="flex flex-col">
          {items.map((it, i) => (
            <RankRow
              key={it.symbol}
              rank={i + 1}
              name={it.name}
              nameSub={it.symbol}
              right={<PctText value={it.pct_change} />}
              main={
                <span className="text-[10px] font-mono text-slate-400">
                  US$ {fmtInt(it.amount_yi)}亿
                </span>
              }
            />
          ))}
        </div>
      )}
    </SectionCard>
  );
};
