/** 恒生四大指数快照卡（对齐 A 股 BroadMarketHeader 的紫色主题卡片风格） */

import React from 'react';
import { Activity } from 'lucide-react';
import type { HkIndexItem } from '../types';
import { PctText } from './shared/ui';

export const HkIndexCards: React.FC<{ indices: HkIndexItem[]; loading?: boolean }> = ({
  indices,
  loading,
}) => {
  if (loading) {
    return (
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-2.5">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="animate-pulse h-20 bg-white/70 rounded-2xl border border-slate-200/70" />
        ))}
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 xl:grid-cols-4 gap-2.5">
      {indices.map((idx) => {
        const up = idx.pct_change >= 0;
        return (
          <div
            key={idx.symbol}
            className="bg-white/90 backdrop-blur-md rounded-2xl px-4 py-3 border border-slate-200/80 shadow-sm flex flex-col gap-1 hover:shadow-md transition-shadow"
          >
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-extrabold text-slate-500 flex items-center gap-1">
                <Activity className="w-3 h-3 text-purple-500" />
                {idx.name}
              </span>
              <span className="text-[10px] font-mono text-slate-400">{idx.trade_date?.slice(5)}</span>
            </div>
            <div className="flex items-baseline gap-2">
              <span className="text-lg font-extrabold text-slate-900 font-mono tracking-tight">
                {idx.price.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}
              </span>
              <PctText value={idx.pct_change} />
            </div>
            <div className="flex items-center justify-between text-[10px]">
              <span className={`font-mono font-extrabold ${up ? 'text-red-600' : 'text-green-600'}`}>
                {up ? '+' : ''}
                {idx.change.toFixed(2)}
              </span>
              <span className="text-slate-400 font-mono">成交 {idx.turnover_yi.toLocaleString('zh-CN')}亿</span>
            </div>
          </div>
        );
      })}
    </div>
  );
};