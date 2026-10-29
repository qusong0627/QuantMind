/**
 * 板块标签融合条（资金流全景工具栏与桑基之间；「标签双向查询」并入资金流全景）。
 *
 * 统一搜索框的解析优先级（Enter 一键提交，候选 chips 可逐个点开）：
 * 1. 个股代码（纯 6 位 / SH|SZ|BJ 前缀式 / 后缀式）→ normalizeStockCode → 打开个股卡；
 * 2. 当前体系板块名精确命中 → 直接联动（保持当前体系）；
 * 3. 首个受支持板块候选 > 首个个股候选（文字搜股，如「茅台」→ 贵州茅台）。
 * 文字搜索**并行**查两路：/tags/matches（板块候选）与 /stock-terminal/list（个股联想，
 * 名称/代码/拼音首字母）；两路独立错误态互不拦——任一路失败仍保留另一路结果。
 * 地区/风格等非资金流类型**如实列出但不可联动**（体系仅此三种，不假装能切）。
 *
 * 热门区与页面体系（sectorSet）同源同步：热门 tabs 即三个体系选择器（点 tab =
 * 切页面体系，与工具栏「体系」组天然一致，无第二份选择状态）；热门数据按体系向
 * /tags/stats?hot_type=… 单体系取数——全局混合 top-N 会被成分数大的体系（概念/
 * 地区）挤空（行业一级在全局 top-100 里一条都没有），单体系才凑得齐 10-20 个。
 * 每体系结果进程内缓存，回切不闪烁；失败只隐去热门区并给重试，绝不拦主路径。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Flame, Search, X } from 'lucide-react';
import { normalizeStockCode } from '../../../utils/portfolioUtils';
import { stockTerminalService } from '../../stock-terminal/services/stockTerminalService';
import {
  getTagMatches,
  getTagStats,
  type FlowSectorSet,
  type FlowTagMatch,
  type FlowTagStats,
} from '../services/moneyFlowService';

/** 板块类型字符串（后端 tags 口径）→ 资金流体系键；仅这三种可联动。 */
const SET_OF_TYPE: Record<string, FlowSectorSet> = {
  '行业板块(一级)': 'industry1',
  '行业板块(二级)': 'industry2',
  '概念板块': 'concept',
};
const SET_LABEL: Record<FlowSectorSet, string> = {
  industry1: '行业一级',
  industry2: '行业二级',
  concept: '概念',
};
/** 体系键 → 后端板块类型字符串（hot_type 入参）。 */
const TYPE_OF_SET: Record<FlowSectorSet, string> = {
  industry1: '行业板块(一级)',
  industry2: '行业板块(二级)',
  concept: '概念板块',
};
/** 热门 tabs 顺序与工具栏「体系」组一致（二级默认在前）。 */
const SET_ORDER: FlowSectorSet[] = ['industry2', 'industry1', 'concept'];
const HOT_MAX = 5;
const MATCH_MAX = 10;
const STOCK_MAX = 6;
const SEARCH_DEBOUNCE_MS = 200;

const CODE_RE = /^(SH|SZ|BJ)\d{6}$/;

interface StockHit {
  symbol: string;
  name: string;
}

export interface TagBrowserStripProps {
  /** 当前体系板块名（全景数据；为空时精确命中路径自然失效，不报错）。 */
  currentSectors: string[];
  /** 当前资金流体系（热门 tabs 高亮与之同步）。 */
  sectorSet: FlowSectorSet;
  /** 热门 tab 点击 → 切换页面体系（复用工具栏同一入口，选中板块联动由页面负责）。 */
  onSectorSetChange: (set: FlowSectorSet) => void;
  /** 板块联动（set 为 null 时保持当前体系）。 */
  onNavigateSector: (name: string, set: string | null) => void;
  /** 代码/个股候选命中 → 打开个股卡。 */
  onSelectStock: (symbol: string, name?: string) => void;
}

