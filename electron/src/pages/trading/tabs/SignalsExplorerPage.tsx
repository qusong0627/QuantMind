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
import { StockTerminalWindow, prefetchTerminal } from '../../../features/market-analysis-shared/components/StockTerminalWindow';
import { StatTile } from '../../../features/desk/components/cardKit';
import { getDeskToday } from '../../../features/desk/services/deskService';
import { EvalScoreBadge } from '../../../components/shared/EvalScoreBadge';
import SignalLookbackCard from '../../../features/stock-terminal/components/SignalLookbackCard';
import type { SignalsBlock } from '../../../features/desk/types';
import { BarChart3, TrendingDown } from 'lucide-react';
import { researchService } from '../../../services/researchService';
import { stockTerminalService as cnTerminalService } from '../../../features/stock-terminal/services/stockTerminalService';

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
  // 低分股（末位 10 只，全市场按得分升序的尾部）——右侧筛选展示
  const [lowScores, setLowScores] = useState<Array<{ symbol: string; name: string; score: number | null }>>([]);

  // 预取个股终端代码包：点行弹窗即刻可用（性能优化）
  useEffect(() => {
    prefetchTerminal('CN');
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const first = await cnTerminalService.getStockList({ page: 1, page_size: 100 });
        const total = first.total || 0;
        const lastPage = Math.max(1, Math.ceil(total / 100));
        const resp = lastPage === 1 ? first : await cnTerminalService.getStockList({ page: lastPage, page_size: 100 });
        const items = [...(resp.items || [])]
          .sort((a, b) => (a.fusion ?? 0) - (b.fusion ?? 0))
          .slice(0, 10)
          .map((i) => ({ symbol: i.symbol, name: i.name || i.symbol, score: i.fusion ?? null }));
        if (!cancelled) setLowScores(items);
      } catch {
        /* 低分股为增强位：失败静默 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
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
    <div className="h-full flex flex-col gap-3 p-3 overflow-hidden bg-gray-50/50">
      {/* 上：左栏（列表）+ 右栏（终端入口/低分股/信号分布） */}
      <div className="flex-1 min-h-0 flex gap-3">
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
          onOpen={(item) => {
            setSelected(item.symbol);
            setSelectedName(item.name || '');
            setWindowSymbol(item.symbol);
          }}
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
        {/* 低分股（末位 10）——右侧筛选展示，点击即开个股终端 */}
        <div className="rounded-2xl border border-gray-200 bg-white p-3">
          <div className="flex items-center gap-2 mb-2">
            <TrendingDown className="w-3.5 h-3.5 text-emerald-600 shrink-0" />
            <span className="text-xs font-bold text-slate-700">低分股（末位 10）</span>
            <span className="text-[10px] text-slate-400">得分最弱 · 回避/观察</span>
          </div>
          {lowScores.length === 0 ? (
            <div className="py-4 text-center text-[11px] text-slate-300">加载中…</div>
          ) : (
            <div className="space-y-0.5">
              {lowScores.map((it) => (
                <button
                  key={it.symbol}
                  type="button"
                  onClick={() => {
                    setSelected(it.symbol);
                    setSelectedName(it.name);
                    setWindowSymbol(it.symbol);
                  }}
                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left transition-colors hover:bg-slate-50"
                  title={`${it.name}（${it.symbol}） 得分 ${it.score != null ? it.score.toFixed(4) : '--'}`}
                >
                  <span className="min-w-0 flex-1 truncate text-[11px] font-semibold text-slate-700">{it.name}</span>
                  <span className="shrink-0 font-mono text-[10px] text-slate-400">{it.symbol.split('.')[0]}</span>
                  <span className="w-14 shrink-0 text-right font-mono text-[11px] font-bold text-slate-500">
                    {it.score != null ? it.score.toFixed(3) : '--'}
                  </span>
                </button>
              ))}
            </div>
          )}
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
      </div>

      {/* 下：信号准确率回看（通栏）。side/model/asof 与上方列表联动，
          保证同一只票在两处的当日分数是同一个 run 的数 */}
      <SignalLookbackCard
        className="shrink-0"
        side={filters.side}
        model={filters.model}
        asof={signalDate}
      />

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
