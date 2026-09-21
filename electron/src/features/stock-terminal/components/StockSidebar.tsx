/** 个股终端左侧栏：搜索 + 市场分段 + 看板筛选（页面持有条件）+ 信息丰富的股票列表 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { Search, RefreshCw, Star, ChevronDown, ChevronLeft, ChevronRight, ChevronsUp, ChevronsDown, ShieldCheck, AlertTriangle, Send, X } from 'lucide-react';
import { Checkbox, Input, Spin, message, Dropdown, Segmented } from 'antd';
import { StockListItem, StockListResponse, StockRisk, ExclusionMeta, PushChannel, PushSide } from '../types';
import { EXCLUDE_ON, riskChips, channelText } from '../riskModel';
import { channelOptions, MAX_PICK } from '../pushModel';
import { scoreFreqView, tableFreqView } from '../scoreFreq';
import { stockTerminalService } from '../services/stockTerminalService';
import { ListFilters, bucketScoreRange, StockFilterPanel, BOARD_OPTIONS, CAP_TIER_OPTIONS, TREND_OPTIONS, BUCKET_OPTIONS } from './StockFilterPanel';
import { PushConfirmPanel } from './PushConfirmPanel';
import { EvalScoreBadge } from '../../../components/shared/EvalScoreBadge';
// 展示面口径：signal_side 只译成「靠前/靠后/居中」（见 features/shared/signalVocabulary.ts）
import { signalPositionLabel } from '../../shared/signalVocabulary';

interface Props {
  selected: string | null;
  onSelect: (item: StockListItem) => void;
  watchlistSymbols: Set<string>;   // prefix 格式（SH600519）· **手工自选真身**，只驱动星标
  /** 行内星标点击：加/移自选（watchlistSymbols 只读，页面持有真实状态） */
  onToggleWatch?: (item: StockListItem, watched: boolean) => void;
  /**
   * 「只看自选」的过滤集合（prefix）。缺省回退 `watchlistSymbols`。
   * 统一视图下 = 手工自选 ∪ 全部持仓（用户的持仓就在自选里）；**星标仍只认手工集合**，
   * 否则给持仓票点星会变成「取消持仓」，语义打架。
   */
  watchFilterSymbols?: Set<string>;
  /** 持仓来源映射（prefix -> 模拟/实盘/BOTH，自选列表「持仓」标记，实时推导） */
  positions?: Map<string, PositionKind>;
  /** 实时价覆盖（prefix -> 最新价，WS 推送）。有值时优先于日线 `close` 渲染，并标注「实时」 */
  livePrices?: Record<string, number>;
  onlyWatchlist: boolean;
  onOnlyWatchlist: (v: boolean) => void;
  /** 筛选条件（页面持有，看板面板在左侧列表上方） */
  filters: ListFilters;
  onFiltersChange: (f: ListFilters) => void;
  onModels?: (models: { model_id: string; display_name?: string }[]) => void;
  /** 全部模型列表（页面持有，用于筛选面板下拉选项） */
  models?: { model_id: string; display_name?: string }[];
  /** 列表数量回传（供筛选面板计数） */
  onTotals?: (filtered: number) => void;
  /** 当前列表基准信号日回传（日历高亮 + 面板日期 chip） */
  onSignalDate?: (d?: string) => void;
  /** 全市场总量（筛选面板命中统计） */
  fullTotal?: number;
  /** 模型刷新（日历补推理）完成回调——宿主页据此跳转「系统健康」（2026-09-17 用户指定） */
  onModelRefreshed?: () => void;
  /** 行点击（用户主动点）额外回调——宿主页用于直接弹出个股终端（自动选中不触发） */
  onOpen?: (item: StockListItem) => void;
}

/** 持仓来源：REAL=实盘，SIM=模拟盘，BOTH=两处都持仓 */
export type PositionKind = 'REAL' | 'SIM' | 'BOTH';

const PAGE_SIZE = 100;