export const TagBrowserStrip: React.FC<TagBrowserStripProps> = ({
  currentSectors,
  sectorSet,
  onSectorSetChange,
  onNavigateSector,
  onSelectStock,
}) => {
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<FlowTagMatch[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchErr, setSearchErr] = useState(false);
  const [stockHits, setStockHits] = useState<StockHit[] | null>(null);
  const [searchingStock, setSearchingStock] = useState(false);
  const [stockErr, setStockErr] = useState(false);
  const [hotBySet, setHotBySet] = useState<Partial<Record<FlowSectorSet, FlowTagStats['hot_tags']>>>({});
  const [hotErrBySet, setHotErrBySet] = useState<Partial<Record<FlowSectorSet, boolean>>>({});
  const [statsMeta, setStatsMeta] = useState<FlowTagStats | null>(null);
  const matchSeq = useRef(0);

  // ── 当前体系热门（按体系取数 + 进程内缓存；失败只隐去热门区并可重试） ──
  useEffect(() => {
    if (hotBySet[sectorSet] || hotErrBySet[sectorSet]) return;
    let alive = true;
    getTagStats(HOT_MAX, TYPE_OF_SET[sectorSet])
      .then((s) => {
        if (!alive) return;
        setHotBySet((prev) => ({
          ...prev,
          [sectorSet]: (s.hot_tags ?? []).filter((t) => SET_OF_TYPE[t.type] === sectorSet),
        }));
        // 统计四数为全量口径（不随 hot_type 变化），任一响应都可作为 meta 源
        setStatsMeta(s);
      })
      .catch(() => {
        if (alive) setHotErrBySet((prev) => ({ ...prev, [sectorSet]: true }));
      });
    return () => {
      alive = false;
    };
  }, [sectorSet, hotBySet, hotErrBySet]);

  const code = useMemo(() => {
    const n = normalizeStockCode(query);
    return CODE_RE.test(n) ? n : null;
  }, [query]);

  // ── 候选查询（防抖；代码形态短路不发请求——板块名不含 6 位数字段） ──
  useEffect(() => {
    const q = query.trim();
    if (!q || code) {
      setMatches(null);
      setStockHits(null);
      setSearching(false);
      setSearchingStock(false);
      setSearchErr(false);
      setStockErr(false);
      return;
    }
    const seq = ++matchSeq.current;
    setSearching(true);
    setSearchingStock(true);
    setSearchErr(false);
    setStockErr(false);
    const timer = setTimeout(() => {
      // 板块候选与个股联想并行、错误态独立：一路失败不拦另一路
      getTagMatches(q)
        .then((m) => {
          if (seq === matchSeq.current) setMatches(m);
        })
        .catch(() => {
          if (seq === matchSeq.current) {
            setMatches(null);
            setSearchErr(true);
          }
        })
        .finally(() => {
          if (seq === matchSeq.current) setSearching(false);
        });
      stockTerminalService
        .getStockList({ q, page: 1, page_size: 8 })
        .then((resp) => {
          if (seq !== matchSeq.current) return;
          setStockHits(
            (resp.items ?? [])
              .map((it) => ({
                symbol: String(it.symbol || ''),
                name: String(it.name || it.symbol || ''),
              }))
              .filter((it) => it.symbol)
              .slice(0, STOCK_MAX),
          );
        })
        .catch(() => {
          if (seq === matchSeq.current) {
            setStockHits(null);
            setStockErr(true);
          }
        })
        .finally(() => {
          if (seq === matchSeq.current) setSearchingStock(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query, code]);

  const exactCurrent = useMemo(() => {
    const q = query.trim();
    return q ? (currentSectors.find((s) => s === q) ?? null) : null;
  }, [query, currentSectors]);

  const supportedMatches = useMemo(
    () => (matches ?? []).filter((m) => SET_OF_TYPE[m.sector_type]),
    [matches],
  );

  /** Enter 一键提交：代码 → 精确当前体系 → 首个受支持板块候选 → 首个个股候选。 */
  const submit = () => {
    if (code) {
      onSelectStock(code);
      setQuery('');
      return;
    }
    if (exactCurrent) {
      onNavigateSector(exactCurrent, null);
      return;
    }
    const first = supportedMatches[0];
    if (first) {
      onNavigateSector(first.name, SET_OF_TYPE[first.sector_type]);
      return;
    }
    const firstStock = stockHits?.[0];
    if (firstStock) {
      onSelectStock(firstStock.symbol, firstStock.name);
      setQuery('');
    }
  };

  const hotList = hotBySet[sectorSet] ?? [];
  const hotErr = hotErrBySet[sectorSet] === true;
  const hotPending = !hotBySet[sectorSet] && !hotErr;

  const retryHot = () =>
    setHotErrBySet((prev) => {
      const next = { ...prev };
      delete next[sectorSet];
      return next;
    });

  const searchActive = query.trim().length > 0;

  return (
    <div className="bg-white/95 backdrop-blur-md rounded-2xl px-3.5 py-2.5 border border-purple-100/80 shadow-xs flex flex-col gap-2 min-w-0">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {/* 统一搜索框 */}
        <div className="flex items-center gap-1.5 bg-slate-100/90 border border-slate-200/70 rounded-full px-3 py-1.5 w-full sm:w-80 shrink-0">
          <Search className="w-3.5 h-3.5 text-slate-400 shrink-0" />
          <input
            aria-label="统一搜索：板块、标签或股票代码"
            className="flex-1 min-w-0 bg-transparent outline-none text-xs font-bold text-slate-700 placeholder:text-slate-400 placeholder:font-normal"
            placeholder="搜索板块 / 概念 / 个股名称或代码"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit();
            }}
          />
          {searchActive && (
            <button
              aria-label="清空搜索"
              className="text-slate-300 hover:text-slate-600 shrink-0 cursor-pointer"
              onClick={() => setQuery('')}
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>

        {/* 热门板块：体系 tabs = 页面体系选择器（同源同步），单体系 top-5 chips */}
        <div className="flex items-center gap-2 flex-wrap min-w-0 flex-1">
          <span className="flex items-center gap-1 text-[10px] font-extrabold text-slate-400 whitespace-nowrap">
            <Flame className="w-3 h-3 text-orange-400" />
            热门
          </span>
          <div className="flex items-center bg-slate-100/90 border border-slate-200/60 p-0.5 rounded-full gap-0.5">
            {SET_ORDER.map((id) => (
              <button
                key={id}
                onClick={() => onSectorSetChange(id)}
                className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold transition-colors cursor-pointer whitespace-nowrap ${
                  sectorSet === id ? 'bg-purple-600 text-white' : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                {SET_LABEL[id]}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1 flex-wrap min-w-0">
            {hotList.map((t) => (
              <button
                key={`${t.type}-${t.name}`}
                onClick={() => onNavigateSector(t.name, SET_OF_TYPE[t.type])}
                title={`${t.type} · 成分 ${t.count} 只：点击联动资金流工作台`}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-slate-50 text-slate-600 border border-slate-200/70 hover:bg-purple-50 hover:text-purple-700 hover:border-purple-200 transition-colors cursor-pointer whitespace-nowrap"
              >
                {t.name}
                <span className="font-mono text-slate-400">{t.count}</span>
              </button>
            ))}
            {hotPending && <span className="text-[10px] text-slate-400">热门加载中…</span>}
            {hotErr && (
              <span className="inline-flex items-center gap-1.5 text-[10px] text-slate-400">
                该体系热门暂不可用（搜索不受影响）
                <button
                  onClick={retryHot}
                  className="text-purple-500 hover:text-purple-700 font-bold cursor-pointer"
                >
                  重试
                </button>
              </span>
            )}
          </div>
        </div>
      </div>

      {/* 统计一行 meta */}
      {statsMeta && (
        <span className="text-[10px] font-mono text-slate-400">
          {statsMeta.trade_date ? `基准日 ${statsMeta.trade_date} · ` : ''}板块{' '}
          {statsMeta.total_sectors.toLocaleString()} · 标的 {statsMeta.total_stocks.toLocaleString()} · 人均标签{' '}
          {statsMeta.avg_tags_per_stock} · 关系 {statsMeta.total_relations.toLocaleString()}
        </span>
      )}

      {/* 搜索候选行 */}
      {searchActive && (
        <div className="flex items-start gap-1.5 flex-wrap pt-0.5 border-t border-slate-100">
          {code && (
            <button
              onClick={() => {
                onSelectStock(code);
                setQuery('');
              }}
              className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[10px] font-extrabold bg-purple-600 text-white hover:bg-purple-500 transition-colors cursor-pointer"
              title="按 Enter 或点击打开个股资金流详情卡"
            >
              打开个股卡 <span className="font-mono">{code}</span>
            </button>
          )}
          {!code && searching && <span className="text-[10px] text-slate-400 py-0.5">搜索中…</span>}
          {!code && !searching && searchErr && (
            <span className="text-[10px] text-slate-400 py-0.5">
              板块候选查询失败——可直接输入完整板块名后回车联动
            </span>
          )}
          {!code && !searching && !searchErr && matches !== null && matches.length > 0 && (
            <>
              {exactCurrent && (
                <span
                  className="text-[10px] text-emerald-700 bg-emerald-50 border border-emerald-200/70 rounded-full px-2 py-0.5 font-bold whitespace-nowrap"
                  title="与当前体系板块名精确一致，按 Enter 直接联动"
                >
                  精准命中：{exactCurrent}
                </span>
              )}
              {matches.slice(0, MATCH_MAX).map((m) => {
                const set = SET_OF_TYPE[m.sector_type];
                return set ? (
                  <button
                    key={`${m.sector_type}-${m.name}`}
                    onClick={() => onNavigateSector(m.name, set)}
                    title={`${m.sector_type}（${SET_LABEL[set]}）· 成分 ${m.count} 只：点击切换体系并联动`}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-purple-50/70 text-purple-700 border border-purple-200/70 hover:bg-purple-100 transition-colors cursor-pointer whitespace-nowrap"
                  >
                    <span className="text-purple-400">{SET_LABEL[set]}</span>
                    {m.name}
                    <span className="font-mono text-purple-400">{m.count}</span>
                  </button>
                ) : (
                  <span
                    key={`${m.sector_type}-${m.name}`}
                    title={`${m.sector_type}不在资金流体系（行业一级/二级/概念），不可联动`}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-slate-50 text-slate-400 border border-slate-200/60 whitespace-nowrap cursor-help"
                  >
                    {m.name}
                    <span className="font-mono">{m.count}</span>
                  </span>
                );
              })}
            </>
          )}
          {/* 板块侧无结果时，个股联想仍在途 → 继续显搜索中（两路错峰完成） */}
          {!code && !searching && !searchErr && matches !== null && matches.length === 0 && searchingStock && (
            <span className="text-[10px] text-slate-400 py-0.5">搜索中…</span>
          )}
          {!code &&
            !searching &&
            !searchErr &&
            matches !== null &&
            matches.length === 0 &&
            !searchingStock &&
            (stockHits?.length ?? 0) === 0 && (
              <span className="text-[10px] text-slate-400 py-0.5">
                {stockErr ? '无匹配板块' : '无匹配板块或个股'}
              </span>
            )}
          {/* 个股候选：个股侧就绪即并入（不依赖板块候选是否为空，也不拦板块渲染） */}
          {!code &&
            !searching &&
            !searchingStock &&
            (stockHits ?? []).map((s) => (
              <button
                key={`stock-${s.symbol}`}
                onClick={() => {
                  onSelectStock(s.symbol, s.name);
                  setQuery('');
                }}
                title={`个股 ${s.name}：点击打开个股资金流详情卡`}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold bg-purple-50/70 text-purple-700 border border-purple-200/70 hover:bg-purple-100 transition-colors cursor-pointer whitespace-nowrap"
              >
                <span className="text-purple-400">个股</span>
                {s.name}
                <span className="font-mono text-purple-400">{s.symbol}</span>
              </button>
            ))}
        </div>
      )}
    </div>
  );
};
