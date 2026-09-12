/** 美股 52 周位置榜 —— 创新高 / 创新低 / 贴近 52 周高点 / 距高点最远
 *
 * drawdown_pct 口径：((收盘 / 52周最高) - 1) × 100，0 表示正处 52 周高点，负值表示距高点回撤。
 * 后端字段可能为 null（数据不足），展示前统一过 safeNum，禁止裸 .toFixed。
 * 排版按看盘密度：行高压到 py-[3px]，每行带齐「现价 / 涨跌 / 距高点 / 52周高 / 52周低」。
 */

import React, { useEffect, useState } from 'react';
import { TrendingUp } from 'lucide-react';
import { getBreadthHighlights } from '../services/api';
import type { UsBreadthHighlights } from '../types';
import {
  SectionCard, EmptyHint, PctText, DateBadge, fmtInt,
} from '../../market-analysis-shared/ui';

type HighlightTab = 'new_highs' | 'new_lows' | 'near_high' | 'far_from_high';

const TABS: Array<{ id: HighlightTab; label: string; hint: string }> = [
  // 默认展示「贴近 52 周高点」：新高股必然在其中（回撤 0），且列表更长更可操作；
  // 单看「创新高」在弱势日只有个位数条目，面板会显得空
  { id: 'near_high', label: '贴近 52 周高点', hint: '距 52 周高点最近的成分股（强势整理，含当日创新高）' },
  { id: 'new_highs', label: '创新高', hint: '当日创 52 周新高的成分股' },
  { id: 'new_lows', label: '创新低', hint: '当日创 52 周新低的成分股' },
  { id: 'far_from_high', label: '距高点最远', hint: '距 52 周高点回撤最深的成分股' },
];

/** 表格列：# / 标的（名称+代码）/ 现价 / 涨跌 / 距高点 / 52周高 / 52周低 */
const GRID = 'grid grid-cols-[16px_1fr_54px_48px_52px_56px_56px] gap-1';

/** null / NaN / Infinity / 非数字 一律收敛为 null */
const safeNum = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null;

/** 距高点为负值，不能用红涨绿跌；按「贴近高点 / 深跌」分档着色 */
function ddTone(v: number | null): string {
  if (v === null) return 'text-slate-300';
  if (v >= -0.5) return 'text-red-600 font-extrabold';
  if (v >= -5) return 'text-orange-600 font-bold';
  if (v <= -40) return 'text-green-700 font-bold';
  return 'text-slate-500';
}

function ddText(v: number | null): string {
  if (v === null) return '--';
  return `${v > 0 ? '+' : ''}${v.toFixed(1)}%`;
}

export const UsBreadthHighlightsPanel: React.FC = () => {
  const [data, setData] = useState<UsBreadthHighlights | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<HighlightTab>('near_high');

  useEffect(() => {
    let alive = true;
    getBreadthHighlights(40)
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
  const active = TABS.find((t) => t.id === tab);

  return (
    <SectionCard
      className="!p-2.5 !gap-1.5"
      title={
        <span className="flex items-center gap-1.5">
          <TrendingUp className="w-3.5 h-3.5 text-blue-600" />
          52 周位置榜
          <span className="text-[9px] font-normal text-slate-400">{active?.hint}</span>
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
      {/* 榜单切换：紧凑按钮组（原 chip 太占高度） */}
      <div className="flex items-center justify-between gap-2 flex-wrap -mt-1">
        <div className="flex items-center gap-1 flex-wrap">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              title={t.hint}
              className={`px-2 py-[3px] rounded-md text-[10px] font-extrabold transition-colors ${
                tab === t.id
                  ? 'bg-blue-600 text-white'
                  : 'bg-slate-100 text-slate-500 hover:bg-slate-200 hover:text-slate-700'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <span className="text-[9px] font-mono text-slate-400 whitespace-nowrap">
          {rows.length} 只
        </span>
      </div>

      {rows.length === 0 ? (
        <EmptyHint loading={loading} />
      ) : (
        <div className="flex flex-col max-h-[560px] overflow-y-auto">
          <div
            className={`${GRID} px-1 py-[3px] text-[9px] font-bold text-slate-400 border-b border-slate-200 sticky top-0 bg-white/95 backdrop-blur z-10`}
          >
            <span>#</span>
            <span>标的</span>
            <span className="text-right">现价</span>
            <span className="text-right">涨跌</span>
            <span className="text-right" title="距 52 周高点涨跌幅（0.00% = 正处高点，负值 = 低于高点）">
              距高点
            </span>
            <span className="text-right" title="52 周最高价">
              52周高
            </span>
            <span className="text-right" title="52 周最低价">
              52周低
            </span>
          </div>

          {rows.map((it, i) => {
            const dd = safeNum(it.drawdown_pct);
            return (
              <div
                key={`${it.symbol}-${i}`}
                className={`${GRID} px-1 py-[3px] text-[10px] items-center border-b border-slate-50 last:border-0 hover:bg-slate-50/80`}
              >
                <span
                  className={`text-[9px] font-extrabold ${
                    i < 3 ? 'text-blue-600' : 'text-slate-400'
                  }`}
                >
                  {i + 1}
                </span>
                <span className="flex items-center gap-1 min-w-0">
                  <span className="font-bold text-slate-800 truncate" title={it.name || it.symbol}>
                    {it.name || it.symbol}
                  </span>
                  <span className="font-mono text-[9px] text-slate-400 truncate">{it.symbol}</span>
                </span>
                <span className="text-right font-mono text-slate-700">
                  $ {fmtInt(safeNum(it.close))}
                </span>
                <span className="text-right">
                  <PctText value={safeNum(it.pct_change)} className="text-[10px]" />
                </span>
                <span className={`text-right font-mono ${ddTone(dd)}`} title="距 52 周高点">
                  {ddText(dd)}
                </span>
                <span className="text-right font-mono text-[9px] text-slate-400">
                  {fmtInt(safeNum(it.high_52w))}
                </span>
                <span className="text-right font-mono text-[9px] text-slate-400">
                  {fmtInt(safeNum(it.low_52w))}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </SectionCard>
  );
};
