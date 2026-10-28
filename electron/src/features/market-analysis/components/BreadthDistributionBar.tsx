/**
 * 七档涨跌分布（当日计数，互斥）—— 原 K 线卡底部的分布条。
 *
 * 保留这条是因为它比 K 线更直接回答「今天多少票在跌」；K 线本身已从面板移除。
 * 颜色沿用 A 股红涨绿跌；缺失（旧快照）整块不渲染，不拿 0 冒充。
 */
import React from 'react';
import type { BreadthDistribution } from '../services/sentimentService';

const ROWS: { key: string; label: string; color: string }[] = [
  { key: 'dist_limit_up', label: '涨停', color: '#dc2626' },
  { key: 'dist_up_big', label: '涨>5%', color: '#f87171' },
  { key: 'dist_up_small', label: '涨0~5%', color: '#fca5a5' },
  { key: 'dist_flat', label: '平', color: '#cbd5e1' },
  { key: 'dist_down_small', label: '跌0~5%', color: '#6ee7b7' },
  { key: 'dist_down_big', label: '跌>5%', color: '#34d399' },
  { key: 'dist_limit_down', label: '跌停', color: '#059669' },
];

interface Props {
  dist?: BreadthDistribution;
  /** 该宇宙当日样本数（分母说明） */
  total?: number | null;
  date?: string;
}

export const BreadthDistributionBar: React.FC<Props> = ({ dist, total, date }) => {
  if (!dist) return null;
  const items = ROWS.map((r) => ({ ...r, value: dist[r.key] ?? null }));
  const sum = items.reduce((acc, it) => acc + (it.value ?? 0), 0);
  if (!sum) return null;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-extrabold text-slate-600">涨跌分布</span>
        <span className="text-[9px] text-slate-400 font-mono">
          {date ? `${date} · ` : ''}合计 {total ?? sum} 只
        </span>
      </div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
        {items.map((it) =>
          it.value && it.value > 0 ? (
            <div
              key={it.key}
              title={`${it.label} ${it.value} 只`}
              style={{ width: `${(it.value / sum) * 100}%`, background: it.color }}
            />
          ) : null,
        )}
      </div>
      <div className="flex items-center gap-x-2 gap-y-0.5 flex-wrap text-[9px] text-slate-500">
        {items.map((it) => (
          <span key={it.key} className="inline-flex items-center gap-0.5">
            <span className="w-1.5 h-1.5 rounded-full" style={{ background: it.color }} />
            {it.label}
            <b className="font-mono text-slate-700">{it.value ?? '—'}</b>
          </span>
        ))}
      </div>
    </div>
  );
};
