/**
 * 顶栏个股搜索框（三市场共用）——服务端全市场联想（防抖 250ms）→ 选中弹出个股终端浮窗。
 *
 * 各市场走各自终端服务的 `getStockList`（CN/HK 为 /stock-terminal/list，US 为 /stock-terminal-us/list，
 * 名称字段差异由 US 服务子类翻译）；`onPick` 可选，供宿主页做自己的联动（如 A 股页聚焦资金流详情）。
 */
import React, { useEffect, useRef, useState } from 'react';
import { Input } from 'antd';
import { Search } from 'lucide-react';
import { stockTerminalService as cnTerminalService } from '../../stock-terminal/services/stockTerminalService';
import { stockTerminalService as hkTerminalService } from '../../stock-terminal-hk/services/stockTerminalService';
import { stockTerminalService as usTerminalService } from '../../stock-terminal-us/services/stockTerminalService';
import {
  StockTerminalWindow,
  formatTerminalSymbol,
  prefetchTerminal,
  type TerminalMarket,
} from './StockTerminalWindow';

const SERVICES = {
  CN: cnTerminalService,
  HK: hkTerminalService,
  US: usTerminalService,
} as const;

const DEFAULT_PLACEHOLDER: Record<TerminalMarket, string> = {
  CN: '输入个股代码/名称，弹出个股终端',
  HK: '输入港股代码/名称，弹出个股终端',
  US: '输入美股代码/名称，弹出个股终端',
};

const ACCENTS = {
  purple: {
    input:
      'rounded-xl border border-purple-200/80 bg-slate-50/60 text-xs text-slate-800 placeholder-slate-400 py-1 px-3 shadow-2xs hover:border-purple-300 focus:bg-white focus:ring-2 focus:ring-purple-100 transition-all',
    icon: 'text-purple-400',
    dropdownBorder: 'border-purple-100',
    itemHover: 'hover:bg-purple-50',
  },
  indigo: {
    input:
      'rounded-xl border border-indigo-200/80 bg-white/70 text-xs text-slate-800 placeholder-slate-400 py-1 px-3 shadow-2xs hover:border-indigo-300 focus:bg-white focus:ring-2 focus:ring-indigo-100 transition-all',
    icon: 'text-indigo-400',
    dropdownBorder: 'border-indigo-100',
    itemHover: 'hover:bg-indigo-50',
  },
  blue: {
    input:
      'rounded-xl border border-blue-200/80 bg-white/70 text-xs text-slate-800 placeholder-slate-400 py-1 px-3 shadow-2xs hover:border-blue-300 focus:bg-white focus:ring-2 focus:ring-blue-100 transition-all',
    icon: 'text-blue-400',
    dropdownBorder: 'border-blue-100',
    itemHover: 'hover:bg-blue-50',
  },
} as const;

interface StockTerminalSearchBoxProps {
  market: TerminalMarket;
  placeholder?: string;
  accent?: keyof typeof ACCENTS;
  /** 宽度类（缺省 w-48 sm:w-64） */
  widthClass?: string;
  /** 聚焦空态 / 无结果时展示的提示（如数据包注意事项） */
  hint?: string;
  /** 选中联动（可选）：宿主页可在打开浮窗前做自己的动作 */
  onPick?: (symbol: string, name: string) => void;
}

interface StockHit {
  symbol: string;
  name: string;
}

export const StockTerminalSearchBox: React.FC<StockTerminalSearchBoxProps> = ({
  market,
  placeholder,
  accent = 'purple',
  widthClass = 'w-48 sm:w-64',
  hint,
  onPick,
}) => {
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState<StockHit[]>([]);
  const [windowSymbol, setWindowSymbol] = useState<string | null>(null);
  const prefetched = useRef(false);
  const accentStyle = ACCENTS[accent];
  const service = SERVICES[market];

  useEffect(() => {
    const query = q.trim();
    if (!query) {
      setHits([]);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = setTimeout(async () => {
      try {
        const resp = await service.getStockList({ q: query, page: 1, page_size: 8 });
        const items = (resp.items ?? [])
          .map((it) => ({ symbol: String(it.symbol || ''), name: String(it.name || it.symbol || '') }))
          .filter((it) => it.symbol);
        if (!cancelled) setHits(items);
      } catch {
        if (!cancelled) setHits([]);
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [q, service]);

  const doPrefetch = () => {
    if (prefetched.current) return;
    prefetched.current = true;
    prefetchTerminal(market);
  };

  return (
    <>
      <div className={`relative ${widthClass}`}>
        <Input
          prefix={<Search className={`w-3.5 h-3.5 ${accentStyle.icon} mr-1.5`} />}
          placeholder={placeholder || DEFAULT_PLACEHOLDER[market]}
          value={q}
          title={hint}
          onChange={(e) => {
            setQ(e.target.value);
            setOpen(true);
            doPrefetch();
          }}
          onFocus={() => {
            setOpen(true);
            doPrefetch();
          }}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          className={accentStyle.input}
        />
        {open && (q.trim() || hint) && (
          <div
            className={`absolute left-0 right-0 top-full mt-1.5 bg-white rounded-xl border ${accentStyle.dropdownBorder} shadow-xl z-50 overflow-hidden max-h-80 overflow-y-auto`}
          >
            {q.trim() && searching && (
              <div className="flex items-center gap-2 px-3 py-2 text-xs text-slate-400">
                <span className="w-3 h-3 border-2 border-slate-300 border-t-transparent rounded-full animate-spin" />
                搜索中…
              </div>
            )}
            {q.trim() && !searching && hits.map((item) => (
              <button
                key={item.symbol}
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onPick?.(item.symbol, item.name);
                  setQ('');
                  setOpen(false);
                  setWindowSymbol(item.symbol);
                }}
                className={`w-full flex items-center justify-between gap-2 px-3 py-2 ${accentStyle.itemHover} border-b border-slate-100 last:border-0 text-left`}
              >
                <span className="text-xs font-extrabold text-slate-800 truncate">{item.name}</span>
                <span className="text-[11px] font-mono text-slate-400">{formatTerminalSymbol(market, item.symbol)}</span>
              </button>
            ))}
            {q.trim() && !searching && hits.length === 0 && (
              <div className="px-3 py-2 text-[11px] leading-relaxed text-slate-400">
                {hint || '未找到匹配标的，可尝试代码、名称或拼音首字母'}
              </div>
            )}
            {!q.trim() && hint && (
              <div className="px-3 py-2 text-[11px] leading-relaxed text-slate-400">{hint}</div>
            )}
          </div>
        )}
      </div>

      <StockTerminalWindow
        open={!!windowSymbol}
        symbol={windowSymbol}
        market={market}
        onClose={() => setWindowSymbol(null)}
      />
    </>
  );
};

export default StockTerminalSearchBox;
