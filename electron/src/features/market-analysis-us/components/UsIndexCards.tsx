/** 美股五大指数快照卡（标普/纳指100/纳指/道指/费半）
 *
 * 与港股卡的差异（数据源决定，不是设计取舍）：
 * - index_daily 的 `amount` 列**恒为 0** → 不显示成交额，改用 `volume`（股数）
 * - `SOX.US` 的 volume 也是 0 → 该项整体隐藏，不渲染成 0
 * - 指数分区通常滞后个股若干天 → 卡片右上角标注各自的 `trade_date`
 */

import React from 'react';
import { Activity } from 'lucide-react';
import type { UsIndexItem } from '../types';

/** 股数 → 紧凑可读（亿股 / 万股） */
function fmtVolume(v: number | null | undefined): string | null {
  if (v === null || v === undefined || Number.isNaN(v) || v <= 0) return null;
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)} 亿股`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(1)} 万股`;
  return `${v.toLocaleString('zh-CN')} 股`;
}

export const UsIndexCards: React.FC<{ indices: UsIndexItem[]; loading?: boolean }> = ({
  indices,
  loading,
}) => {
  if (loading) {
    return (
      <div className="grid grid-cols-2 xl:grid-cols-5 gap-1.5">
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="animate-pulse h-14 bg-white/70 rounded-xl border border-slate-200/70" />
        ))}
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 xl:grid-cols-5 gap-1.5">
      {indices.map((idx) => {
        const up = idx.pct_change >= 0;
        const vol = fmtVolume(idx.volume);
        return (
          <div
            key={idx.symbol}
            className="bg-white/90 backdrop-blur-md rounded-xl px-2.5 py-1.5 border border-slate-200/80 shadow-sm flex flex-col gap-0.5 hover:shadow-md transition-shadow"
          >
            <div className="flex items-center justify-between gap-1">
              <span className="text-[10px] font-extrabold text-slate-500 flex items-center gap-1 truncate">
                <Activity className="w-2.5 h-2.5 text-blue-500 flex-shrink-0" />
                {idx.name}
              </span>
              <span className="text-[9px] font-mono text-slate-400 flex-shrink-0">
                {idx.trade_date?.slice(5)}
              </span>
            </div>
            <div className="flex items-baseline justify-between gap-1">
              <span className="text-[15px] font-extrabold text-slate-900 font-mono tracking-tight leading-none">
                {idx.price.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}
              </span>
              <span
                className={`text-[11px] font-extrabold font-mono leading-none ${
                  up ? 'text-red-600' : 'text-green-600'
                }`}
              >
                {up ? '+' : ''}
                {idx.pct_change?.toFixed(2) ?? '--'}%
              </span>
            </div>
            <div className="flex items-center justify-between text-[9px] leading-none">
              <span className={`font-mono font-bold ${up ? 'text-red-500' : 'text-green-500'}`}>
                {up ? '+' : ''}
                {idx.change?.toFixed(2) ?? '--'}
              </span>
              <span className="flex items-center gap-1.5">
                {/* 量比：大盘是否放量（SOX 无 volume 数据时为 null，不渲染） */}
                {idx.rvol !== null && idx.rvol !== undefined && (
                  <span
                    className={`font-mono font-extrabold ${
                      idx.rvol >= 1.2
                        ? 'text-orange-600'
                        : idx.rvol < 0.8
                        ? 'text-slate-400'
                        : 'text-slate-500'
                    }`}
                    title="量比 = 当日成交量 / 前 20 日均量"
                  >
                    量比 {idx.rvol.toFixed(2)}
                  </span>
                )}
                {vol && <span className="text-slate-400 font-mono">{vol}</span>}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
};
