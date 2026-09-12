/**
 * 个股终端页面骨架 —— 三市场共用的外壳。
 *
 * 结构：顶栏（标题 + 居中搜索 + 选中后价格/模型下拉）
 *   → 左：K 线卡（日/周/月 + 复权切换 + 底部推理分副图）
 *   → 右：400px 详情卡（头部概况 + 市场标签），详情体由各市场通过 renderDetail 注入。
 *
 * 市场差异全部来自 useStockTerminal() 的主题与服务，本文件不含任何市场判断。
 */

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { CandlestickChart, Search, Layers, Building2, Database, TrendingUp, TrendingDown } from 'lucide-react';
import { message, Select } from 'antd';
import { PAGE_LAYOUT } from '../../../config/pageLayout';
import { useStockTerminal } from '../adapter';
import { StockListItem, StockProfile, KlineBar, KlineMarker } from '../types';
import type { KlineAdjust } from '../service';
import { modelTrainingService } from '../../../services/modelTrainingService';
import { StockSearchBar } from '../components/StockSearchBar';
import type { Point as ScorePoint } from '../components/InferenceScoreChart';
import { KlineChart } from '../components/KlineChart';

export interface DetailRenderContext {
  symbol: string;
  name: string;
  close: number | null | undefined;
  signalDate?: string;
  profile: StockProfile | null;
}

interface Props {
  /** 右侧详情体（各市场实现：A 股 9 Tab / 港股 7 Tab / 美股 9 Tab） */
  renderDetail: (ctx: DetailRenderContext) => ReactNode;
  /** 右侧头部额外内容（可选，如美股的公司英文名行） */
  renderHeaderExtra?: (ctx: DetailRenderContext) => ReactNode;
  /** 顶部通栏提示条（可选，A 股用它放「模型推理分说明」；未提供则不占位） */
  renderBanner?: (ctx: DetailRenderContext) => ReactNode;
}

const KLINE_PERIODS: { key: 'daily' | 'weekly' | 'monthly'; label: string }[] = [
  { key: 'daily', label: '日K' },
  { key: 'weekly', label: '周K' },
  { key: 'monthly', label: '月K' },
];

function resampleBars(bars: KlineBar[], period: 'weekly' | 'monthly'): KlineBar[] {
  const map = new Map<string, KlineBar[]>();
  for (const b of bars) {
    const key = period === 'weekly' ? weekKey(b.date) : b.date.slice(0, 7);
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(b);
  }
  return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([, grp]) => ({
    date: period === 'weekly' ? grp[grp.length - 1].date : `${grp[0].date.slice(0, 7)}-01`,
    open: grp[0].open,
    high: Math.max(...grp.map((g) => g.high)),
    low: Math.min(...grp.map((g) => g.low)),
    close: grp[grp.length - 1].close,
    volume: grp.reduce((s, g) => s + (g.volume ?? 0), 0),
  }));
}

function weekKey(date: string): string {
  const d = new Date(date + 'T00:00:00');
  const day = (d.getDay() + 6) % 7;
  d.setDate(d.getDate() - day);
  return d.toISOString().slice(0, 10);
}

