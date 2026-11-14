import React from 'react';
import type { IndexItem } from '../BroadMarketHeader';
import { fmtPct, fmtPrice, isNum, toneBySign } from './format';

function Sparkline({
  values,
  color,
}: {
  values?: number[];
  color: string;
}): React.ReactElement | null {
  if (!values || values.length < 2) return null;
  const w = 84;
  const h = 24;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - 2 - ((v - min) / span) * (h - 4);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const area = `0,${h} ${pts.join(' ')} ${w},${h}`;
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="shrink-0" aria-hidden>
      <polygon points={area} fill={color} fillOpacity={0.1} />
      <polyline
        points={pts.join(' ')}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** 指数快照带：5 个指数一格，价格/涨跌/成交额 + 迷你走势，替代原来的大卡片。 */
export function IndexTape({
  indices,
  loading,
  note,
}: {
  indices: IndexItem[];
  loading?: boolean;
  note?: string;
}): React.ReactElement {
  if (!indices.length) {
    if (note) {
      return (
        <div className="rounded-xl border border-amber-200/70 bg-amber-50/60 px-3 py-2 text-[10px] font-bold text-amber-700">
          {note}
        </div>
      );
    }
    return (
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-5">
        {Array.from({ length: 5 }).map((_, i) => (
          <div
            key={i}
            className="h-[62px] animate-pulse rounded-xl border border-slate-100 bg-slate-50"
          />
        ))}
        {!loading && (
          <div className="col-span-full text-center text-[11px] text-slate-400">
            指数快照暂无数据
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 overflow-hidden rounded-xl border border-slate-200/80 bg-white shadow-sm lg:grid-cols-5 lg:divide-x lg:divide-slate-100">
      {indices.map((item) => {
        const pct = item.pct_change;
        const up = isNum(pct) && pct >= 0;
        const color = up ? '#e11d48' : '#059669';
        return (
          <div key={item.symbol} className="px-3 py-2">
            <div className="flex items-baseline justify-between gap-2">
              <span className="truncate text-[11px] font-bold text-slate-700">
                {item.name}
              </span>
              <span className={`font-mono text-[11px] font-extrabold ${toneBySign(pct)}`}>
                {fmtPct(pct)}
              </span>
            </div>
            <div className="mt-0.5 flex items-end justify-between gap-2">
              <span className={`font-mono text-base font-black tracking-tight ${toneBySign(pct)}`}>
                {fmtPrice(item.price)}
              </span>
              <Sparkline values={item.trend} color={color} />
            </div>
            <div className="mt-0.5 flex items-center justify-between text-[9px] font-mono text-slate-400">
              <span>{item.symbol}</span>
              <span>成交 ¥{isNum(item.turnover) ? item.turnover.toLocaleString() : '—'} 亿</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
