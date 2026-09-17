/**
 * 候选信号探索页（模拟交易 · 候选信号页签，2026-09-17）
 *
 * 复刻 quant-Trader 个股终端左栏：检索 + 条件筛选（概念/推理模型 + 大盘 MA20 日历「补推理」）
 * + 排名列表（排名｜股票｜15日走势｜板块·分｜行业·分｜市值·分｜趋势｜得分｜仓位｜信号）。
 * 右侧：选中股终端入口 + 信号分布卡（原 SignalsSection）。
 * 纪律：模型刷新（日历补推理）完成后按用户指定跳转「系统健康」（onModelRefreshed 上抛给宿主页签容器）。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { message } from 'antd';
import { CandlestickChart } from 'lucide-react';
import { StockSidebar, toPrefix } from '../../../features/stock-terminal/components/StockSidebar';
import type { ListFilters } from '../../../features/stock-terminal/components/StockFilterPanel';
import type { StockListItem } from '../../../features/stock-terminal/types';
import { StockTerminalWindow } from '../../../features/market-analysis-shared/components/StockTerminalWindow';
import { StatTile } from '../../../features/desk/components/cardKit';
import { getDeskToday } from '../../../features/desk/services/deskService';
import { EvalScoreBadge } from '../../../components/shared/EvalScoreBadge';
import type { SignalsBlock } from '../../../features/desk/types';
import { BarChart3 } from 'lucide-react';
import { researchService } from '../../../services/researchService';

interface SignalsExplorerPageProps {
  /** 模型刷新（日历补推理）完成 → 宿主页跳「系统健康」 */
  onModelRefreshed?: () => void;
}

