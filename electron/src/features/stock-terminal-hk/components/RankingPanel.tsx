/** 推理排名面板：当前模型最近交易日全市场分数排名列表（推理研究内容） */

import { useEffect, useState } from 'react';
import { Trophy, TrendingUp, TrendingDown } from 'lucide-react';
import { Spin, message } from 'antd';
import { stockTerminalService } from '../services/stockTerminalService';
import { StockListItem } from '../types';

interface Props {
  signalDate?: string;
  onSelectStock: (item: StockListItem) => void;
  onOpenKline: () => void;
}

const PAGE = 50;

export function RankingPanel({ signalDate, onSelectStock, onOpenKline }: Props) {
  const [items, setItems] = useState<StockListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [realSignalDate, setRealSignalDate] = useState<string | undefined>();

  useEffect(() => {
    let c = false;
    setLoading(true);
    stockTerminalService.getStockList({ market: 'ALL', page_size: PAGE })
      .then(resp => { if (!c) { setItems(resp.items); setRealSignalDate(resp.signal_date); } })
      .catch(() => { if (!c) message.error('推理排名加载失败'); })
      .finally(() => { if (!c) setLoading(false); });
    return () => { c = true; };
  }, [signalDate]);

  // 去掉无分数项（排前面的都是真实分数）
  const ranked = items.filter(it => it.fusion != null).slice(0, PAGE);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-100 shrink-0">
        <div className="flex items-center gap-1.5">
          <Trophy className="w-3.5 h-3.5 text-amber-500" />
          <span className="text-xs font-black text-slate-800">推理排名</span>
          <span className="text-[10px] text-slate-400">当前模型 · 最近交易日{realSignalDate || signalDate ? ` ${realSignalDate || signalDate}` : ''}</span>
        </div>
        <span className="text-[10px] text-slate-400">点击行查看 K 线</span>
      </div>
      <Spin spinning={loading}>
        <div className="flex-1 min-h-0 overflow-y-auto">
          {!loading && ranked.length === 0 && (
            <div className="py-8 text-center text-[11px] text-slate-400">暂无推理排名数据</div>
          )}
          {ranked.map((it, i) => {
            const up = (it.pct_change ?? 0) >= 0;
            return (
              <button
                key={it.symbol}
                onClick={() => { onSelectStock(it); onOpenKline(); }}
                className="w-full grid grid-cols-[28px_1fr_58px_52px_58px] gap-1 items-center px-2 py-1.5 rounded-lg text-left transition-colors hover:bg-slate-50 border-b border-slate-50"
              >
                <span className={`text-[11px] font-black font-mono ${i < 3 ? 'text-amber-500' : 'text-slate-400'}`}>{i + 1}</span>
                <span className="flex flex-col min-w-0">
                  <span className="text-xs font-bold text-slate-700 truncate">{it.name}</span>
                  <span className="text-[9px] text-slate-400 font-mono">{it.symbol}</span>
                </span>
                <span className="text-right text-xs font-mono font-bold text-blue-600">{(it.fusion ?? 0) * 100 > 0 ? '+' : ''}{((it.fusion ?? 0) * 100).toFixed(2)}</span>
                <span className="text-right text-[11px] font-mono font-bold text-slate-700">{it.close?.toFixed(2) ?? '--'}</span>
                <span className={`text-right text-[11px] font-mono font-bold flex items-center justify-end gap-0.5 ${up ? 'text-rose-500' : 'text-emerald-500'}`}>
                  {up ? <TrendingUp className="w-2.5 h-2.5" /> : <TrendingDown className="w-2.5 h-2.5" />}
                  {it.pct_change != null ? `${up ? '+' : ''}${it.pct_change.toFixed(2)}%` : '--'}
                </span>
              </button>
            );
          })}
        </div>
      </Spin>
    </div>
  );
}
