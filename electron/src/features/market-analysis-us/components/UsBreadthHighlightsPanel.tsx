/** 美股 52 周位置榜 —— 创新高 / 创新低 / 贴近 52 周高点 / 距高点最远
 *
 * drawdown_pct 口径：((收盘 / 52周最高) - 1) × 100，0 表示正处 52 周高点，负值表示距高点回撤。
 * 后端字段可能为 null（数据不足），展示前统一过 safeNum，禁止裸 .toFixed。
 */

import React, { useEffect, useState } from 'react';
import { TrendingUp } from 'lucide-react';
import { getBreadthHighlights } from '../services/api';
import type { UsBreadthHighlights } from '../types';
import {
  SectionCard, RankRow, EmptyHint, PctText, DateBadge, PeriodChips, fmtInt,
} from '../../market-analysis-shared/ui';

type HighlightTab = 'new_highs' | 'new_lows' | 'near_high' | 'far_from_high';

const TABS: Array<{ id: HighlightTab; label: string }> = [
  { id: 'new_highs', label: '创新高' },
  { id: 'new_lows', label: '创新低' },
  { id: 'near_high', label: '贴近 52 周高点' },
  { id: 'far_from_high', label: '距高点最远' },
];

/** null / NaN / Infinity / 非数字 一律收敛为 null */
const safeNum = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null;

export const UsBreadthHighlightsPanel: React.FC = () => {
  const [data, setData] = useState<UsBreadthHighlights | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<HighlightTab>('new_highs');

  useEffect(() => {
    let alive = true;
    getBreadthHighlights(30)
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

  const rows = data && Array.isArray(data[tab]) ? data[tab] : [];
  const counts = data?.high_low_counts;

  return (
    <SectionCard
      title={
        <span className="flex items-center gap-1.5">
          <TrendingUp className="w-3.5 h-3.5 text-blue-600" />
          52 周位置榜
        </span>
      }
      extra={
        <div className="flex items-center gap-2">
          <span
            className="text-[10px] font-mono text-slate-400 whitespace-nowrap"
            title="当日创 52 周新高 / 新低的股票数（标的池内）"
          >
            新高 <b className="text-red-600">{fmtInt(safeNum(counts?.new_highs))}</b>
            {' · '}
            新低 <b className="text-green-600">{fmtInt(safeNum(counts?.new_lows))}</b>
          </span>
          <DateBadge label="数据日期" date={data?.trade_date} />
        </div>
      }
    >
      <div className="flex items-center justify-between gap-2">
        <PeriodChips
          options={TABS}
          value={tab}
          onChange={(id) => setTab(id as HighlightTab)}
          accent="blue"
        />
        <span className="text-[10px] font-mono text-slate-400 whitespace-nowrap">
          {rows.length} 只
        </span>
      </div>

      {rows.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div className="flex flex-col max-h-[520px] overflow-y-auto">
          {rows.map((it, i) => (
            <RankRow
              key={`${it.symbol}-${i}`}
              rank={i + 1}
              name={it.name || it.symbol}
              nameSub={it.symbol}
              main={
                <span className="text-[10px] font-mono text-slate-400">
                  $ {fmtInt(safeNum(it.close))}
                </span>
              }
              right={
                <span
                  className="flex-shrink-0"
                  title="距 52 周高点涨跌幅（0.00% = 正处高点，负值 = 低于高点）"
                >
                  <PctText value={safeNum(it.drawdown_pct)} className="text-[11px]" />
                </span>
              }
            />
          ))}
        </div>
      )}
    </SectionCard>
  );
};
