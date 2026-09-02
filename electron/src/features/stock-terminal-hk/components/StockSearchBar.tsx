/** 顶部股票搜索框：输入联想（不预加载全量），选中即展示 */
import { useEffect, useRef, useState } from 'react';
import { Search, Star, X, Clock3, TrendingUp } from 'lucide-react';
import { Spin } from 'antd';
import { stockTerminalService } from '../services/stockTerminalService';
import { StockListItem } from '../types';
import { toPrefix } from './StockSidebar';

interface Props {
  onSelect: (item: StockListItem) => void;
  watchlistSymbols: Set<string>;
  placeholder?: string;
}

const HISTORY_KEY = 'stock-terminal-search-history';

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

export function StockSearchBar({ onSelect, watchlistSymbols, placeholder = '搜索港股代码 / 名称，如 0700 或 腾讯控股' }: Props) {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<StockListItem[]>([]);
  const [history, setHistory] = useState<string[]>(() => loadHistory());
  const [highlight, setHighlight] = useState(0);
  const wrapRef = useRef<HTMLDivElement>(null);

  // 点击外部收起
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  // 联想：q>=1 时请求，debounce 300ms
  useEffect(() => {
    if (!open) return;
    const trimmed = q.trim();
    if (trimmed.length < 1) {
      setItems([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const resp = await stockTerminalService.getStockList({ q: trimmed, page: 1, page_size: 10 });
        setItems(resp.items ?? []);
      } catch {
        setItems([]);
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [q, open]);

  const handleSelect = (it: StockListItem) => {
    const key = `${it.symbol}|${it.name}`;
    const next = [key, ...history.filter((h) => h !== key)].slice(0, 5);
    setHistory(next);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
    setOpen(false);
    onSelect(it);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!open) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setHighlight(Math.min(highlight + 1, Math.max(0, items.length - 1)));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setHighlight(Math.max(0, highlight - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (items[highlight]) handleSelect(items[highlight]);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  const hasQuery = q.trim().length >= 1;

  return (
    <div ref={wrapRef} className="relative w-full max-w-[560px] mx-auto">
      <div className="relative flex items-center">
        <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400 pointer-events-none" />
        <input
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setHighlight(0);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          className="w-full h-9 pl-10 pr-10 rounded-full border border-slate-200 bg-white text-[13px] placeholder:text-slate-400 shadow-sm focus:outline-none focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
        />
        {q ? (
          <button
            onClick={() => {
              setQ('');
              setItems([]);
              setHighlight(0);
            }}
            className="absolute right-2 top-1/2 -translate-y-1/2 w-7 h-7 rounded-full flex items-center justify-center text-slate-400 hover:bg-slate-100 hover:text-slate-600"
          >
            <X className="w-4 h-4" />
          </button>
        ) : null}
      </div>

      {/* 下拉 */}
      {open && (
        <div className="absolute left-0 right-0 top-[44px] bg-white rounded-2xl border border-slate-200 shadow-lg overflow-hidden z-20">
          {/* 搜索结果 */}
          {hasQuery ? (
            <div className="max-h-[320px] overflow-y-auto custom-scrollbar">
              {loading ? (
                <div className="flex items-center justify-center py-6 gap-2 text-xs text-slate-400">
                  <Spin size="small" /> 搜索中…
                </div>
              ) : items.length ? (
                items.map((it, idx) => {
                  const watched = watchlistSymbols.has(toPrefix(it.symbol));
                  const up = (it.pct_change ?? 0) >= 0;
                  return (
                    <button
                      key={it.symbol}
                      onClick={() => handleSelect(it)}
                      onMouseEnter={() => setHighlight(idx)}
                      className={`w-full flex items-center gap-3 px-4 py-2.5 text-left hover:bg-slate-50 ${idx === highlight ? 'bg-indigo-50' : ''}`}
                    >
                      <span className="flex-1 min-w-0">
                        <span className="flex items-center gap-1.5">
                          <span className="text-[13px] font-bold text-slate-800 truncate">{it.name}</span>
                          <span className="text-[11px] font-mono text-slate-400">{it.symbol}</span>
                          {watched && <Star className="w-3 h-3 text-amber-400 fill-amber-400 shrink-0" />}
                        </span>
                        <span className="text-[11px] text-slate-400 truncate">
                          {it.board ?? ''} {it.industry ? `· ${it.industry}` : ''} {it.total_mv ? `· ${it.total_mv.toFixed(0)}亿` : ''}
                        </span>
                      </span>
                      <span className="shrink-0 text-right">
                        <span className={`block text-[12px] font-mono font-bold ${up ? 'text-rose-500' : 'text-emerald-500'}`}>
                          {it.close != null ? it.close.toFixed(2) : '--'}
                        </span>
                        <span className={`block text-[11px] font-mono ${up ? 'text-rose-500' : 'text-emerald-500'}`}>
                          {it.pct_change != null ? `${it.pct_change >= 0 ? '+' : ''}${it.pct_change.toFixed(2)}%` : '--'}
                        </span>
                      </span>
                    </button>
                  );
                })
              ) : (
                <div className="py-8 text-center text-xs text-slate-400">无匹配结果，试试完整代码或名称</div>
              )}
            </div>
          ) : (
            <div className="p-3">
              {history.length > 0 && (
                <div className="mb-3">
                  <div className="flex items-center gap-1.5 px-1 mb-1.5">
                    <Clock3 className="w-3 h-3 text-slate-400" />
                    <span className="text-[11px] font-bold text-slate-500">最近搜索</span>
                    <button
                      onClick={() => {
                        setHistory([]);
                        localStorage.removeItem(HISTORY_KEY);
                      }}
                      className="ml-auto text-[10px] text-slate-400 hover:text-slate-600"
                    >
                      清空
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {history.map((h) => {
                      const [sym, name] = h.split('|');
                      return (
                        <button
                          key={h}
                          onClick={async () => {
                            try {
                              const resp = await stockTerminalService.getStockList({ q: sym, page: 1, page_size: 10 });
                              const hit = resp.items?.find((x) => x.symbol === sym) ?? resp.items?.[0];
                              if (hit) handleSelect(hit);
                              else handleSelect({ symbol: sym, name: name || sym } as StockListItem);
                            } catch {
                              handleSelect({ symbol: sym, name: name || sym } as StockListItem);
                            }
                          }}
                          className="px-2.5 py-1 rounded-full bg-slate-100 hover:bg-slate-200 text-[11px] text-slate-600"
                        >
                          {name || sym} <span className="text-slate-400 font-mono">{sym}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
              <div className="flex items-center gap-1.5 px-1 mb-1.5">
                <TrendingUp className="w-3 h-3 text-slate-400" />
                <span className="text-[11px] font-bold text-slate-500">输入关键词开始搜索</span>
              </div>
              <div className="px-1 text-[11px] text-slate-400 leading-relaxed">
                支持代码（如 00700 / 0700.HK / 腾讯）、名称；不输入时不加载全量列表。
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