function fmtPct(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

function fmtMv(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return '--';
  if (v >= 10000) return `${(v / 10000).toFixed(1)}万亿`;
  return `${v.toFixed(0)}亿`;
}

/** suffix(600519.SH) -> prefix(SH600519)，自选表用 prefix 格式 */
export function toPrefix(symbol: string): string {
  const [code, ex] = symbol.split('.');
  return ex && code ? `${ex}${code}` : symbol;
}

/**
 * 截面位置徽标配色：**单色深浅，不用红绿**。
 *
 * A 股红涨绿跌，红绿一上来就把「靠前」翻译成「该买」——刚用位置词换掉的方向，
 * 会从颜色通道原样回来，而且比文字更难察觉。深浅只表达位置前后，不表达涨跌。
 */
const SIDE_COLOR: Record<string, string> = {
  BUY: 'bg-blue-100 text-blue-700',
  SELL: 'bg-slate-100 text-slate-500',
  HOLD: 'bg-slate-50 text-slate-400',
};

/** 持仓来源徽标样式：模拟=蓝、实盘=紫、双持仓=靛蓝 */
const POSITION_BADGE: Record<PositionKind, { cls: string; label: string; title: string }> = {
  SIM: { cls: 'bg-sky-100 text-sky-700 border-sky-200', label: '模拟', title: '模拟盘持仓' },
  REAL: { cls: 'bg-violet-100 text-violet-700 border-violet-200', label: '实盘', title: '实盘持仓' },
  BOTH: { cls: 'bg-indigo-100 text-indigo-700 border-indigo-200', label: '模拟·实盘', title: '模拟盘+实盘均持仓' },
};

const TREND_COLOR: Record<string, string> = {
  '连续上升': 'text-rose-500',
  '上升': 'text-rose-400',
  '先升后降': 'text-amber-600 font-bold',
  '连续下降': 'text-emerald-600',
  '下降': 'text-emerald-500',
};

/** 板块按市场着色（板块/行业两列共用） */
export const BOARD_TONE: Record<string, string> = {
  '沪市主板': 'bg-rose-50 text-rose-600 border-rose-200',
  '深市主板': 'bg-blue-50 text-blue-600 border-blue-200',
  '科创板': 'bg-violet-50 text-violet-600 border-violet-200',
  '创业板': 'bg-amber-50 text-amber-600 border-amber-200',
  '北交所': 'bg-emerald-50 text-emerald-600 border-emerald-200',
};

export function boardToneOf(board?: string): string {
  return board ? (BOARD_TONE[board] ?? 'bg-slate-50 text-slate-500 border-slate-200') : 'bg-slate-50 text-slate-400 border-slate-200';
}

/** 仓位信号着色：0=灰禁 / 0.1~0.5 淡红 / 0.5~0.8 中红 / 0.8~0.99 深红白字。
 *  A 股涨红跌绿，仓位建议越高越红。 */
export function positionToneOf(v: number | null | undefined): { cls: string; txt: string } {
  if (v == null) return { cls: 'bg-slate-50 text-slate-300 border-slate-100', txt: '--' };
  if (v <= 0) return { cls: 'bg-slate-100 text-slate-400 border-slate-200', txt: '禁' };
  if (v < 0.5) return { cls: 'bg-rose-50 text-rose-500 border-rose-200', txt: `${Math.round(v * 100)}%` };
  if (v < 0.8) return { cls: 'bg-rose-200 text-rose-700 border-rose-300', txt: `${Math.round(v * 100)}%` };
  return { cls: 'bg-rose-600 text-white border-rose-700', txt: `${Math.round(v * 100)}%` };
}

const MARKETS: [string, string][] = [['ALL', '全部'], ['SH', '沪市'], ['SZ', '深市'], ['BJ', '北交']];

/** 行内风险/新闻徽章：判据在 riskModel.riskChips（纯函数，单测盯口径），这里只管画 */
function RiskBadges({ risk }: { risk?: StockRisk | null }): ReactElement | null {
  const chips = riskChips(risk);
  if (!chips.length) return null;
  return (
    <>
      {chips.map(c => (
        <span key={c.key} title={c.title} className={`text-[9px] rounded px-0.5 shrink-0 ${c.cls}`}>
          {c.label}
        </span>
      ))}
    </>
  );
}

export function StockSidebar({ selected, onSelect, watchlistSymbols, watchFilterSymbols, positions = new Map<string, PositionKind>(), livePrices = {}, onlyWatchlist, onOnlyWatchlist, onToggleWatch, filters, onFiltersChange, onModels, models: modelOptions = [], onTotals, onSignalDate, fullTotal = 0, onModelRefreshed, onOpen }: Props) {
  const [market, setMarket] = useState('ALL');
  const [q, setQ] = useState('');
  /** 「只看自选」口径（手工 ∪ 持仓）；星标与推送仍走 watchlistSymbols/manual，两者不混 */
  const watchFilter = watchFilterSymbols ?? watchlistSymbols;
  const [data, setData] = useState<StockListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [optionCounts, setOptionCounts] = useState<Record<string, Record<string, number>>>({});
  const [facets, setFacets] = useState<Record<string, string[]>>({});
  const listRef = useRef<HTMLDivElement>(null);
  const itemsRef = useRef<StockListItem[]>([]);
  const initialAutoSelected = useRef(false);
  // selected 只影响「首次自动选中」逻辑，不触发列表重新请求（否则点股票整表刷新，表格打架）
  const selectedRef = useRef<string | null>(selected);
  selectedRef.current = selected;
  // 列表跳转：切日期/筛选后选中股票可能掉到几千名——按 find_rank 跳到对应页并滚动到该行。
  // jumpKey 防重复跳转（同一日期+股票只跳一次）；pageOffsetRef 记录当前 items 的起始排名偏移。
  const jumpKeyRef = useRef<string>('');
  const pageOffsetRef = useRef(0);

  /**
   * 多选推送（T-FE-09）：勾选的票 + 目标通道 + 面板开关。
   *
   * 存整条 `StockListItem` 而不是只存代码：勾中的票滚出已加载页之后，底部操作条仍要
   * 说得清「你选的是哪几只」（只留代码的话用户只能靠记忆核对）。
   * **勾选不参与任何请求**：它不进 `buildParams`，所以勾一只票不会让整表重拉
   * （与 `selectedRef` 同一条纪律，见上方注释）。
   */
  const [picked, setPicked] = useState<Map<string, StockListItem>>(new Map());
  const [pushChannels, setPushChannels] = useState<PushChannel[]>(['sim']);
  const [pushOpen, setPushOpen] = useState(false);
  /** 由操作条上点的那颗按钮决定（买/卖各一颗），不从列表筛选推导——见操作条注释 */
  const [pushSide, setPushSide] = useState<PushSide>('buy');

  /**
   * 检索模式：搜索框有内容时，让开「候选列表专属」的那几道闸。
   *
   * 信号=买入 与三道风险排除闸都是**页面默认**，用户从没主动勾过；而搜索框的语义是
   * 「找到这只票」，不是「在候选里找这只票」。实测：搜 600036（招商银行）在默认参数下
   * `total=0` —— 它当日信号是 SELL、且命中通道 A 的年内新闻黑名单，两条都不是用户输入
   * 造成的，界面却统一显示成「搜不到」。用户自己设的筛选（行业/板块/分数/模型…）保持不动，
   * 那些是他明确表达过的意图，见下方 `activeNarrowing` 的空结果提示。
   */
  const searching = q.trim().length > 0;

  /**
   * 用户**主动设置**且会缩小股票集合的条件 —— 搜索空结果时用来回答「是没这只票，
   * 还是被你自己的条件挡住了」。
   * 排除闸不算（那是页面默认），date 只换基准日不改集合，side 在检索模式下已让开。
   */
  const NARROWING_KEYS = [
    ['board', '板块'], ['capTier', '市值'], ['bucket', '分数档'], ['trend', '趋势'],
    ['industry', '行业'], ['concept', '概念'], ['indexCode', '宽基'], ['model', '推理模型'],
    ['scoreMin', '分数下限'], ['tagId', '标签'],
  ] as const;

  const activeNarrowing = NARROWING_KEYS.filter(([k]) => {
    const v = (filters as Record<string, unknown>)[k];
    return v != null && v !== '';
  });

  /** 清掉上面那些条件（保留 date 与排除闸开关状态）后重新检索 */
  const clearNarrowing = useCallback(() => {
    const next: ListFilters = { ...filters };
    for (const [k] of NARROWING_KEYS) delete next[k];
    onFiltersChange(next);
  }, [filters, onFiltersChange]); // eslint-disable-line react-hooks/exhaustive-deps

  /** 组装 /list 请求参数（首页附带 with_counts / find_symbol） */
  const buildParams = useCallback((page: number, withCounts: boolean) => {
    const range = bucketScoreRange(filters.bucket);
    return {
      market, q: q || undefined, page, page_size: PAGE_SIZE,
      date: filters.date,
      score_min: range.min ?? filters.scoreMin,
      score_max: range.max,
      model: filters.model,
      industry: filters.industry,
      concept: filters.concept,
      board: filters.board,
      cap_tier: filters.capTier,
      trend: filters.trend,
      tag: filters.tagId,
      index_code: filters.indexCode,
      // 检索模式与自选视图都让开「信号」闸：页面的 side=BUY 默认是**候选列表**的语义，
      // 而「我的自选」问的是「我池子里有什么」—— 一只 SELL 信号的持仓若被这条默认筛掉，
      // 用户视角就是自己的票凭空消失。侧栏信号列照常逐行标注方向，信息不藏。
      side: searching || onlyWatchlist ? undefined : filters.side,
      // 风险排除闸**显式传布尔**（后端三个开关默认 false，是为了服务检索框/自选股；
      // 候选列表是「要排除的调用方」，默认全开——见 StockFilterPanel.EXCLUDE_ON）。
      // 检索模式下强制放行：用户搜一只票是为了看它，不是为了被名单静默吞掉。
      // 只看自选同样强制放行 —— 后端排除闸跑在 `symbols=` **之前**（stock_terminal.py 里
      // 名单/新闻过滤在前、symbols 过滤在后），闸开着时用户自己的 ST / 名单 / 新闻命中持仓
      // 会从「我的自选」里静默消失：那是他自己的票，也是最该看见的。
      exclude_st: !searching && !onlyWatchlist && EXCLUDE_ON(filters.excludeSt),
      exclude_risk_list: !searching && !onlyWatchlist && EXCLUDE_ON(filters.excludeRiskList),
      exclude_news_risk: !searching && !onlyWatchlist && EXCLUDE_ON(filters.excludeNewsRisk),
      // 只看自选：把全量自选传给后端过滤（保留分数序），否则前端只过滤已加载页导致列表不全
      symbols: onlyWatchlist && watchFilter.size ? [...watchFilter].join(',') : undefined,
      ...(withCounts ? { with_counts: true } : {}),
      ...(withCounts && selectedRef.current ? { find_symbol: selectedRef.current } : {}),
    };
  }, [market, q, filters, onlyWatchlist, watchFilter, searching]);

  const fetchList = useCallback(async (page = 1, append = false) => {
    setLoading(true);
    try {
      const resp = await stockTerminalService.getStockList(buildParams(page, !append));
      const models = resp.models ?? [];
      if (models.length) onModels?.(models);
      if (!append) {
        pageOffsetRef.current = (page - 1) * PAGE_SIZE;
        setOptionCounts(resp.option_counts ?? {});
        setFacets(resp.facets ?? {});
        onSignalDate?.(resp.signal_date);
      }
      itemsRef.current = append ? [...itemsRef.current, ...resp.items] : resp.items;
      setData({ ...resp, items: itemsRef.current });
      onTotals?.(resp.total);
      // 默认选中排名第一（仅首次加载且未选中）
      if (!append && !initialAutoSelected.current && !selectedRef.current && itemsRef.current.length) {
        initialAutoSelected.current = true;
        onSelect(itemsRef.current[0]);
      }
      // 列表自动跳转：选中股票不在当前页时，按 find_rank 跳到其所在页并滚动定位
      const sel = selectedRef.current;
      if (!append && sel && resp.find_rank != null && !resp.items.some(it => it.symbol === sel)) {
        const targetPage = Math.ceil(resp.find_rank / PAGE_SIZE);
        const jumpKey = `${resp.signal_date ?? ''}:${sel}:${targetPage}`;
        if (jumpKeyRef.current !== jumpKey) {
          jumpKeyRef.current = jumpKey;
          const pageResp = await stockTerminalService.getStockList(buildParams(targetPage, false));
          if (pageResp.items.some(it => it.symbol === sel)) {
            pageOffsetRef.current = (targetPage - 1) * PAGE_SIZE;
            itemsRef.current = pageResp.items;
            setData({ ...resp, items: pageResp.items, page: targetPage });
            requestAnimationFrame(() => {
              const row = listRef.current?.querySelector<HTMLElement>(`[data-symbol="${sel}"]`);
              row?.scrollIntoView({ block: 'center' });
            });
          }
        }
      }
    } catch {
      if (!append) message.error('股票列表加载失败');
    } finally {
      setLoading(false);
    }
  }, [buildParams, onModels, onTotals, onSelect, onSignalDate]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const t = setTimeout(() => fetchList(1, false), q ? 300 : 0);
    return () => clearTimeout(t);
  }, [fetchList, q]); // eslint-disable-line react-hooks/exhaustive-deps

  /**
   * 换一批候选（改筛选/换市场/换基准日/切检索词）就清空勾选。
   *
   * 不清的话会出现「勾了 5 只 → 换筛选 → 推送」把一个已经不在当前视角里的组合发出去：
   * 勾中的票里可能有已被新条件排除、甚至已删号的，而确认面板之外没人会再看一遍。
   * 清空是明面上的行为（操作条消失），残留才是隐形的。
   */
  const queryKey = useMemo(
    () => JSON.stringify([market, q, filters, onlyWatchlist]),
    [market, q, filters, onlyWatchlist],
  );
  useEffect(() => {
    setPicked(new Map());
  }, [queryKey]);

  const handleScroll = useCallback(() => {
    const el = listRef.current;
    if (!el || loading || !data) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 40) {
      if (data.items.length < data.total) fetchList(data.page + 1, true);
    }
  }, [loading, data, fetchList]);

  // 首页/末页跳转：L2 分数普遍偏低时，点首页立刻看到当天排名第1，不用反复刷新找。
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  const goFirst = useCallback(() => {
    if (!data || data.page === 1 || loading) return;
    fetchList(1, false);
    listRef.current?.scrollTo({ top: 0 });
  }, [data, loading, fetchList]);
  const goLast = useCallback(() => {
    if (!data || data.page >= totalPages || loading) return;
    fetchList(totalPages, false);
    listRef.current?.scrollTo({ top: 0 });
  }, [data, totalPages, loading, fetchList]);
  // 上一页/下一页：逐页翻（区别于首页/末页的跳转）
  const goPrev = useCallback(() => {
    if (!data || data.page <= 1 || loading) return;
    fetchList(data.page - 1, false);
    listRef.current?.scrollTo({ top: 0 });
  }, [data, loading, fetchList]);
  const goNext = useCallback(() => {
    if (!data || data.page >= totalPages || loading) return;
    fetchList(data.page + 1, false);
    listRef.current?.scrollTo({ top: 0 });
  }, [data, totalPages, loading, fetchList]);

  // 页码输入跳转：输入框内容与当前页同步；回车/失焦时提交，越界自动夹到 [1, totalPages]
  const [pageInput, setPageInput] = useState('');
  useEffect(() => { setPageInput(String(data?.page ?? 1)); }, [data?.page]);
  const jumpToPage = useCallback(() => {
    if (!data) return;
    const n = parseInt(pageInput, 10);
    if (!Number.isFinite(n)) { setPageInput(String(data.page)); return; }
    const target = Math.min(Math.max(1, n), totalPages);
    setPageInput(String(target));
    if (target !== data.page) {
      fetchList(target, false);
      listRef.current?.scrollTo({ top: 0 });
    }
  }, [data, pageInput, totalPages, fetchList]);

  const visibleItems = useMemo(
    () => onlyWatchlist ? (data?.items ?? []).filter(it => watchFilter.has(toPrefix(it.symbol))) : (data?.items ?? []),
    [data, onlyWatchlist, watchFilter],
  );

  /** 勾/取消一只。单批上限与服务端 `MAX_BATCH_SYMBOLS` 同口径 —— 超了当场说，不留给 400。 */
  const togglePick = useCallback((item: StockListItem) => {
    const next = new Map(picked);
    if (next.has(item.symbol)) {
      next.delete(item.symbol);
    } else {
      if (next.size >= MAX_PICK) {
        message.warning(`单批最多 ${MAX_PICK} 只（已选 ${next.size} 只），请分两批推送`);
        return;
      }
      next.set(item.symbol, item);
    }
    setPicked(next);
  }, [picked]);

  /** 全选/取消全选**当前已加载的**可见行（不做「全市场全选」——那会把没看过的票也算进去） */
  const pickAllVisible = useCallback((checked: boolean) => {
    if (!checked) {
      setPicked(new Map());
      return;
    }
    const next = new Map<string, StockListItem>();
    for (const it of visibleItems) {
      if (next.size >= MAX_PICK) { message.warning(`单批最多 ${MAX_PICK} 只，已按顺序取前 ${MAX_PICK} 只`); break; }
      next.set(it.symbol, it);
    }
    setPicked(next);
  }, [visibleItems]);

  const allVisiblePicked = visibleItems.length > 0 && visibleItems.every(it => picked.has(it.symbol));

  // 推送完成后**关掉面板才**清空勾选：面板开着的时候 `symbols` 是它的入参，
  // 当场清空会让正在看的回执失去上下文（那一刻 `symbols` 变空数组）。
  const pushDoneRef = useRef(false);
  const closePush = useCallback(() => {
    setPushOpen(false);
    if (pushDoneRef.current) {
      pushDoneRef.current = false;
      setPicked(new Map());
    }
  }, []);

  // 单一 grid 贯穿表头+每行，所有列严格对齐。
  // 列：勾选 | 排名 | 股票 | 板块·分 | 行业·分 | 市值·分 | 趋势 | 得分 | 仓位 | 信号
  // （2026-09-17 去走势迷你线列：每行懒加载 K 线极易卡顿，牺牲此列换流畅度）
  const GRID = 'grid grid-cols-[16px_24px_1.4fr_56px_70px_50px_42px_56px_38px_30px] gap-1';

  /** 表头兜底名：走全站唯一的展示面译法（见 features/shared/signalVocabulary.ts）。
   *  正常情况下标签来自后端 facets，这里只在 facets 缺该值时兜底。 */
  const SIDE_LABEL: Record<string, string> = {
    BUY: signalPositionLabel('BUY'),
    SELL: signalPositionLabel('SELL'),
    HOLD: signalPositionLabel('HOLD'),
  };
  /** 得分档表头短名（列宽有限）。**只写区间，不评价区间**：
   *  原来的「黄金 / 谨慎 / 做空」是在替用户判断哪一段值得下手，而这列本来就是
   *  模型原始分的分区，用户看的是「落在哪一段」而不是「该不该动手」。 */
  const BUCKET_SHORT: Record<string, string> = {
    golden: '0.10-0.12', optional: '0.12-0.15', caution: '0.15-0.20', extreme: '≥0.20',
    neg_extreme: '≤-0.20', neg_short: '≤-0.15', pos: '≥0', neg: '<0',
  };

  /** 表头列筛选下拉（板块/行业/市值/趋势/得分/信号），长菜单限高滚动避免盖住整个列表。
   *  `locked` = 该列在当前模式下被放行（值不参与请求）：此时**不给下拉**，
   *  改为灰字 + 说明 title —— 能点却无效果比不能点更糟。 */
  const headerDropdown = (items: { value: string; label: string }[], current: string | undefined, onPick: (v?: string) => void, placeholder: string, locked = false) => {
    if (locked) {
      return (
        <button
          disabled
          title={`${placeholder}筛选项在「自选」/检索模式下已放行（列表含全部方向）——关掉「自选」并清空搜索框后可筛选`}
          className="flex items-center justify-center gap-0.5 px-0.5 rounded text-slate-300 cursor-not-allowed"
        >
          <span className="truncate">{current ? (SIDE_LABEL[current] ?? current) : placeholder}</span>
        </button>
      );
    }
    return (
      <Dropdown
        trigger={['click']}
        placement="bottom"
        menu={{
          items: [
            { key: '__all', label: `全部${placeholder}` },
            ...items.map(x => ({ key: x.value, label: x.label })),
          ],
          selectable: true,
          selectedKeys: current ? [current] : ['__all'],
          onClick: ({ key }) => onPick(key === '__all' ? undefined : key),
          style: { maxHeight: 260, overflowY: 'auto' },
        }}
      >
        <button className={`flex items-center justify-center gap-0.5 px-0.5 rounded transition-colors ${current ? 'text-blue-600 font-black' : 'hover:text-blue-500'}`}>
          <span className="truncate">{current ? (SIDE_LABEL[current] ?? current) : placeholder}</span>
          <ChevronDown className="w-2.5 h-2.5 shrink-0 opacity-60" />
        </button>
      </Dropdown>
    );
  };

  /** 列筛选值集合（优先后端 facets，回退全量选项） */
  const fac = (key: string, fallback: { value: string; label: string }[]): { value: string; label: string }[] => {
    const f = facets[key];
    return f && f.length ? f.map(v => ({ value: v, label: v })) : fallback;
  };

  return (
    <div className="w-[42rem] flex-1 min-h-0 flex flex-col bg-white/80 backdrop-blur-xl rounded-3xl border border-white/90 shadow-xs p-4 overflow-hidden">
      {/* 搜索框 + 市场分段 + 自选（自选放北交后面，省空间） */}
      <div className="flex items-center gap-1.5 mb-2">
        <div className="flex-1 flex items-center bg-white border border-slate-200 hover:border-blue-400 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-100 rounded-xl px-3 py-1 transition-all shadow-2xs">
          <Search className="w-3.5 h-3.5 text-blue-500 shrink-0" />
          <Input
            variant="borderless"
            placeholder="输入代码 / 名称"
            value={q}
            onChange={e => setQ(e.target.value)}
            allowClear
            className="p-0 font-mono font-bold text-sm text-blue-600"
            style={{ padding: 0 }}
          />
        </div>
        <div className="grid grid-cols-5 gap-0.5 p-0.5 bg-slate-100/70 rounded-lg shrink-0">
          {MARKETS.map(([v, label]) => (
            <button
              key={v}
              onClick={() => setMarket(v)}
              className={`px-2.5 py-1 rounded-md text-[11px] font-bold transition-all ${
                market === v ? 'bg-white text-blue-600 shadow-2xs' : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {label}
            </button>
          ))}
          <button
            onClick={() => onOnlyWatchlist(!onlyWatchlist)}
            title="只看自选"
            className={`px-2.5 py-1 rounded-md text-[11px] font-bold transition-all flex items-center justify-center gap-0.5 ${
              onlyWatchlist ? 'bg-white text-amber-600 shadow-2xs' : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            <Star className="w-2.5 h-2.5" /> 自选
          </button>
        </div>
      </div>

      {/* 检索模式声明：搜索时让开了候选列表的默认闸门，必须**说出来**。
          不说的话，用户会以为「列表里这些票就是候选」——而检索结果里恰恰包含
          非买入信号与被名单/新闻拦下的票。让开是行为，声明才是证据。 */}
      {searching && !onlyWatchlist && (
        <div className="flex items-center gap-1.5 shrink-0 mb-1.5 rounded-lg border border-blue-100 bg-blue-50/70 px-2 py-1">
          <Search className="w-3 h-3 text-blue-500 shrink-0" />
          <span className="text-[10px] font-bold text-blue-700 shrink-0">检索模式</span>
          <span className="text-[9px] text-slate-600 leading-tight">
            已暂时放行「信号=买入」与三道风险排除闸 —— 你搜的票若信号不是买入、或被名单/新闻拦下，
            这里仍会列出来并逐行标注原因；关掉搜索框即恢复候选视图。
          </span>
        </div>
      )}

      {/* 自选视图声明：与检索模式同理，让开闸门必须**说出来**。
          后端三道风险闸跑在 `symbols=` 之前 —— 闸开着时，自己持仓里的 ST / 名单命中票
          会从「只看自选」里静默消失（用户视角：我的票丢了）。故自选视图下一律放行，
          并在此声明；行内风险徽章照常逐行标注，不藏信息。 */}
      {onlyWatchlist && (
        <div className="flex items-center gap-1.5 shrink-0 mb-1.5 rounded-lg border border-amber-200 bg-amber-50/70 px-2 py-1">
          <Star className="w-3 h-3 text-amber-500 shrink-0" />
          <span className="text-[10px] font-bold text-amber-700 shrink-0">自选视图</span>
          <span className="text-[9px] text-slate-600 leading-tight">
            已放行三道风险排除闸与「信号」筛选{searching ? '（并含检索模式的放行）' : ''} —— 你的持仓 / 自选即使 ST、在名单上、
            有新闻利空，或当日信号不是买入，也会列出来并逐行标注；关掉「自选」即恢复候选视图的默认闸门。
          </span>
        </div>
      )}

      {/* 筛选面板：columnOnly 只留 模型+概念 两列（其余维度在列表表头筛选）；日历补推理后刷新列表 */}
      {/* 筛选行（统一网格：概念 1fr | 日历 | 推理模型 1fr | 页码 auto 右贴边——比例协调，无死区） */}
      <StockFilterPanel
        filters={filters}
        onChange={onFiltersChange}
        total={data?.total ?? 0}
        fullTotal={fullTotal}
        models={modelOptions}
        compact
        columnOnly
        optionCounts={optionCounts}
        showMarketCalendar
        onInferred={() => {
          fetchList(1, false);
          onModelRefreshed?.();
        }}
        extraRight={
          data && data.total > 0 ? (
            <div className="flex items-center gap-0.5 font-mono text-[10px] text-slate-500 whitespace-nowrap">
              <button onClick={goFirst} disabled={data.page <= 1 || loading} title="首页（排名第1）"
                className="flex items-center rounded-md border border-slate-200 bg-slate-50 px-1 py-0.5 text-slate-600 hover:bg-blue-50 hover:text-blue-600 disabled:opacity-30 disabled:cursor-not-allowed">
                <ChevronsUp className="w-3 h-3" />
              </button>
              <button onClick={goPrev} disabled={data.page <= 1 || loading} title="上一页"
                className="flex items-center rounded-md border border-slate-200 bg-slate-50 px-1 py-0.5 text-slate-600 hover:bg-blue-50 hover:text-blue-600 disabled:opacity-30 disabled:cursor-not-allowed">
                <ChevronLeft className="w-3 h-3" />
              </button>
              <input
                value={pageInput}
                onChange={e => setPageInput(e.target.value.replace(/[^\d]/g, ''))}
                onKeyDown={e => { if (e.key === 'Enter') { e.currentTarget.blur(); jumpToPage(); } }}
                onBlur={jumpToPage}
                inputMode="numeric"
                aria-label="页码"
                title="输入页码后回车跳转"
                className="w-8 rounded border border-slate-200 px-1 py-0.5 text-center text-[10px] text-slate-700 bg-white focus:border-blue-400 focus:outline-none"
              />
              <span className="px-0.5">/ {totalPages} 页 · {data.total} 只</span>
              <button onClick={goNext} disabled={data.page >= totalPages || loading} title="下一页"
                className="flex items-center rounded-md border border-slate-200 bg-slate-50 px-1 py-0.5 text-slate-600 hover:bg-blue-50 hover:text-blue-600 disabled:opacity-30 disabled:cursor-not-allowed">
                <ChevronRight className="w-3 h-3" />
              </button>
              <button onClick={goLast} disabled={data.page >= totalPages || loading} title="末页（排名最后）"
                className="flex items-center rounded-md border border-slate-200 bg-slate-50 px-1 py-0.5 text-slate-600 hover:bg-blue-50 hover:text-blue-600 disabled:opacity-30 disabled:cursor-not-allowed">
                <ChevronsDown className="w-3 h-3" />
              </button>
            </div>
          ) : null
        }
      />

      {/* 风险闸收据：默认排除了什么、各排掉多少、名单是哪天的 —— 摆在列表头上，
          让「列表怎么少了」有一个当场可查的答案（只减不说的列表会被当成数据丢了）。

          **只列分项、不给合计**：三个通道的计数会互相重叠（同一只票既在名单又有新闻时
          各记一笔，实测 204+1808+65=2077 而真实剔除 2038），加出来摆上去就是假证据。
          计数落后端**实际**剔掉的只数，通道关掉时为 0 —— 所以关掉时显示「放行」而不是
          「0」，否则读起来像「这个通道一只都没命中」。 */}
      {(() => {
        const em = data?.exclusion_meta;
        if (!em) return null;
        // 兜底走「未导入 / 不可用」而不是空对象：字段缺失时**不能**渲染成
        // 「已按名单过滤、一只都没命中」——那是把「不知道」说成了「没问题」
        const listMeta: ExclusionMeta['list'] = em.list ?? { imported: false, reason: '响应缺 exclusion_meta.list' };
        const newsMeta: ExclusionMeta['news'] = em.news ?? { available: false, reason: '响应缺 exclusion_meta.news' };
        const stOn = EXCLUDE_ON(filters.excludeSt);
        const riskOn = EXCLUDE_ON(filters.excludeRiskList);
        const newsOn = EXCLUDE_ON(filters.excludeNewsRisk);
        // 自选视图下 buildParams 强制放行三道闸：按钮点了不生效，必须**锁住**——
        // 可点却无效果比不存在更糟（用户以为自己开了闸，实际列表一只没排）。
        const gateLocked = onlyWatchlist;
        const channel = (
          label: string, on: boolean, n: number, tip: string, isBad: boolean, onToggle: () => void,
        ) => (
          <button
            key={label}
            type="button"
            disabled={gateLocked}
            onClick={onToggle}
            title={gateLocked
              ? '自选视图期间强制放行（避免你自己的持仓被名单静默吞掉）；关掉「自选」后可调'
              : tip}
            className={`shrink-0 rounded border px-1 py-0.5 text-[9px] font-bold transition-colors ${
              gateLocked
                ? 'border-slate-200 bg-slate-50 text-slate-300 cursor-not-allowed'
                : isBad
                  ? 'border-rose-200 bg-rose-50 text-rose-600 hover:bg-rose-100'
                  : on
                    ? 'border-amber-200 bg-white text-amber-700 hover:bg-amber-100'
                    : 'border-slate-200 bg-slate-100 text-slate-400 hover:bg-white'
            }`}
          >
            {label} {gateLocked ? '放行' : isBad ? '不可用' : channelText(on, n)}
          </button>
        );
        const asofShort = (listMeta.asof || '').slice(5);   // 2026-09-18 → 09-18
        return (
          <div className="flex items-center gap-1 flex-wrap shrink-0 mt-0.5 rounded-lg border border-amber-100 bg-amber-50/70 px-1.5 py-1">
            <ShieldCheck className="w-3 h-3 text-amber-500 shrink-0" />
            <span className="text-[10px] font-black text-amber-700 shrink-0">风险闸</span>
            <span className="text-[9px] text-slate-500 shrink-0">{onlyWatchlist ? '放行中' : '已排除'}</span>
            {channel('ST', stOn, em.excluded?.st ?? 0,
              'ST / *ST：QuantDB instrument_detail.IsSTGP 快照，只在实盘窗口内有真值（历史日不套用，避免前视偏差）',
              false, () => onFiltersChange({ ...filters, excludeSt: !stOn }))}
            {channel('名单', riskOn, em.excluded?.risk_list ?? 0,
              listMeta.imported === false
                ? `名单未导入：${listMeta.reason ?? '文件不在盘'}（当前一只都没排掉）`
                : `用户不买入名单 ${em.risk_list_size ?? 0} 只（基准 ${listMeta.asof || '未知'}，${listMeta.stale_days ?? 0} 天前）· 点击放行`,
              listMeta.imported === false, () => onFiltersChange({ ...filters, excludeRiskList: !riskOn }))}
            {channel('新闻', newsOn, em.excluded?.news_risk ?? 0,
              newsMeta.available === false
                ? `新闻标签不可用：${newsMeta.reason ?? '汇总任务未产出'}（当前一只都没排掉）`
                : `近 20 天监管/司法类新闻利空 ${em.news_risk_size ?? 0} 只（窗口至 ${newsMeta.window_end || '未知'}）· 点击放行`,
              newsMeta.available === false, () => onFiltersChange({ ...filters, excludeNewsRisk: !newsOn }))}
            <span className="ml-auto flex items-center gap-1 shrink-0">
              {listMeta.stale ? (
                <span className="flex items-center gap-0.5 rounded bg-rose-50 px-1 py-0.5 text-[9px] font-bold text-rose-600"
                  title={`名单基准 ${listMeta.asof || '未知'}，已 ${listMeta.stale_days ?? '?'} 天未更新 —— 请重跑 backend/scripts/import_exclusion_list.py`}>
                  <AlertTriangle className="w-2.5 h-2.5" />名单 {listMeta.stale_days ?? '?'} 天未更新
                </span>
              ) : (
                <span className="text-[9px] font-mono text-slate-400"
                  title={`名单基准日 ${listMeta.asof || '未知'}（生成于 ${listMeta.generated_at || '未知'}），来源逐项：${
                    Object.entries(listMeta.sources ?? {}).map(([k, v]) => `${v.label || k} ${v.count ?? 0}${v.blocking === false ? '(只提示)' : ''}`).join('、') || '—'}`}>
                  名单 {asofShort || '—'}
                </span>
              )}
            </span>
          </div>
        );
      })()}

      {/* 当前信号日 chip：随日历切换显示该日期（琥珀底色），点击回到最新；后备注当天各维度头部均分基准 */}
      {(() => {
        // 切了历史日优先显示 filters.date；否则显示最近信号日 signal_date
        const shownDate = filters.date || data?.signal_date;
        if (!shownDate) return null;
        const isHistorical = !!filters.date;
        // 当天头部基准：取排名第1股票的 board/industry/cap top10 均分作参照线
        const top = visibleItems[0];
        const bench = top && (top.board_top10_avg != null || top.industry_top10_avg != null || top.cap_top10_avg != null);
        return (
          <div className="flex items-center gap-1.5 shrink-0 flex-wrap">
            <span className="text-[9px] font-bold text-slate-400">信号日</span>
            <button
              onClick={() => onFiltersChange({ ...filters, date: undefined })}
              title={isHistorical ? '当前列表基准日，点击回到最新' : '当前列表基准信号日'}
              className={`shrink-0 text-[10px] font-mono font-bold rounded-md px-1.5 py-0.5 border transition-colors ${
                isHistorical
                  ? 'bg-amber-50 text-amber-700 border-amber-200 hover:bg-amber-100'
                  : 'bg-slate-50 text-slate-500 border-slate-200 hover:bg-slate-100'
              }`}
            >
              {shownDate}
              {isHistorical && <span className="ml-0.5 opacity-70">✕</span>}
            </button>
            {/* 分频标注：本页分数是盘中实时分还是隔夜日频分。计数来自后端实测
                （开关开着但行情未到达时后端不发布伪实时分，计数仍为 0），
                不是「开关是否打开」的猜测 */}
            {(() => {
              const fv = tableFreqView(data?.realtime_rows, data?.signal_date);
              return (
                <span
                  title={fv.title}
                  className={`shrink-0 rounded-md border px-1 py-0.5 text-[9px] font-bold ${fv.cls}`}
                >
                  {fv.label}
                </span>
              );
            })()}
            {bench && (
              <span className="text-[9px] text-slate-400 font-mono" title="当天各维度头部前10均分基准（排名第1股票所在维度）">
                头部基准
                {top!.board_top10_avg != null && <span className="text-slate-500"> 板{top!.board_top10_avg >= 0 ? '+' : ''}{top!.board_top10_avg.toFixed(3)}</span>}
                {top!.industry_top10_avg != null && <span className="text-slate-500"> 行{top!.industry_top10_avg >= 0 ? '+' : ''}{top!.industry_top10_avg.toFixed(3)}</span>}
                {top!.cap_top10_avg != null && <span className="text-slate-500"> 市{top!.cap_top10_avg >= 0 ? '+' : ''}{top!.cap_top10_avg.toFixed(3)}</span>}
              </span>
            )}
            {/* 选股评分徽章（空位利用；与信号分布卡同源） */}
            <span className="ml-auto">
              {data?.signal_date && (
                <EvalScoreBadge objectType="daily_selection" objectId={data.signal_date} prefix="选股评分" />
              )}
            </span>
          </div>
        );
      })()}
      {/* 列表头：单行 10 列，与每行严格对齐；点击表头筛选 */}
      <div className={`${GRID} px-1 pb-1 pt-2 text-[10px] font-bold text-slate-400 border-b border-slate-100 shrink-0 items-center`}>
        {/* 勾选表头 = 全选**当前已加载的**可见行（不做全市场全选：没看过的票不该被勾上） */}
        <span className="flex justify-center" title="全选 / 取消全选当前已加载的行">
          <Checkbox
            checked={allVisiblePicked}
            indeterminate={picked.size > 0 && !allVisiblePicked}
            disabled={visibleItems.length === 0}
            onChange={e => pickAllVisible(e.target.checked)}
            aria-label="全选当前列表"
          />
        </span>
        <span className="text-center">排名</span>
        <span>股票</span>
        <span className="text-center">{headerDropdown(fac('board', BOARD_OPTIONS.map(b => ({ value: b, label: b }))), filters.board, v => onFiltersChange({ ...filters, board: v }), '板块')}</span>
        <span className="text-center">{headerDropdown(fac('industry', []), filters.industry, v => onFiltersChange({ ...filters, industry: v }), '行业')}</span>
        <span className="text-center">{headerDropdown(fac('cap_tier', CAP_TIER_OPTIONS), filters.capTier, v => onFiltersChange({ ...filters, capTier: v }), '市值')}</span>
        <span className="text-center">{headerDropdown(fac('trend', TREND_OPTIONS), filters.trend, v => onFiltersChange({ ...filters, trend: v }), '趋势')}</span>
        <span className="text-right">{headerDropdown(fac('bucket', BUCKET_OPTIONS), filters.bucket, v => onFiltersChange({ ...filters, bucket: v, scoreMin: undefined }),
          filters.bucket ? (BUCKET_SHORT[filters.bucket] ?? '得分') : '得分')}</span>
        <span className="text-center" title="仓位系数：0=模型不给出仓位（低于行业头部或大盘空仓），0.1~0.99=参考仓位系数（半凯利）。它是模型输出，不是给你的下单建议。">仓位</span>
        <span className="text-center">{headerDropdown(fac('side', [
          { value: 'BUY', label: signalPositionLabel('BUY') },
          { value: 'SELL', label: signalPositionLabel('SELL') },
          { value: 'HOLD', label: signalPositionLabel('HOLD') },
        ]), filters.side, v => onFiltersChange({ ...filters, side: v }), '位置', searching || onlyWatchlist)}</span>
      </div>

      {/* 股票列表 */}
      <div ref={listRef} onScroll={handleScroll} className="flex-1 min-h-0 overflow-x-auto overflow-y-auto relative">
        {loading && !data && (
          <div className="absolute inset-x-0 top-20 flex justify-center">
            <Spin size="small" />
          </div>
        )}
        {visibleItems.map((it, i) => {
            const isSel = it.symbol === selected;
            const isPicked = picked.has(it.symbol);
            const prefix = toPrefix(it.symbol);
            // 实时价覆盖（WS `stock.{code}`，2s 一帧）：有帧时显示盘中价、涨跌幅相对
            // 最近收盘价重算；无帧回退日线 close/pct_change。两种情况各自如实呈现，
            // 不把日线收盘价标成实时，也不拿实时价冒充收盘价。
            const live = livePrices[prefix];
            const hasLive = live != null && Number.isFinite(live) && live > 0;
            const price = hasLive ? live : it.close;
            const pct = hasLive && it.close != null && it.close > 0
              ? ((live - it.close) / it.close) * 100
              : it.pct_change;
            const up = (pct ?? 0) >= 0;
            const rank = pageOffsetRef.current + i + 1;   // 跳页后显示真实名次
            const rankMedal = rank <= 3 ? ['🥇', '🥈', '🥉'][rank - 1] : String(rank);
            return (
              <button
                key={it.symbol}
                data-symbol={it.symbol}
                onClick={() => { onSelect(it); onOpen?.(it); }}
                className={`w-full ${GRID} items-center px-1.5 py-1 rounded-lg text-left transition-colors ${
                  isSel ? 'bg-blue-50 border border-blue-200' : 'hover:bg-slate-50 border border-transparent'
                }`}
              >
                {/* 勾选格：整行是 <button>，故用嵌套 role="checkbox" + stopPropagation
                    （与下面星标同一范式）——换成真 <input> 嵌在按钮里既非法也点不准 */}
                <span
                  role="checkbox"
                  aria-checked={isPicked}
                  aria-label={`${it.symbol} 加入推送`}
                  tabIndex={-1}
                  title={isPicked ? '取消勾选' : '勾选（可多选后一键推送）'}
                  onClick={(e) => { e.stopPropagation(); togglePick(it); }}
                  className="flex justify-center items-center cursor-pointer py-1"
                >
                  <span className={`w-3 h-3 rounded border flex items-center justify-center text-[8px] leading-none ${
                    isPicked ? 'bg-blue-600 border-blue-600 text-white' : 'border-slate-300 hover:border-blue-400'
                  }`}>{isPicked ? '✓' : ''}</span>
                </span>
                <span className={`text-center text-[10px] font-mono font-bold ${rank <= 3 ? 'text-sm leading-none' : 'text-slate-400'}`}>{rankMedal}</span>
                {/* 股票单元格：主行(名称|涨幅) + 副行(代码|价格·市值)，单列内 flex-col */}
                <span className="flex flex-col min-w-0 gap-0.5">
                  <span className="flex items-center justify-between gap-1">
                    <span className="text-xs font-bold text-slate-700 truncate flex items-center gap-0.5 min-w-0">
                      {(() => {
                        const watched = watchlistSymbols.has(prefix);
                        return (
                          <span
                            role="button"
                            tabIndex={-1}
                            title={watched ? '移出自选' : '加入自选'}
                            onClick={(e) => { e.stopPropagation(); onToggleWatch?.(it, watched); }}
                            className="flex items-center shrink-0 cursor-pointer rounded hover:bg-amber-50 p-0.5 -m-0.5"
                          >
                            <Star className={`w-2.5 h-2.5 transition-colors ${watched ? 'text-amber-400 fill-amber-400' : 'text-slate-300 hover:text-amber-400'}`} />
                          </span>
                        );
                      })()}
                      {(() => {
                        const kind = positions.get(prefix);
                        if (!kind) return null;
                        const badge = POSITION_BADGE[kind];
                        return <span title={badge.title} className={`text-[8px] font-bold rounded px-0.5 shrink-0 border ${badge.cls}`}>{badge.label}</span>;
                      })()}
                      {it.is_st && <span className="text-[9px] bg-rose-50 text-rose-500 rounded px-0.5 shrink-0">ST</span>}
                      <RiskBadges risk={it.risk} />
                      <span className="truncate">{it.name}</span>
                    </span>
                    <span
                      className={`text-[10px] font-mono shrink-0 ${up ? 'text-rose-500' : 'text-emerald-500'}`}
                      title={hasLive ? `实时涨跌幅（相对最近收盘 ${it.close?.toFixed(2) ?? '--'}）` : undefined}
                    >
                      {fmtPct(pct)}{hasLive && <span className="ml-0.5 text-[8px] text-sky-500 font-bold">实时</span>}
                    </span>
                  </span>
                  <span className="flex items-center justify-between gap-1">
                    <span className="text-[9px] text-slate-400 font-mono truncate">{it.symbol}</span>
                    <span className="text-[9px] font-mono shrink-0" title={hasLive ? '盘中实时价' : '日线收盘价'}>
                      <span className={hasLive ? 'text-sky-600 font-bold' : 'text-slate-500'}>{price?.toFixed(2) ?? '--'}</span>
                      {' · '}{fmtMv(it.total_mv)}
                    </span>
                  </span>
                </span>
                {/* 板块 + 当天头部 top10 均分 */}
                <span className="flex flex-col items-center min-w-0 gap-0" title={`${it.board ?? '--'} · top10 ${it.board_top10_avg != null ? (it.board_top10_avg >= 0 ? '+' : '') + it.board_top10_avg.toFixed(3) : '--'}`}>
                  <span className={`inline-block text-[8px] font-bold rounded px-0.5 border truncate max-w-full ${boardToneOf(it.board)}`}>{it.board?.replace('市主板', '主板') ?? '--'}</span>
                  <span className="text-[7px] font-mono text-slate-400 truncate">{it.board_top10_avg != null ? `${it.board_top10_avg >= 0 ? '+' : ''}${it.board_top10_avg.toFixed(3)}` : ''}</span>
                </span>
                {/* 行业 + 当天头部 top10 均分 */}
                <span className="flex flex-col items-center min-w-0 gap-0" title={`${it.industry ?? '--'} · top10 ${it.industry_top10_avg != null ? (it.industry_top10_avg >= 0 ? '+' : '') + it.industry_top10_avg.toFixed(3) : '--'}`}>
                  <span className="text-[9px] text-slate-600 truncate max-w-full">{it.industry ?? '--'}</span>
                  <span className="text-[7px] font-mono text-slate-400 truncate">{it.industry_top10_avg != null ? `${it.industry_top10_avg >= 0 ? '+' : ''}${it.industry_top10_avg.toFixed(3)}` : ''}</span>
                </span>
                {/* 市值档 + 当天头部 top10 均分 */}
                <span className="flex flex-col items-center min-w-0 gap-0" title={`${it.cap_tier || '--'} · top10 ${it.cap_top10_avg != null ? (it.cap_top10_avg >= 0 ? '+' : '') + it.cap_top10_avg.toFixed(3) : '--'}`}>
                  <span className="text-[9px] text-slate-600 shrink-0">{it.cap_tier || '--'}</span>
                  <span className="text-[7px] font-mono text-slate-400 truncate">{it.cap_top10_avg != null ? `${it.cap_top10_avg >= 0 ? '+' : ''}${it.cap_top10_avg.toFixed(3)}` : ''}</span>
                </span>
                {/* 趋势 */}
                <span className={`text-center text-[9px] truncate ${TREND_COLOR[it.trend ?? ''] ?? 'text-slate-400'}`}>{it.trend ?? '-'}</span>
                {/* 得分（盘中实时分带「实时」徽章；日频不逐行标，由头部统一说明） */}
                <span className="flex items-center justify-end gap-0.5 min-w-0">
                  {(() => {
                    const fv = scoreFreqView(it.freq, it.signal_date);
                    if (!fv || fv.freq !== 'realtime') return null;
                    return (
                      <span title={fv.title} className={`shrink-0 rounded border px-0.5 text-[8px] font-bold ${fv.cls}`}>
                        {fv.label}
                      </span>
                    );
                  })()}
                  <span className={`text-right text-[11px] font-mono font-bold ${(it.fusion ?? 0) >= 0 ? 'text-blue-600' : 'text-slate-400'}`}>
                    {it.fusion != null ? `+${(it.fusion).toFixed(3)}`.replace('+-', '-') : '--'}
                  </span>
                </span>
                {/* 仓位信号 */}
                <span className="text-center">
                  {(() => {
                    const ps = it.position_score;
                    const tone = positionToneOf(ps);
                    const pct = it.pct_industry;
                    const empty = it.market_empty;
                    // 措辞一律归到「模型给出的系数」：数值照旧（下游撮合要用），
                    // 但不写「建议投入」——那是替用户决定投多少
                    const tip = ps == null
                      ? '该日无仓位信号（未推理或缺失基准）'
                      : ps <= 0
                        ? (empty ? '模型不给出仓位（大盘空仓）' : (pct != null && pct < 0.8 ? `模型不给出仓位（行业百分位 ${(pct * 100).toFixed(0)}% < 80%）` : '模型不给出仓位'))
                        : `参考仓位系数 ${Math.round(ps * 100)}%（半凯利）· 行业百分位 ${pct != null ? (pct * 100).toFixed(0) + '%' : '--'}`;
                    return (
                      <span className={`inline-block text-[9px] font-bold rounded px-0.5 py-0.5 border ${tone.cls}`} title={tip}>
                        {tone.txt}
                      </span>
                    );
                  })()}
                </span>
                {/* 截面位置：这一格原来直接印枚举（BUY/SELL），读着就是「买入/卖出」。
                    走全站唯一译法换成位置词；HOLD 不写出来，留空格子更好扫。 */}
                <span className="text-center">
                  <span className={`text-[9px] rounded px-1 py-0.5 font-bold ${SIDE_COLOR[it.side ?? 'HOLD'] ?? SIDE_COLOR.HOLD}`}>
                    {(it.side ?? 'HOLD') === 'HOLD' ? '-' : signalPositionLabel(it.side)}
                  </span>
                </span>
              </button>
            );
          })}
          {loading && data && (
            <div className="flex items-center justify-center py-2 text-[10px] text-slate-400 gap-1">
              <RefreshCw className="w-3 h-3 animate-spin" /> 加载更多…
            </div>
          )}
          {/* 空结果必须分清「没有这只票」和「被条件挡住了」——两者都渲染成一句
              「无匹配股票」时，用户只会以为自己搜错了代码，然后反复重搜。 */}
          {!loading && visibleItems.length === 0 && (
            searching && activeNarrowing.length > 0 ? (
              <div className="text-center py-6 px-3">
                <div className="text-[11px] text-slate-500">
                  没有命中「{q.trim()}」—— 但当前还有 {activeNarrowing.length} 个筛选条件在生效
                </div>
                <div className="mt-1 text-[10px] text-slate-400">
                  {activeNarrowing.map(([, label]) => label).join(' · ')}
                  {onlyWatchlist ? ' · 只看自选' : ''}
                </div>
                <button
                  type="button"
                  onClick={clearNarrowing}
                  className="mt-2 rounded-lg border border-blue-200 bg-blue-50 px-2.5 py-1 text-[11px] font-bold text-blue-600 hover:bg-blue-100 transition-colors"
                >
                  清除这些条件，全市场重新检索
                </button>
              </div>
            ) : (
              <div className="text-center py-8 text-[11px] text-slate-400">
                {searching ? `全市场没有匹配「${q.trim()}」的股票代码或名称` : '无匹配股票'}
              </div>
            )
          )}
      </div>

      {/* 多选操作条：勾了才浮出。通道摆在这里而不是藏进弹窗——用户点推送之前就该知道
          这一批是「只动模拟盘」还是「连真单一起发」。 */}
      {picked.size > 0 && (
        <div className="shrink-0 mt-2 flex items-center gap-2 rounded-2xl border border-blue-200 bg-blue-50/80 px-2.5 py-1.5">
          <span className="text-[11px] font-bold text-blue-700 shrink-0">已选 {picked.size} 只</span>
          <span className="text-[10px] text-slate-500 truncate min-w-0 flex-1" title={[...picked.values()].map(x => `${x.symbol} ${x.name}`).join('\n')}>
            {[...picked.values()].slice(0, 3).map(x => x.name || x.symbol).join('、')}
            {picked.size > 3 ? ` 等 ${picked.size} 只` : ''}
          </span>
          <Segmented
            size="small"
            value={pushChannels.includes('real') ? 'real' : 'sim'}
            onChange={v => setPushChannels(v === 'real' ? ['sim', 'real'] : ['sim'])}
            options={channelOptions().map(o => ({ value: o.value, label: o.label, title: o.hint }))}
          />
          <button
            type="button"
            onClick={() => setPicked(new Map())}
            title="清空勾选"
            className="shrink-0 flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-bold text-slate-500 hover:bg-slate-50"
          >
            <X className="w-3 h-3" /> 清空
          </button>
          {/* 买卖**各一个按钮**，不跟随列表的「信号」筛选：检索模式下那个筛选根本不生效
              （列表请求不带 side、控件也置灰），从前用 `filters.side === 'SELL' ? sell : buy`
              推导，搜到一只票直接点推送会**悄悄发成买单**——用户看不见自己在买。
              买红卖绿沿用 A 股口径（同 ReplayReportPage）。

              这里说得直白（买入/卖出）是**故意的**：本栏挂在交易台里，用户点下去要选股数、
              要确认，属于执行面，含糊化会让人下错单。同理去掉「一键」二字——它暗示
              「不必细看」，而下一屏的确认面板才是这道操作真正的把关处。
              展示面（研究页、评分卡、信号点下钻）一律只说「靠前/靠后」，见
              `features/shared/signalVocabulary.ts`。 */}
          <button
            type="button"
            onClick={() => { setPushSide('buy'); setPushOpen(true); }}
            className="shrink-0 flex items-center gap-1 rounded-lg bg-red-600 px-3 py-1 text-[11px] font-bold text-white shadow-sm hover:bg-red-700"
          >
            <Send className="w-3 h-3" /> 买入
          </button>
          <button
            type="button"
            onClick={() => { setPushSide('sell'); setPushOpen(true); }}
            className="shrink-0 flex items-center gap-1 rounded-lg bg-emerald-600 px-3 py-1 text-[11px] font-bold text-white shadow-sm hover:bg-emerald-700"
          >
            <Send className="w-3 h-3" /> 卖出
          </button>
        </div>
      )}

      <PushConfirmPanel
        open={pushOpen}
        symbols={[...picked.keys()]}
        side={pushSide}
        channels={pushChannels}
        onChannelsChange={setPushChannels}
        onDone={() => { pushDoneRef.current = true; }}
        onClose={closePush}
      />
    </div>
  );
}
