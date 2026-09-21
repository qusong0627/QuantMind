/**
 * 候选信号探索页（模拟交易 · 候选信号页签，2026-09-17）
 *
 * 复刻 quant-Trader 个股终端左栏：检索 + 条件筛选（概念/推理模型 + 大盘 MA20 日历「补推理」）
 * + 排名列表（排名｜股票｜15日走势｜板块·分｜行业·分｜市值·分｜趋势｜得分｜仓位｜信号）。
 * 右侧：选中股终端入口 + 信号分布卡（原 SignalsSection）。
 * 纪律：模型刷新（日历补推理）完成后按用户指定跳转「系统健康」（onModelRefreshed 上抛给宿主页签容器）。
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { message } from 'antd';
import { CandlestickChart } from 'lucide-react';
import { StockSidebar, toPrefix } from '../../../features/stock-terminal/components/StockSidebar';
import type { ListFilters } from '../../../features/stock-terminal/components/StockFilterPanel';
import type { StockListItem } from '../../../features/stock-terminal/types';
import { StockTerminalWindow, prefetchTerminal } from '../../../features/market-analysis-shared/components/StockTerminalWindow';
import { StatTile } from '../../../features/desk/components/cardKit';
import { getDeskToday } from '../../../features/desk/services/deskService';
import { EvalScoreBadge } from '../../../components/shared/EvalScoreBadge';
import { ComplianceStrip } from '../../../components/shared/compliance/ComplianceChrome';
import SignalLookbackCard from '../../../features/stock-terminal/components/SignalLookbackCard';
import type { SignalsBlock } from '../../../features/desk/types';
import { BarChart3, TrendingDown } from 'lucide-react';
import { researchService } from '../../../services/researchService';
import { stockTerminalService as cnTerminalService } from '../../../features/stock-terminal/services/stockTerminalService';
import type { PositionKind } from '../../../features/stock-terminal/components/StockSidebar';
import { websocketService, MessageType } from '../../../services/websocketService';

/** A 股交易时段（含集合竞价尾段与尾盘），用于自选池实时刷新节流 */
function isCnTradingHours(d = new Date()): boolean {
  const day = d.getDay();
  if (day === 0 || day === 6) return false;
  const m = d.getHours() * 60 + d.getMinutes();
  return (m >= 9 * 60 + 25 && m <= 11 * 60 + 35) || (m >= 12 * 60 + 55 && m <= 15 * 60 + 5);
}

/** stream 服务实时行情（topic stock.{code}，2s 推一次；与持仓监控同管道） */
interface LiveQuote {
  stock_code?: string;
  data?: { price?: number | null };
}

/**
 * 内容相等判定 —— 60s 轮询每次都新建 Set/Map，若原样塞进 state，引用变更会顺着
 * props → buildParams → fetchList 一路传下去，让侧栏列表**每分钟被重置回第 1 页**
 * （用户正翻到第 5 页看候选，轮询一到就被拽回来）。内容没变就复用旧引用。
 */
