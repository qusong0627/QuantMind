/** 个股终端左侧栏：搜索 + 市场分段 + 看板筛选（页面持有条件）+ 信息丰富的股票列表 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Search, RefreshCw, Star, ChevronDown, ChevronsUp, ChevronsDown } from 'lucide-react';
import { Input, Spin, message, Dropdown } from 'antd';
import { StockListItem, StockListResponse } from '../types';
import { stockTerminalService } from '../services/stockTerminalService';
import { ListFilters, bucketScoreRange, StockFilterPanel, BOARD_OPTIONS, CAP_TIER_OPTIONS, TREND_OPTIONS, BUCKET_OPTIONS } from './StockFilterPanel';
import { Sparkline } from './Sparkline';

interface Props {
  selected: string | null;
  onSelect: (item: StockListItem) => void;
  watchlistSymbols: Set<string>;   // prefix 格式（SH600519）
  /** 行内星标点击：加/移自选（watchlistSymbols 只读，页面持有真实状态） */
  onToggleWatch?: (item: StockListItem, watched: boolean) => void;
  /** 持仓来源映射（prefix -> 模拟/实盘/BOTH，自选列表「持仓」标记，实时推导） */
  positions?: Map<string, PositionKind>;
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

const SIDE_COLOR: Record<string, string> = {
  BUY: 'bg-rose-50 text-rose-600',
  SELL: 'bg-emerald-50 text-emerald-600',
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

export function StockSidebar({ selected, onSelect, watchlistSymbols, positions = new Map<string, PositionKind>(), onlyWatchlist, onOnlyWatchlist, onToggleWatch, filters, onFiltersChange, onModels, models: modelOptions = [], onTotals, onSignalDate, fullTotal = 0 }: Props) {
  const [market, setMarket] = useState('ALL');
  const [q, setQ] = useState('');
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
      side: filters.side,
      exclude_st: filters.excludeSt || undefined,
      // 只看自选：把全量自选传给后端过滤（保留分数序），否则前端只过滤已加载页导致列表不全
      symbols: onlyWatchlist && watchlistSymbols.size ? [...watchlistSymbols].join(',') : undefined,
      ...(withCounts ? { with_counts: true } : {}),
      ...(withCounts && selectedRef.current ? { find_symbol: selectedRef.current } : {}),
    };
  }, [market, q, filters, onlyWatchlist, watchlistSymbols]);

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

  const visibleItems = useMemo(
    () => onlyWatchlist ? (data?.items ?? []).filter(it => watchlistSymbols.has(toPrefix(it.symbol))) : (data?.items ?? []),
    [data, onlyWatchlist, watchlistSymbols],
  );

  // 单一 grid 贯穿表头+每行，所有列严格对齐。
  // 列：排名 | 股票 | 走势(微缩折线) | 板块·分 | 行业·分 | 市值·分 | 趋势 | 得分 | 仓位 | 信号
  const GRID = 'grid grid-cols-[24px_1.4fr_48px_56px_70px_50px_42px_56px_38px_30px] gap-1';

  const SIDE_LABEL: Record<string, string> = { BUY: '买入', SELL: '卖出', HOLD: '持有' };
  /** 得分档表头短名（列宽有限） */
  const BUCKET_SHORT: Record<string, string> = {
    golden: '黄金', optional: '可选', caution: '谨慎', extreme: '极端高',
    neg_extreme: '极端低', neg_short: '做空', pos: '正分', neg: '负分',
  };

  /** 表头列筛选下拉（板块/行业/市值/趋势/得分/信号），长菜单限高滚动避免盖住整个列表 */
  const headerDropdown = (items: { value: string; label: string }[], current: string | undefined, onPick: (v?: string) => void, placeholder: string) => (
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

      {/* 筛选面板：columnOnly 只留 模型+概念 两列（其余维度在列表表头筛选）；日历补推理后刷新列表 */}
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
        onInferred={() => fetchList(1, false)}
      />

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
            {bench && (
              <span className="text-[9px] text-slate-400 font-mono" title="当天各维度头部前10均分基准（排名第1股票所在维度）">
                头部基准
                {top!.board_top10_avg != null && <span className="text-slate-500"> 板{top!.board_top10_avg >= 0 ? '+' : ''}{top!.board_top10_avg.toFixed(3)}</span>}
                {top!.industry_top10_avg != null && <span className="text-slate-500"> 行{top!.industry_top10_avg >= 0 ? '+' : ''}{top!.industry_top10_avg.toFixed(3)}</span>}
                {top!.cap_top10_avg != null && <span className="text-slate-500"> 市{top!.cap_top10_avg >= 0 ? '+' : ''}{top!.cap_top10_avg.toFixed(3)}</span>}
              </span>
            )}
          </div>
        );
      })()}

      {/* 分页跳转栏：首页/末页 + 当前页/总页数（放条件筛选后、列表前，避免底部遮住列表） */}
      {data && data.total > 0 && (
        <div className="flex items-center justify-between gap-1.5 px-1 py-1 shrink-0">
          <button
            onClick={goFirst}
            disabled={data.page <= 1 || loading}
            title="跳到首页（排名第1）"
            className="flex items-center gap-0.5 px-2 py-1 rounded-md text-[10px] font-bold border transition-colors disabled:opacity-30 disabled:cursor-not-allowed bg-slate-50 text-slate-600 border-slate-200 hover:bg-blue-50 hover:text-blue-600 hover:border-blue-200"
          >
            <ChevronsUp className="w-3 h-3" /> 首页
          </button>
          <span className="text-[10px] font-mono text-slate-500">
            第 <b className="text-slate-700">{data.page}</b> / {totalPages} 页
            <span className="text-slate-400"> · 共 {data.total} 只</span>
          </span>
          <button
            onClick={goLast}
            disabled={data.page >= totalPages || loading}
            title="跳到末页（排名最后）"
            className="flex items-center gap-0.5 px-2 py-1 rounded-md text-[10px] font-bold border transition-colors disabled:opacity-30 disabled:cursor-not-allowed bg-slate-50 text-slate-600 border-slate-200 hover:bg-blue-50 hover:text-blue-600 hover:border-blue-200"
          >
            末页 <ChevronsDown className="w-3 h-3" />
          </button>
        </div>
      )}

      {/* 列表头：单行 10 列，与每行严格对齐；点击表头筛选 */}
      <div className={`${GRID} px-1 pb-1 pt-2 text-[11px] font-bold text-slate-400 border-b border-slate-100 shrink-0 items-center`}>
        <span className="text-center">排名</span>
        <span>股票</span>
        <span className="text-center" title="近15日收盘价走势（红涨绿跌）">走势</span>
        <span className="text-center">{headerDropdown(fac('board', BOARD_OPTIONS.map(b => ({ value: b, label: b }))), filters.board, v => onFiltersChange({ ...filters, board: v }), '板块')}</span>
        <span className="text-center">{headerDropdown(fac('industry', []), filters.industry, v => onFiltersChange({ ...filters, industry: v }), '行业')}</span>
        <span className="text-center">{headerDropdown(fac('cap_tier', CAP_TIER_OPTIONS), filters.capTier, v => onFiltersChange({ ...filters, capTier: v }), '市值')}</span>
        <span className="text-center">{headerDropdown(fac('trend', TREND_OPTIONS), filters.trend, v => onFiltersChange({ ...filters, trend: v }), '趋势')}</span>
        <span className="text-right">{headerDropdown(fac('bucket', BUCKET_OPTIONS), filters.bucket, v => onFiltersChange({ ...filters, bucket: v, scoreMin: undefined }),
          filters.bucket ? (BUCKET_SHORT[filters.bucket] ?? '得分') : '得分')}</span>
        <span className="text-center" title="仓位信号：0=不入场（低于行业头部/大盘空仓），0.1~0.99=建议投入比例（半凯利）">仓位</span>
        <span className="text-center">{headerDropdown(fac('side', [{ value: 'BUY', label: '买入' }, { value: 'SELL', label: '卖出' }, { value: 'HOLD', label: '持有' }]), filters.side, v => onFiltersChange({ ...filters, side: v }), '信号')}</span>
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
            const up = (it.pct_change ?? 0) >= 0;
            const rank = pageOffsetRef.current + i + 1;   // 跳页后显示真实名次
            const rankMedal = rank <= 3 ? ['🥇', '🥈', '🥉'][rank - 1] : String(rank);
            return (
              <button
                key={it.symbol}
                data-symbol={it.symbol}
                onClick={() => onSelect(it)}
                className={`w-full ${GRID} items-center px-1.5 py-1.5 rounded-lg text-left transition-colors ${
                  isSel ? 'bg-blue-50 border border-blue-200' : 'hover:bg-slate-50 border border-transparent'
                }`}
              >
                <span className={`text-center text-[11px] font-mono font-bold ${rank <= 3 ? 'text-base leading-none' : 'text-slate-400'}`}>{rankMedal}</span>
                {/* 股票单元格：主行(名称|涨幅) + 副行(代码|价格·市值)，单列内 flex-col */}
                <span className="flex flex-col min-w-0 gap-0.5">
                  <span className="flex items-center justify-between gap-1">
                    <span className="text-[13px] font-bold text-slate-700 truncate flex items-center gap-0.5 min-w-0">
                      {(() => {
                        const watched = watchlistSymbols.has(toPrefix(it.symbol));
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
                        const kind = positions.get(toPrefix(it.symbol));
                        if (!kind) return null;
                        const badge = POSITION_BADGE[kind];
                        return <span title={badge.title} className={`text-[9px] font-bold rounded px-0.5 shrink-0 border ${badge.cls}`}>{badge.label}</span>;
                      })()}
                      {it.is_st && <span className="text-[10px] bg-rose-50 text-rose-500 rounded px-0.5 shrink-0">ST</span>}
                      <span className="truncate">{it.name}</span>
                    </span>
                    <span className={`text-[11px] font-mono shrink-0 ${up ? 'text-rose-500' : 'text-emerald-500'}`}>{fmtPct(it.pct_change)}</span>
                  </span>
                  <span className="flex items-center justify-between gap-1">
                    <span className="text-[10px] text-slate-400 font-mono truncate">{it.symbol}</span>
                    <span className="text-[10px] text-slate-500 font-mono shrink-0">{it.close?.toFixed(2) ?? '--'} · {fmtMv(it.total_mv)}</span>
                  </span>
                </span>
                {/* 近15日走势微缩折线（懒加载，红涨绿跌） */}
                <span className="flex items-center justify-center">
                  <Sparkline symbol={it.symbol} days={15} />
                </span>
                {/* 板块 + 当天头部 top10 均分 */}
                <span className="flex flex-col items-center min-w-0 gap-0" title={`${it.board ?? '--'} · top10 ${it.board_top10_avg != null ? (it.board_top10_avg >= 0 ? '+' : '') + it.board_top10_avg.toFixed(3) : '--'}`}>
                  <span className={`inline-block text-[10px] font-bold rounded px-0.5 border truncate max-w-full ${boardToneOf(it.board)}`}>{it.board?.replace('市主板', '主板') ?? '--'}</span>
                  <span className="text-[10px] font-mono text-slate-400 truncate">{it.board_top10_avg != null ? `${it.board_top10_avg >= 0 ? '+' : ''}${it.board_top10_avg.toFixed(3)}` : ''}</span>
                </span>
                {/* 行业 + 当天头部 top10 均分 */}
                <span className="flex flex-col items-center min-w-0 gap-0" title={`${it.industry ?? '--'} · top10 ${it.industry_top10_avg != null ? (it.industry_top10_avg >= 0 ? '+' : '') + it.industry_top10_avg.toFixed(3) : '--'}`}>
                  <span className="text-[10px] text-slate-600 truncate max-w-full">{it.industry ?? '--'}</span>
                  <span className="text-[10px] font-mono text-slate-400 truncate">{it.industry_top10_avg != null ? `${it.industry_top10_avg >= 0 ? '+' : ''}${it.industry_top10_avg.toFixed(3)}` : ''}</span>
                </span>
                {/* 市值档 + 当天头部 top10 均分 */}
                <span className="flex flex-col items-center min-w-0 gap-0" title={`${it.cap_tier || '--'} · top10 ${it.cap_top10_avg != null ? (it.cap_top10_avg >= 0 ? '+' : '') + it.cap_top10_avg.toFixed(3) : '--'}`}>
                  <span className="text-[10px] text-slate-600 shrink-0">{it.cap_tier || '--'}</span>
                  <span className="text-[10px] font-mono text-slate-400 truncate">{it.cap_top10_avg != null ? `${it.cap_top10_avg >= 0 ? '+' : ''}${it.cap_top10_avg.toFixed(3)}` : ''}</span>
                </span>
                {/* 趋势 */}
                <span className={`text-center text-[10px] truncate ${TREND_COLOR[it.trend ?? ''] ?? 'text-slate-400'}`}>{it.trend ?? '-'}</span>
                {/* 得分 */}
                <span className={`text-right text-[12px] font-mono font-bold ${(it.fusion ?? 0) >= 0 ? 'text-blue-600' : 'text-slate-400'}`}>
                  {it.fusion != null ? `+${(it.fusion).toFixed(3)}`.replace('+-', '-') : '--'}
                </span>
                {/* 仓位信号 */}
                <span className="text-center">
                  {(() => {
                    const ps = it.position_score;
                    const tone = positionToneOf(ps);
                    const pct = it.pct_industry;
                    const empty = it.market_empty;
                    const tip = ps == null
                      ? '该日无仓位信号（未推理或缺失基准）'
                      : ps <= 0
                        ? (empty ? '大盘空仓信号，不入场' : (pct != null && pct < 0.8 ? `行业百分位 ${(pct * 100).toFixed(0)}% < 80%，不入场` : '不入场'))
                        : `建议投入 ${Math.round(ps * 100)}%（半凯利）· 行业百分位 ${pct != null ? (pct * 100).toFixed(0) + '%' : '--'}`;
                    return (
                      <span className={`inline-block text-[10px] font-bold rounded px-0.5 py-0.5 border ${tone.cls}`} title={tip}>
                        {tone.txt}
                      </span>
                    );
                  })()}
                </span>
                {/* 信号方向 */}
                <span className="text-center">
                  <span className={`text-[10px] rounded px-1 py-0.5 font-bold ${SIDE_COLOR[it.side ?? 'HOLD'] ?? SIDE_COLOR.HOLD}`}>
                    {(it.side ?? 'HOLD') === 'HOLD' ? '-' : it.side}
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
          {!loading && visibleItems.length === 0 && (
            <div className="text-center py-8 text-[11px] text-slate-400">无匹配股票</div>
          )}
      </div>
    </div>
  );
}
