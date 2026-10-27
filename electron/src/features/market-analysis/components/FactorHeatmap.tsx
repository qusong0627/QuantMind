import React from 'react';
import { factorShortName } from './BreadthFactorBars';
import type { FactorHistoryItem } from '../services/sentimentService';

/**
 * 分项冷热热力图：8 个指标 × 近 60 个交易日。
 * 颜色＝冷度（红热→绿冷），用来一眼看出「哪些指标在变冷、哪些在变热」。
 */

/** 冷度 0~100 → 颜色（红→琥珀→灰→青→绿，五段线性）。 */
export function coldColor(cold: number): string {
  const stops: [number, [number, number, number]][] = [
    [0, [220, 38, 38]],
    [25, [245, 158, 11]],
    [50, [148, 163, 184]],
    [75, [20, 184, 166]],
    [100, [16, 185, 129]],
  ];
  const v = Math.max(0, Math.min(100, cold));
  for (let i = 1; i < stops.length; i++) {
    const [hi, c2] = stops[i];
    const [lo, c1] = stops[i - 1];
    if (v <= hi) {
      const t = (v - lo) / (hi - lo);
      const rgb = c1.map((a, k) => Math.round(a + (c2[k] - a) * t));
      return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
    }
  }
  return 'rgb(16, 185, 129)';
}

interface Props {
  dates: string[];
  items: FactorHistoryItem[];
}

export const FactorHeatmap: React.FC<Props> = ({ dates, items }) => {
  if (!dates.length || !items.length) return null;
  const pct = (s: string) => (s.length >= 10 ? s.slice(5) : s);
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between text-[10px] text-slate-400">
        <span className="font-bold text-slate-600">分项冷热 · 近 {dates.length} 个交易日</span>
        <span className="flex items-center gap-2">
          <span className="flex items-center gap-1">
            <span className="inline-block w-3 h-2 rounded-sm" style={{ background: coldColor(10) }} />热
            <span className="inline-block w-3 h-2 rounded-sm ml-1" style={{ background: coldColor(50) }} />中性
            <span className="inline-block w-3 h-2 rounded-sm ml-1" style={{ background: coldColor(90) }} />冷
          </span>
          <span className="font-mono">{pct(dates[0])} → {pct(dates[dates.length - 1])}</span>
        </span>
      </div>

      <div className="flex flex-col gap-[3px]">
        {items.map((it) => (
          <div key={it.key} className="grid grid-cols-[62px_1fr] items-center gap-1.5">
            <span className="text-[10px] text-slate-600 truncate" title={it.label}>
              {factorShortName(it.key, it.label)}
            </span>
            <div className="flex gap-[2px]">
              {it.cold.map((c, i) => (
                <div
                  key={i}
                  className="flex-1 h-3 rounded-[2px]"
                  style={{ background: c === null ? '#f1f5f9' : coldColor(c) }}
                  title={`${dates[i]} ${c === null ? '无数据' : `冷度 ${Math.round(c)}`}`}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* 底部日期轴：与上面 60 列对齐 */}
      <div className="grid grid-cols-[62px_1fr] items-center gap-1.5">
        <span />
        <div className="relative h-3">
          {[0, 0.25, 0.5, 0.75, 1].map((f) => {
            const idx = Math.round(f * (dates.length - 1));
            const align = f === 0 ? 'none' : f === 1 ? 'translateX(-100%)' : 'translateX(-50%)';
            return (
              <span
                key={f}
                className="absolute top-0 text-[9px] text-slate-400 font-mono whitespace-nowrap"
                style={{ left: `${f * 100}%`, transform: align }}
              >
                {pct(dates[idx])}
              </span>
            );
          })}
        </div>
      </div>
    </div>
  );
};