function sameSymbols(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

function samePositions(a: Map<string, PositionKind>, b: Map<string, PositionKind>): boolean {
  if (a.size !== b.size) return false;
  for (const [k, v] of a) if (b.get(k) !== v) return false;
  return true;
}

interface SignalsExplorerPageProps {
  /** 模型刷新（日历补推理）完成 → 宿主页跳「系统健康」 */
  onModelRefreshed?: () => void;
}

const SignalsExplorerPage: React.FC<SignalsExplorerPageProps> = ({ onModelRefreshed }) => {
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState('');
  const [windowSymbol, setWindowSymbol] = useState<string | null>(null);
  // 默认只到「截面靠前」（页签语义 = 候选信号）：这是一道**位置**筛选，不是方向默认——
  // 筛的是模型当日输出里排在前面的一批，筛完该怎么处理由用户自己定。
  // 展示层走 signalPositionLabel，所以筛选条上显示的是「位置 靠前」而不是「BUY」。
  const [filters, setFilters] = useState<ListFilters>({ side: 'BUY' });
  const [models, setModels] = useState<{ model_id: string; display_name?: string }[]>([]);
  const [listTotal, setListTotal] = useState(0);
  const [fullTotal, setFullTotal] = useState(0);
  const [signalDate, setSignalDate] = useState<string | undefined>(undefined);
  const [watchlist, setWatchlist] = useState<Set<string>>(new Set());          // 手工自选（星标真身）
  const [watchFilterSymbols, setWatchFilterSymbols] = useState<Set<string>>(new Set()); // 手工 ∪ 持仓（只看自选口径）
  const [positions, setPositions] = useState<Map<string, PositionKind>>(new Map());      // 持仓徽章
  const [livePrices, setLivePrices] = useState<Record<string, number>>({});              // prefix -> 实时价
  const [onlyWatchlist, setOnlyWatchlist] = useState(false);
  // 低分股（末位 10 只，全市场按得分升序的尾部）——右侧筛选展示
  const [lowScores, setLowScores] = useState<Array<{ symbol: string; name: string; score: number | null }>>([]);
  // 上面三个集合的渲染期镜像 ref：轮询回调（useCallback 空依赖）需要读「上一次的值」
  // 才能做内容比对，而本项目 tsc 不接受函数式 setter（见 useState-functional-setter-type-bug）。
  const watchlistRef = useRef(watchlist);
  watchlistRef.current = watchlist;
  const positionsRef = useRef(positions);
  positionsRef.current = positions;
  const watchFilterRef = useRef(watchFilterSymbols);
  watchFilterRef.current = watchFilterSymbols;
  // 实时价用「ref 作真源、state 作渲染镜像」：WS 帧快于渲染时也不会丢更新
  const livePricesRef = useRef<Record<string, number>>({});

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

  /**
   * 自选池统一视图（手工 ∪ 模拟持仓 ∪ 实盘持仓 ∪ 正分候选），读时并集不写库：
   * - 星标只用 manual（点击加/移的是手工自选真身，持仓不会因为点星消失）
   * - 「只看自选」用 手工 ∪ 持仓（用户口径：我的持仓就在自选里）
   * - 持仓明细给出行内 模拟/实盘/双 徽章（此前是留好的空插槽）
   */
  const loadUnifiedWatchlist = useCallback(async () => {
    const d = await researchService.getUnifiedWatchlist(300, 200);
    const manual = new Set<string>();
    const pos = new Map<string, PositionKind>();
    const filterSet = new Set<string>();
    for (const it of d.items ?? []) {
      const p = it.symbol;
      const hasSim = !!it.position?.sim;
      const hasReal = !!it.position?.real;
      if (it.sources.includes('manual')) {
        manual.add(p);
        filterSet.add(p);
      }
      if (hasSim || hasReal) {
        pos.set(p, hasSim && hasReal ? 'BOTH' : hasReal ? 'REAL' : 'SIM');
        filterSet.add(p);
      }
    }
    if (!sameSymbols(watchlistRef.current, manual)) setWatchlist(manual);
    if (!samePositions(positionsRef.current, pos)) setPositions(pos);
    if (!sameSymbols(watchFilterRef.current, filterSet)) setWatchFilterSymbols(filterSet);
    return filterSet;
  }, []);

  useEffect(() => {
    let cancelled = false;
    loadUnifiedWatchlist().catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [loadUnifiedWatchlist]);

  // 自选/持仓池实时刷新：交易时段 60s 一次（分数由日频/实时推理落库，价格走下方 WS）
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.hidden) return;
      if (!isCnTradingHours()) return;
      loadUnifiedWatchlist().catch(() => {});
    }, 60_000);
    return () => clearInterval(timer);
  }, [loadUnifiedWatchlist]);

  // 订阅自选/持仓池实时价（topic stock.{code}；与持仓监控同一管道，2s 一推）
  const subscribedRef = React.useRef<string[]>([]);
  useEffect(() => {
    const symbols = [...watchFilterSymbols];
    if (symbols.length === 0) return;
    const toSubscribe = symbols.filter((c) => !subscribedRef.current.includes(c));
    if (toSubscribe.length === 0) return;
    subscribedRef.current = [...subscribedRef.current, ...toSubscribe];
    websocketService.subscribe({ symbols: toSubscribe });
  }, [watchFilterSymbols]);

  useEffect(() => {
    const handler = (data: unknown) => {
      const msg = data as LiveQuote;
      const code = String(msg?.stock_code || '').toUpperCase();
      const price = Number(msg?.data?.price);
      if (!code || !Number.isFinite(price) || price <= 0) return;
      // stock_code 可能是代码或 prefix/suffix，归一到 prefix 键
      const prefix = /^(SH|SZ|BJ)\d{6}$/.test(code)
        ? code
        : toPrefix(code.includes('.') ? code : `${code}.SH`);
      const prev = livePricesRef.current;
      if (prev[prefix] === price) return;
      livePricesRef.current = { ...prev, [prefix]: price };
      setLivePrices(livePricesRef.current);
    };
    websocketService.addMessageHandler('quote' as MessageType, handler);
    return () => {
      websocketService.removeMessageHandler('quote' as MessageType, handler);
    };
  }, []);

  // 退页时退订
  useEffect(() => () => {
    if (subscribedRef.current.length) {
      websocketService.unsubscribe(subscribedRef.current);
      subscribedRef.current = [];
    }
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
    // 整页可滚动：回看卡展开后约 1150px（要露 20 行明细），比视口高。
    // 以前这里是 overflow-hidden + 两栏 flex-1，卡片一展开就把候选列表压到只剩 3 行。
    <div className="h-full overflow-y-auto bg-gray-50/50">
      {/* h-full 而非 min-h-full：min-h-full 下容器高度随内容长，左栏（flex-1）会被撑成
          内容高度（实测 4335px，50 行全铺开、失去自身滚动）。h-full 让两栏高度确定，
          卡片超出时由外层滚动条接管。 */}
      <div className="flex h-full flex-col gap-3 px-3 py-3">
        {/* 上：左栏（列表）+ 右栏（终端入口/低分股/信号分布） */}
        <div className="flex-1 min-h-[420px] flex gap-3">
        {/* 左栏：检索 + 筛选 + 排名列表（quant-Trader 个股终端左栏复刻） */}
        <div className="flex-1 min-w-0 min-h-0 flex">
          <StockSidebar
            selected={selected}
            onSelect={onSelect}
            watchlistSymbols={watchlist}
            watchFilterSymbols={watchFilterSymbols}
            positions={positions}
            livePrices={livePrices}
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

        {/* 给悬浮 Dock 让位。.bottom-dock 是 absolute 覆盖层（z-index 1050，不占布局），
            本页滚动区又不为它留高度，于是滚到底时回看卡的拖动手柄正好落在 Dock 底下
            （实测手柄 935~947、Dock 922~966），elementFromPoint 命中的是 dock-item
            —— 鼠标事件全被接走，真实用户也抓不到这个手柄。

            必须是**占位的实体块**，不能靠给滚动容器加 padding-bottom：
            (a) 挂在 h-full 的内层 wrapper 上时，padding 只是从它内部抠掉一块，
                可滚动区纹丝不动（实测 scrollHeight 前后同为 1772）；
            (b) 挂在滚动容器自己身上同样无效 —— 容器的 end-padding 只能在内容末端
                之前补足，内容已经溢出到更下面时它一点都延伸不出去，还顺手把 wrapper
                压矮 52px（左栏可见行数跟着少一行）。
            高度算式沿用 ResearchPlatformPage 的 --dock-height 约定；max() 兜住无 Dock
            的页面，否则 calc(0px-12px) 是负值，整条声明失效变 0。 */}
        {/* 合规免责横条：放在 Dock 占位块**之上**，否则会被悬浮 Dock 盖住。
            gap-3（12px）+ 占位块 52px + 容器 py-3（12px）= 距底 76px > Dock 胶囊顶沿 64px */}
        <ComplianceStrip className="shrink-0 px-1" />

        <div aria-hidden className="h-[max(12px,calc(var(--dock-height)-12px))] shrink-0" />

        {/* 个股终端浮窗（点「个股终端」打开选中股） */}
        <StockTerminalWindow
          open={!!windowSymbol}
          symbol={windowSymbol}
          market="CN"
          onClose={() => setWindowSymbol(null)}
        />
      </div>
    </div>
  );
};

export default SignalsExplorerPage;