export default function StockTerminalShell({ renderDetail, renderHeaderExtra, renderBanner }: Props) {
  const { service, theme, toWatchSymbol } = useStockTerminal();
  const [selected, setSelected] = useState<StockListItem | null>(null);
  const [profile, setProfile] = useState<StockProfile | null>(null);
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [barsLoading, setBarsLoading] = useState(false);
  /** K 线事件竖线（美股拆股）；A 股/港股服务默认返回空数组 */
  const [markers, setMarkers] = useState<KlineMarker[]>([]);
  const [period, setPeriod] = useState<'daily' | 'weekly' | 'monthly'>('daily');
  const [adjust, setAdjust] = useState<KlineAdjust>(theme.defaultAdjust);
  const [signalDate, setSignalDate] = useState<string | undefined>(undefined);
  const [watchlist, setWatchlist] = useState<Set<string>>(new Set());
  const [scorePoints, setScorePoints] = useState<ScorePoint[]>([]);
  // 多模型切换：当前选中模型（undefined=默认模型）+ 下拉模型列表
  const [modelId, setModelId] = useState<string | undefined>(undefined);
  const [scoreModels, setScoreModels] = useState<Array<{ model_id: string; display_name?: string }>>([]);
  const [modelName, setModelName] = useState<string>('');
  const [scoreLast, setScoreLast] = useState<{ value: number; date: string; up: boolean } | null>(null);

  // 拉起推理分数：喂给 K线底部分数副图 + 顶部最近分数 + 模型下拉列表。
  // 不渲染独立折线图组件，分数统一由 K线底部副图承载（与主图同 x 轴对齐）。
  useEffect(() => {
    if (!selected) {
      setScorePoints([]);
      setScoreLast(null);
      setScoreModels([]);
      return;
    }
    let cancelled = false;
    const code = selected.symbol.split('.')[0];
    modelTrainingService
      .getStockInferenceHistory(code, 750, modelId || undefined)
      .then((resp) => {
        if (cancelled) return;
        const pts: ScorePoint[] = (resp.items ?? [])
          .filter((it) => it.fusion_score != null)
          .map((it) => ({
            date: String(it.trade_date).slice(0, 10),
            value: Number(it.fusion_score),
            side: it.signal_side ? String(it.signal_side) : null,
          }))
          .sort((a, b) => a.date.localeCompare(b.date));
        setScorePoints(pts);
        const last = pts[pts.length - 1];
        setScoreLast(last && typeof last.value === 'number'
          ? { value: Number(last.value), date: last.date, up: Number(last.value) >= 0 }
          : null);
        const models = resp.models ?? [];
        setScoreModels(models);
        const chosen = modelId ? models.find((m) => m.model_id === modelId) : models[0];
        setModelName(chosen ? (chosen.display_name || chosen.model_id || '') : '');
      })
      .catch(() => {
        if (!cancelled) {
          setScorePoints([]);
          setScoreLast(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selected, modelId]);

  // watchlist 仅为搜索下拉星标
  useEffect(() => {
    let cancelled = false;
    import('../../../services/researchService')
      .then(({ researchService }) =>
        researchService.getWatchlist(200).then((resp) => {
          if (!cancelled) setWatchlist(new Set(resp.items.map((i) => toWatchSymbol(i.symbol))));
        }),
      )
      .catch(() => {
        if (!cancelled) setWatchlist(new Set());
      });
    return () => {
      cancelled = true;
    };
  }, [toWatchSymbol]);

  const handleSelect = useCallback((item: StockListItem) => {
    setSelected(item);
    setSignalDate(undefined);
  }, []);

  // 详情随选中+信号日联动
  useEffect(() => {
    if (!selected) {
      setProfile(null);
      return;
    }
    let cancelled = false;
    service.getProfile(selected.symbol, signalDate).then((p) => {
      if (!cancelled) setProfile(p);
    });
    return () => {
      cancelled = true;
    };
  }, [selected, signalDate, service]);

  // K线随选中+周期+复权方式联动；取数回 2 年（≈500 根）不硬截断——首屏由 dataZoom
  // 聚焦最近 200 根，滑条可向左拖回看更早日期；周/月由日K重采样。
  useEffect(() => {
    if (!selected) {
      setBars([]);
      return;
    }
    let cancelled = false;
    setBarsLoading(true);
    const endD = new Date();
    const startD = new Date(endD);
    startD.setFullYear(startD.getFullYear() - 2);
    const iso = (d: Date) => d.toISOString().slice(0, 10);
    service
      .getDailyKline(selected.symbol, 500, adjust, iso(startD), iso(endD))
      .then((items) => {
        if (cancelled) return;
        if (period !== 'daily' && items.length) {
          setBars(resampleBars(items, period));
        } else {
          setBars(items);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBars([]);
          message.error('K线加载失败');
        }
      })
      .finally(() => {
        if (!cancelled) setBarsLoading(false);
      });
    // 事件标记独立取（A 股/港股返回空数组，不产生额外请求）
    service
      .getKlineMarkers(selected.symbol, iso(startD), iso(endD))
      .then((ms) => {
        if (!cancelled) setMarkers(ms ?? []);
      })
      .catch(() => {
        if (!cancelled) setMarkers([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, period, adjust, service]);

  const up = (profile?.pct_change ?? selected?.pct_change ?? 0) >= 0;

  // 首屏缩放窗口：默认聚焦最近 200 根；根数不足 200 时全显（周/月重采样后根数少）
  const zoomStart = bars.length > 200 ? Number((100 - (200 / bars.length) * 100).toFixed(1)) : 0;

  const detailCtx: DetailRenderContext | null = selected
    ? {
        symbol: selected.symbol,
        name: profile?.name ?? selected.name ?? selected.symbol,
        close: profile?.close ?? selected.close,
        signalDate,
        profile,
      }
    : null;

  return (
    /* 底部 pb-[84px]：给悬浮 Dock 菜单栏（64px）留出空间，避免遮挡 K线图底部的缩放条 */
    <div className="w-full h-full bg-[#f8fafc] px-6 pt-6 pb-[84px] flex flex-col overflow-hidden">
      <div className={PAGE_LAYOUT.frameClass}>
        {/* 顶栏：标题 + 居中搜索框 + 价格/模型 */}
        <header className={PAGE_LAYOUT.headerClass} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
          <div className="flex items-center gap-3 min-w-0 shrink-0">
            <div className={`w-10 h-10 bg-gradient-to-br ${theme.accentFrom} ${theme.accentTo} rounded-2xl flex items-center justify-center shadow-lg shrink-0`}>
              <CandlestickChart className="w-5 h-5 text-white" />
            </div>
            <div className="flex items-center min-w-0">
              <h1 className="text-lg font-bold text-slate-800 tracking-tight whitespace-nowrap">{theme.title}</h1>
            </div>
          </div>
          <div className="flex-1 min-w-0 px-3">
            <div className="max-w-[560px] mx-auto">
              <StockSearchBar onSelect={handleSelect} watchlistSymbols={watchlist} />
            </div>
          </div>
          {selected && (
            <div className="hidden md:flex items-center gap-2 text-[11px] text-slate-500 shrink-0">
              <span className="font-mono font-bold text-slate-700">{selected.symbol}</span>
              <span className="text-slate-300">·</span>
              <span className={`font-mono font-bold ${up ? 'text-rose-500' : 'text-emerald-500'}`}>
                {profile?.close?.toFixed(3) ?? selected.close?.toFixed(3) ?? '--'}
              </span>
              <span className="text-slate-300">·</span>
              <Select
                size="small"
                style={{ width: 130 }}
                placeholder="默认模型"
                value={modelId}
                onChange={setModelId}
                popupMatchSelectWidth={false}
                options={[
                  { value: 'default', label: '默认模型' },
                  ...scoreModels.map((m) => ({ value: m.model_id, label: m.display_name || m.model_id })),
                ]}
              />
            </div>
          )}
        </header>
        {detailCtx && renderBanner?.(detailCtx)}

        {/* 主体 */}
        {!selected || !detailCtx ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-3 bg-gray-50/50 p-8 text-center">
            <div className={`w-14 h-14 rounded-2xl bg-gradient-to-br ${theme.accentFrom} ${theme.accentTo} flex items-center justify-center shadow-md`}>
              <Search className="w-6 h-6 text-white" />
            </div>
            <div className="text-sm font-bold text-slate-700">{theme.emptyTitle}</div>
            <div className="text-xs text-slate-400 max-w-[420px] leading-relaxed">{theme.emptyDesc}</div>
            {/* 数据提示：一句话引导下载完整数据包并保持更新 */}
            <div className="flex items-center gap-1.5 text-[11px] text-slate-400">
              <Database className="w-3.5 h-3.5 shrink-0 text-slate-300" />
              <span>温馨提示：请先下载完整行情数据包并保持每日更新，否则可能搜不到标的、K线或推理分为空。</span>
            </div>
          </div>
        ) : (
          <div className="flex flex-1 min-h-0 overflow-hidden bg-gray-50/50 p-4 gap-4">
            {/* 左侧：K线大图（推理分数在底部独立副图，与主图 x 轴对齐） */}
            <div className="flex-1 min-w-0 flex flex-col overflow-hidden">
              <div className={`flex-1 min-h-0 flex flex-col rounded-3xl bg-white border ${theme.klineCardBorder} shadow-sm overflow-hidden`}>
                <div className="flex items-center justify-between px-4 py-2.5 border-b border-slate-100 bg-slate-50/60 shrink-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-xs font-black text-slate-700 truncate">
                      {selected.name} <span className="font-mono text-[11px] text-slate-400">{selected.symbol}</span>
                    </span>
                    {profile && (
                      <span className={`text-[11px] font-mono font-bold ${up ? 'text-rose-500' : 'text-emerald-500'}`}>
                        {profile.close?.toFixed(3) ?? '--'} {up ? '+' : ''}{(profile.pct_change ?? 0).toFixed(3)}%
                      </span>
                    )}
                    {scoreLast && (
                      <span className="flex items-center gap-1 shrink-0 text-[11px]">
                        <span className="text-slate-300">·</span>
                        <span className="flex items-center gap-0.5 text-slate-500">
                          {scoreLast.up
                            ? <TrendingUp className="w-3 h-3 text-rose-500" />
                            : <TrendingDown className="w-3 h-3 text-emerald-500" />}
                          <span className="font-mono font-bold text-slate-700">{scoreLast.value.toFixed(4)}</span>
                        </span>
                        <span className="text-slate-400">{scoreLast.date}</span>
                      </span>
                    )}
                    {modelName && <span className="hidden xl:inline text-[10px] font-mono text-indigo-500 truncate max-w-[120px]">· {modelName}</span>}
                    {theme.klineNote && (
                      <span className="hidden 2xl:inline text-[10px] text-slate-400 truncate max-w-[220px]">· {theme.klineNote}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-1 p-1 bg-slate-100 rounded-full shrink-0">
                    {KLINE_PERIODS.map((p) => (
                      <button
                        key={p.key}
                        onClick={() => setPeriod(p.key)}
                        className={`px-3 py-1 rounded-full text-[11px] font-bold transition-colors ${period === p.key ? `bg-white ${theme.accentText} shadow-sm` : 'text-slate-500 hover:text-slate-700'}`}
                      >
                        {p.label}
                      </button>
                    ))}
                    <div className="w-px h-4 bg-slate-200 mx-1" />
                    {theme.adjusts.map((a) => (
                      <button
                        key={a.key}
                        onClick={() => setAdjust(a.key)}
                        className={`px-2.5 py-1 rounded-full text-[11px] font-bold transition-colors ${adjust === a.key ? `bg-white ${theme.accentText} shadow-sm` : 'text-slate-500 hover:text-slate-700'}`}
                      >
                        {a.label}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="flex-1 min-h-0 p-2 flex flex-col">
                  {barsLoading ? (
                    <div className="h-full flex items-center justify-center text-xs text-slate-400">K线加载中…</div>
                  ) : bars.length ? (
                    <div className="flex-1 min-h-0">
                      <KlineChart bars={bars} config={{ ma: true, boll: false, subplots: ['vol'] }} overlays={[]} period={period} scorePoints={scorePoints.map((p) => ({ date: p.date, value: p.value }))} showScoreSubplot={true} markers={markers} zoomStart={zoomStart} zoomEnd={100} />
                    </div>
                  ) : (
                    <div className="h-full flex items-center justify-center text-xs text-slate-400">暂无K线</div>
                  )}
                </div>
              </div>
            </div>

            {/* 右侧：详情 — 400px 定宽 */}
            <div className="w-[400px] max-w-[400px] shrink-0 flex flex-col rounded-3xl bg-white border border-slate-200/80 shadow-sm overflow-hidden">
              <div className="shrink-0 flex flex-col overflow-hidden border-b border-slate-100">
                <div className="px-4 py-3 bg-slate-50/60 shrink-0">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className={`w-7 h-7 rounded-lg ${theme.accentText} bg-white border border-slate-200 flex items-center justify-center shrink-0`}>
                          <Building2 className="w-3.5 h-3.5" />
                        </span>
                        <span className="text-[15px] font-black text-slate-800 truncate">{detailCtx.name}</span>
                        <span className="shrink-0 text-[10px] font-mono px-1.5 py-0.5 rounded-md bg-white border border-slate-200 text-slate-500">
                          {detailCtx.symbol}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center gap-2 flex-wrap text-[11px]">
                        <span className={`font-mono font-bold ${up ? 'text-rose-500' : 'text-emerald-500'}`}>
                          {profile?.close?.toFixed(3) ?? selected.close?.toFixed(3) ?? '--'} {up ? '+' : ''}
                          {(profile?.pct_change ?? selected.pct_change ?? 0).toFixed(3)}%
                        </span>
                        {profile?.board && (
                          <>
                            <span className="text-slate-300">·</span>
                            <span className="text-slate-500">{profile.board}</span>
                          </>
                        )}
                        {profile?.industry && (
                          <>
                            <span className="text-slate-300">·</span>
                            <span className="text-slate-500 truncate">{profile.industry}</span>
                          </>
                        )}
                      </div>
                      {renderHeaderExtra?.(detailCtx)}
                    </div>
                    {profile && (
                      <span className="shrink-0 flex items-center gap-1 text-[10px] text-slate-400 bg-white border border-slate-200 rounded-full px-2 py-1">
                        <Layers className="w-3 h-3" />
                        {profile.trade_date}
                      </span>
                    )}
                  </div>
                  {profile && (profile.index_membership.length > 0 || profile.concepts.length > 0) && (
                    <div className="mt-2.5 space-y-1.5 max-w-[400px]">
                      {profile.index_membership.length > 0 && (
                        <div className="flex gap-1.5 items-start">
                          <span className="text-[10px] font-bold text-slate-400 shrink-0 pt-0.5">宽基</span>
                          <div className="flex flex-wrap gap-1.5 min-w-0 flex-1">
                            {profile.index_membership.map((m) => (
                              <span key={m.index_code} className="shrink-0 text-[10px] px-2 py-0.5 rounded-full bg-violet-50 text-violet-700 border border-violet-100 font-bold">
                                {m.index_name}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                      {profile.concepts.length > 0 && (
                        <div className="flex gap-1.5 items-start">
                          <span className="text-[10px] font-bold text-slate-400 shrink-0 pt-0.5">概念</span>
                          <div className="flex flex-wrap gap-1.5 min-w-0 flex-1 max-w-[368px]">
                            {profile.concepts.map((c) => (
                              <span key={c} className="shrink-0 text-[10px] px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-100">
                                {c}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>

              {/* Tab + 详情体（占剩余全部高度） */}
              <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
                <div className="flex-1 min-h-0 overflow-y-auto p-3 bg-gray-50/30 custom-scrollbar">
                  {renderDetail(detailCtx)}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
