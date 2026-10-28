import React, { useState } from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';

export interface IndexItem {
  symbol: string;
  name: string;
  price: number;
  change: number;
  pct_change: number;
  turnover: number;
  trend: number[];
  trade_date?: string;
}

interface BroadMarketHeaderProps {
  indices: IndexItem[];
  loading?: boolean;
  onSelectIndex?: (symbol: string) => void;
  /** 紧凑版：放进「大盘全景看板」页签后压缩纵向占用 */
  compact?: boolean;
}

export const BroadMarketHeader: React.FC<BroadMarketHeaderProps> = ({
  indices,
  loading = false,
  onSelectIndex,
  compact = false,
}) => {
  const [selectedSymbol, setSelectedSymbol] = useState<string>('000001.SH');

  const handleCardClick = (symbol: string) => {
    setSelectedSymbol(symbol);
    if (onSelectIndex) onSelectIndex(symbol);
  };

  return (
    <div className={`grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 w-full ${compact ? 'gap-2' : 'gap-2.5'}`}>
      {indices.map((item) => {
        const price = item.price == null ? '—' : item.price.toFixed(2);
        const pct = item.pct_change == null ? '—' : `${item.pct_change >= 0 ? '+' : ''}${item.pct_change.toFixed(2)}%`;
        const isPositive = (item.pct_change ?? 0) >= 0;
        const isSelected = selectedSymbol === item.symbol;

        return (
          <div
            key={item.symbol}
            onClick={() => handleCardClick(item.symbol)}
            className={`group relative rounded-2xl transition-all duration-300 cursor-pointer border flex flex-col justify-between ${compact ? 'px-3 py-2' : 'px-4 py-2.5'} ${
              isSelected
                ? 'bg-gradient-to-br from-purple-500/10 via-white to-purple-50/40 border-purple-400 shadow-md shadow-purple-500/10 ring-2 ring-purple-400/30 scale-[1.01]'
                : 'bg-white/95 hover:bg-gradient-to-br hover:from-white hover:to-purple-50/20 border-purple-100/80 hover:border-purple-300 shadow-xs hover:shadow-md'
            }`}
          >
            {/* 顶部指数名称 + 股票代码 Badge */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-black text-slate-900 tracking-tight">{item.name}</span>
                  <span className={`w-1.5 h-1.5 rounded-full ${isPositive ? 'bg-red-500' : 'bg-emerald-500'} animate-pulse`} />
                </div>
                <span className="text-[10px] text-purple-700 bg-purple-50/90 px-2 py-0.5 rounded-full font-mono border border-purple-200/60 font-bold shadow-2xs">
                  {item.symbol}
                </span>
              </div>

              {/* 价格与涨跌幅 */}
              <div className="flex items-baseline justify-between gap-1 mb-1">
                <span className={`${compact ? 'text-base' : 'text-lg'} font-black font-mono tracking-tight ${isPositive ? 'text-red-500' : 'text-emerald-600'}`}>
                  {price}
                </span>
                <span className={`text-xs font-extrabold font-mono px-2 py-0.5 rounded-full flex items-center gap-0.5 border shadow-2xs ${
                  isPositive ? 'bg-red-50 text-red-600 border-red-100' : 'bg-emerald-50 text-emerald-600 border-emerald-100'
                }`}>
                  {isPositive ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                  {pct}
                </span>
              </div>

              {/* 成交额 */}
              <div className="flex items-center justify-between text-[11px] text-slate-400 font-mono pt-1 border-t border-slate-100/80">
                <span>成交额:</span>
                <span className="font-bold text-slate-700">¥{item.turnover} 亿</span>
              </div>

            </div>
          </div>
        );
      })}
    </div>
  );
};