const SignalsExplorerPage: React.FC<SignalsExplorerPageProps> = ({ onModelRefreshed }) => {
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState('');
  const [windowSymbol, setWindowSymbol] = useState<string | null>(null);
  // 默认只买入候选（页签语义 = 候选信号）
  const [filters, setFilters] = useState<ListFilters>({ side: 'BUY' });
  const [models, setModels] = useState<{ model_id: string; display_name?: string }[]>([]);
  const [listTotal, setListTotal] = useState(0);
  const [fullTotal, setFullTotal] = useState(0);
  const [signalDate, setSignalDate] = useState<string | undefined>(undefined);
  const [watchlist, setWatchlist] = useState<Set<string>>(new Set());
  const [onlyWatchlist, setOnlyWatchlist] = useState(false);
  // 右栏「信号分布」概览（全市场 BUY/SELL/HOLD 计数；明细在左侧列表，不重复渲染 Top 列表）
  const [signals, setSignals] = useState<SignalsBlock | null>(null);
  useEffect(() => {
    let cancelled = false;
    getDeskToday({ health: false, plan: false })
      .then((resp) => {
        if (!cancelled) setSignals(resp?.data?.signals || null);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    researchService
      .getWatchlist(300)
      .then((resp) => {
        if (!cancelled) setWatchlist(new Set((resp.items || []).map((i) => i.symbol)));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const onSelect = useCallback((item: StockListItem) => {
    setSelected(item.symbol);
    setSelectedName(item.name || '');
  }, []);

  const toggleWatch = useCallback(
    async (item: StockListItem, watched: boolean) => {
      const prefix = toPrefix(item.symbol);
      const next = new Set(watchlist);
      if (watched) next.delete(prefix);
      else next.add(prefix);
      setWatchlist(next);
      try {
        if (watched) await researchService.removeFromWatchlist(prefix);
        else await researchService.addToWatchlist(prefix, { stockName: item.name });
        message.success(watched ? `已移出自选：${item.name}` : `已加入自选：${item.name}`);
      } catch {
        message.error('自选操作失败，请重试');
      }
    },
    [watchlist],
  );

  const handleTotals = useCallback(
    (total: number) => {
      setListTotal(total);
      // date 只影响基准日、不改变股票集合——不计入筛选激活判断
      const { date: _dateIgnored, ...rest } = filters;
      const hasActive = Object.values(rest).some((v) => v != null && v !== '');
      if (!hasActive) setFullTotal(total);
    },
    [filters],
  );

  return (
    <div className="h-full flex gap-3 p-3 overflow-hidden bg-gray-50/50">
      {/* 左栏：检索 + 筛选 + 排名列表（quant-Trader 个股终端左栏复刻） */}
      <div className="flex-1 min-w-0 min-h-0 flex">
        <StockSidebar
          selected={selected}
          onSelect={onSelect}
          watchlistSymbols={watchlist}
          onToggleWatch={(item, watched) => void toggleWatch(item, watched)}
          onlyWatchlist={onlyWatchlist}
          onOnlyWatchlist={setOnlyWatchlist}
          filters={filters}
          onFiltersChange={setFilters}
          onModels={setModels}
          models={models}
          onTotals={handleTotals}
          onSignalDate={setSignalDate}
          fullTotal={fullTotal}
          onModelRefreshed={onModelRefreshed}
        />
      </div>

      {/* 右栏：选中股终端入口 + 信号分布 */}
      <div className="w-[330px] xl:w-[370px] shrink-0 min-h-0 overflow-y-auto space-y-3 pr-0.5">
        <div className="rounded-2xl border border-gray-200 bg-white p-3 flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-blue-500 to-indigo-500 flex items-center justify-center shrink-0">
            <CandlestickChart className="w-4 h-4 text-white" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[10px] text-slate-400 font-bold">
              当前选中{signalDate ? ` · 信号日 ${signalDate}` : ''}
            </div>
            <div className="text-xs font-bold text-slate-800 truncate">{selectedName || selected || '—'}</div>
          </div>
          <button
            type="button"
            disabled={!selected}
            onClick={() => selected && setWindowSymbol(selected)}
            className="shrink-0 rounded-xl bg-slate-800 px-3 py-1.5 text-[11px] font-bold text-white shadow-sm hover:bg-slate-700 active:scale-95 disabled:opacity-40 transition-all"
          >
            个股终端
          </button>
        </div>
        {/* 信号分布概览（原右侧「候选信号」卡与左侧列表重复，2026-09-17 精简为分布 + 评分徽章） */}
        <div className="rounded-2xl border border-gray-200 bg-white p-3">
          <div className="flex items-center gap-2 mb-2">
            <BarChart3 className="w-3.5 h-3.5 text-blue-500 shrink-0" />
            <span className="text-xs font-bold text-slate-700">信号分布</span>
            {signals?.trade_date && (
              <span className="text-[10px] font-mono text-slate-400">{signals.trade_date}</span>
            )}
            {signals?.trade_date && (
              <span className="ml-auto">
                <EvalScoreBadge objectType="daily_selection" objectId={signals.trade_date} prefix="选股评分" />
              </span>
            )}
          </div>
          <div className="grid grid-cols-3 gap-2">
            <StatTile label="BUY" value={signals?.buy} tone="red" />
            <StatTile label="SELL" value={signals?.sell} tone="green" />
            <StatTile label="HOLD" value={signals?.hold} tone="slate" />
          </div>
          <p className="mt-2 text-[10px] leading-4 text-slate-400">
            全市场信号计数；候选明细即左侧列表（默认「信号 = 买入」），点行选中后可从上方打开个股终端。
          </p>
        </div>
        <p className="px-1 text-[10px] text-slate-400">
          共 {listTotal} 只命中{fullTotal > 0 ? ` / 全市场 ${fullTotal} 只` : ''}；行内星标加自选，点行选中后可在上方打开个股终端浮窗。
        </p>
      </div>

      {/* 个股终端浮窗（点「个股终端」打开选中股） */}
      <StockTerminalWindow
        open={!!windowSymbol}
        symbol={windowSymbol}
        market="CN"
        onClose={() => setWindowSymbol(null)}
      />
    </div>
  );
};

export default SignalsExplorerPage;
