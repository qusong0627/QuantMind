/** 个股终端 — 搜索驱动展示：顶部搜索 + 左右布局（左 K线大图·推理分数叠右侧轴 + 右详情） */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { CandlestickChart, Search, Layers, Building2, Database, TrendingUp, TrendingDown } from 'lucide-react';
import { message, Select } from 'antd';
import { PAGE_LAYOUT } from '../../../config/pageLayout';
import { StockListItem, StockProfile, KlineBar } from '../types';
import { stockTerminalService, type KlineAdjust } from '../services/stockTerminalService';
import { modelTrainingService } from '../../../services/modelTrainingService';
import { StockSearchBar } from '../components/StockSearchBar';
import { type Point as ScorePoint } from '../components/InferenceScoreChart';
import { KlineChart, type ScoreSeries } from '../components/kline/KlineChart';
import { OverviewTab } from '../components/OverviewTab';
import { FinancialsTab, ValuationTab, ChipFlowTab, MarginTab, SentimentTab, HoldersTab } from '../components/tabs/P2Tabs';
import { NewsTab } from '../components/tabs/NewsTab';
import { L2FeatureCard } from '../components/L2FeatureCard';

type DetailTab = 'overview' | 'financials' | 'valuation' | 'chipflow' | 'margin' | 'sentiment' | 'holders' | 'news' | 'l2';

const DETAIL_TABS: { id: DetailTab; label: string }[] = [
  { id: 'overview', label: '概况' },
  { id: 'financials', label: '财务' },
  { id: 'valuation', label: '估值' },
  { id: 'chipflow', label: '筹码' },
  { id: 'margin', label: '融资' },
  { id: 'sentiment', label: '形态' },
  { id: 'holders', label: '股东' },
  { id: 'news', label: '资讯' },
  { id: 'l2', label: 'L2' },
];

const KLINE_PERIODS: { key: 'daily' | 'weekly' | 'monthly'; label: string }[] = [
  { key: 'daily', label: '日K' },
  { key: 'weekly', label: '周K' },
  { key: 'monthly', label: '月K' },
];

/** 复权方式切换项：与后端 /market/kline 的 adjust 参数对应 */
const KLINE_ADJUSTS: { key: KlineAdjust; label: string }[] = [
  { key: 'none', label: '不复权' },
  { key: 'qfq', label: '前复权' },
  { key: 'hfq', label: '后复权' },
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

export default function StockTerminalPage() {
  const [selected, setSelected] = useState<StockListItem | null>(null);
  const [profile, setProfile] = useState<StockProfile | null>(null);
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [barsLoading, setBarsLoading] = useState(false);
  const [period, setPeriod] = useState<'daily' | 'weekly' | 'monthly'>('daily');
  const [adjust, setAdjust] = useState<KlineAdjust>('qfq');
  const [signalDate, setSignalDate] = useState<string | undefined>(undefined);
  const [detailTab, setDetailTab] = useState<DetailTab>('overview');
  const [watchlist, setWatchlist] = useState<Set<string>>(new Set());
  const [scorePoints, setScorePoints] = useState<ScorePoint[]>([]);
  // 多模型切换：当前选中模型（undefined=默认模型）+ 下拉模型列表
  const [modelId, setModelId] = useState<string | undefined>(undefined);
  const [scoreModels, setScoreModels] = useState<Array<{ model_id: string; display_name?: string }>>([]);
  const [modelName, setModelName] = useState<string>('');
  const [scoreLast, setScoreLast] = useState<{ value: number; date: string; up: boolean } | null>(null);

  // 当前选中模型（默认=默认模型）分数 → 转成 K线主图右侧分数轴序列（单模型一条线）
  const scoreSeries = useMemo<ScoreSeries[]>(() => {
    if (!scorePoints.length) return [];
    return [{
      model: modelName || modelId || '默认模型',
      color: '#6366f1',
      points: scorePoints.map((p) => ({ date: p.date, fusion: p.value, side: p.side })),
    }];
  }, [scorePoints, modelName, modelId]);

  // 拉起推理分数：喂给 K线右侧分数轴 + 顶部最近分数 + 模型下拉列表。
  // 不渲染独立折线图组件，分数线叠加在 K线主图右侧分数轴（与 K线同日期对齐）。
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
          if (!cancelled) setWatchlist(new Set(resp.items.map((i) => i.symbol)));
        }),
      )
      .catch(() => {
        if (!cancelled) setWatchlist(new Set());
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSelect = useCallback((item: StockListItem) => {
    setSelected(item);
    setDetailTab('overview');
    setSignalDate(undefined);
  }, []);

  // 详情随选中+信号日联动
  useEffect(() => {
    if (!selected) {
      setProfile(null);
      return;
    }
    let cancelled = false;
    stockTerminalService.getProfile(selected.symbol, signalDate).then((p) => {
      if (!cancelled) setProfile(p);
    });
    return () => {
      cancelled = true;
    };
  }, [selected, signalDate]);

  // K线随选中+周期+复权方式联动；就近约一年（≈245 根日K）取数，统一保留最近 200 根，
  // 周/月由日K重采样。不传 days：后端只传 days 时会按 days×2 自然日回溯，区间会超出一年。
  useEffect(() => {
    if (!selected) {
      setBars([]);
      return;
    }
    let cancelled = false;
    setBarsLoading(true);
    const endD = new Date();
    const startD = new Date(endD);
    startD.setFullYear(startD.getFullYear() - 1);
    const iso = (d: Date) => d.toISOString().slice(0, 10);
    stockTerminalService
      .getDailyKline(selected.symbol, 500, adjust, iso(startD), iso(endD))
      .then((items) => {
        if (cancelled) return;
        const daily = items.slice(-200);
        if (period !== 'daily' && daily.length) {
          setBars(resampleBars(daily, period));
        } else {
          setBars(daily);
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
    return () => {
      cancelled = true;
    };
  }, [selected, period, adjust]);

  const up = (profile?.pct_change ?? selected?.pct_change ?? 0) >= 0;

  return (
    /* 底部 pb-[84px]：给悬浮 Dock 菜单栏（64px）留出空间，避免遮挡 K线图底部的缩放条 */
    <div className="w-full h-full bg-[#f8fafc] px-6 pt-6 pb-[84px] flex flex-col overflow-hidden">
      <div className={PAGE_LAYOUT.frameClass}>
        {/* 顶栏：标题 + 居中搜索框（原独立搜索行并入顶部，K线图整体上移）+ 价格/模型 */}
        <header className={PAGE_LAYOUT.headerClass} style={{ height: `${PAGE_LAYOUT.headerHeight}px` }}>
          <div className="flex items-center gap-3 min-w-0 shrink-0">
            <div className="w-10 h-10 bg-gradient-to-br from-blue-500 to-violet-500 rounded-2xl flex items-center justify-center shadow-lg shrink-0">
              <CandlestickChart className="w-5 h-5 text-white" />
            </div>
            <div className="flex items-center min-w-0">
              <h1 className="text-lg font-bold text-slate-800 tracking-tight whitespace-nowrap">个股终端</h1>
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

        {/* 主体 */}
        {!selected ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-3 bg-gray-50/50 p-8 text-center">
            <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-blue-500 to-violet-500 flex items-center justify-center shadow-md">
              <Search className="w-6 h-6 text-white" />
            </div>
            <div className="text-sm font-bold text-slate-700">在上方搜索框输入代码或名称开始</div>
            <div className="text-xs text-slate-400 max-w-[420px] leading-relaxed">
              不会预加载全量列表，输入关键词后联想最相关的 8 只股票；选中后左侧展示历史K线与默认模型推理分，右侧展示个股详情。
            </div>
            {/* 数据提示：一句话引导下载完整数据包并保持更新 */}
            <div className="flex items-center gap-1.5 text-[11px] text-slate-400">
              <Database className="w-3.5 h-3.5 shrink-0 text-slate-300" />
              <span>温馨提示：请先下载完整行情数据包并保持每日更新，否则可能搜不到标的、K线或推理分为空。</span>
            </div>
          </div>
        ) : (
          <div className="flex flex-1 min-h-0 overflow-hidden bg-gray-50/50 p-4 gap-4">
            {/* 左侧：K线大图（推理分数叠在右侧刻度，与 K线日期对齐） */}
            <div className="flex-1 min-w-0 flex flex-col overflow-hidden">
              {/* K线卡 */}
              <div className="flex-1 min-h-0 flex flex-col rounded-3xl bg-white border border-purple-100/80 shadow-sm overflow-hidden">
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
                  </div>
                  <div className="flex items-center gap-1 p-1 bg-slate-100 rounded-full shrink-0">
                    {KLINE_PERIODS.map((p) => (
                      <button
                        key={p.key}
                        onClick={() => setPeriod(p.key)}
                        className={`px-3 py-1 rounded-full text-[11px] font-bold transition-colors ${period === p.key ? 'bg-white text-blue-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
                      >
                        {p.label}
                      </button>
                    ))}
                    <div className="w-px h-4 bg-slate-200 mx-1" />
                    {KLINE_ADJUSTS.map((a) => (
                      <button
                        key={a.key}
                        onClick={() => setAdjust(a.key)}
                        className={`px-2.5 py-1 rounded-full text-[11px] font-bold transition-colors ${adjust === a.key ? 'bg-white text-blue-600 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
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
                      <KlineChart bars={bars} config={{ ma: true, boll: false, subplots: ['vol'] }} overlays={[]} period={period} scoreSeries={scoreSeries} />
                    </div>
                  ) : (
                    <div className="h-full flex items-center justify-center text-xs text-slate-400">暂无K线</div>
                  )}
                </div>
              </div>
            </div>

            {/* 右侧：详情 — 400px 定宽，上 2/5 下 3/5，概念标签横向排列、超 400 换行 */}
            <div className="w-[400px] max-w-[400px] shrink-0 flex flex-col rounded-3xl bg-white border border-slate-200/80 shadow-sm overflow-hidden">
              {/* 上方：头部 + 概念标签（纯内容高度，横向排列、超宽换行） */}
              <div className="shrink-0 flex flex-col overflow-hidden border-b border-slate-100">
                <div className="px-4 py-3 bg-slate-50/60 shrink-0">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="w-7 h-7 rounded-lg bg-blue-50 border border-blue-100 flex items-center justify-center text-blue-600 shrink-0">
                          <Building2 className="w-3.5 h-3.5" />
                        </span>
                        <span className="text-[15px] font-black text-slate-800 truncate">{profile?.name ?? selected.name}</span>
                        <span className="shrink-0 text-[10px] font-mono px-1.5 py-0.5 rounded-md bg-white border border-slate-200 text-slate-500">
                          {profile?.symbol ?? selected.symbol}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center gap-2 flex-wrap text-[11px]">
                        <span className={`font-mono font-bold ${up ? 'text-rose-500' : 'text-emerald-500'}`}>
                          {profile?.close?.toFixed(3) ?? selected.close?.toFixed(3) ?? '--'} {up ? '+' : ''}
                          {(profile?.pct_change ?? selected.pct_change ?? 0).toFixed(3)}%
                        </span>
                        <span className="text-slate-300">·</span>
                        <span className="text-slate-500">{profile?.board ?? selected.board ?? '--'}</span>
                        {profile?.industry && (
                          <>
                            <span className="text-slate-300">·</span>
                            <span className="text-slate-500 truncate">{profile.industry}</span>
                          </>
                        )}
                      </div>
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

              {/* 下方：Tab + 详情体（占剩余全部高度） */}
              <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
                <div className="px-3 py-2 border-b border-slate-100 bg-white shrink-0">
                  <div className="grid grid-cols-5 gap-1.5">
                    {DETAIL_TABS.map((t) => (
                      <button
                        key={t.id}
                        onClick={() => setDetailTab(t.id)}
                        className={`px-2 py-1.5 rounded-full text-[11px] font-bold border transition-colors ${detailTab === t.id ? 'bg-blue-600 text-white border-blue-600 shadow-sm' : 'bg-slate-50 text-slate-600 border-slate-200 hover:bg-white hover:border-slate-300'}`}
                      >
                        {t.label}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="flex-1 min-h-0 overflow-y-auto p-3 bg-gray-50/30 custom-scrollbar">
                  <div className={detailTab === 'overview' ? '[&>div]:!grid-cols-1 [&>div]:!gap-3' : ''}>
                    {detailTab === 'overview' && <OverviewTab profile={profile} />}
                    {detailTab === 'financials' && <FinancialsTab symbol={selected.symbol} asof={signalDate} />}
                    {detailTab === 'valuation' && <ValuationTab symbol={selected.symbol} asof={signalDate} />}
                    {detailTab === 'chipflow' && <ChipFlowTab symbol={selected.symbol} asof={signalDate} />}
                    {detailTab === 'margin' && <MarginTab symbol={selected.symbol} asof={signalDate} />}
                    {detailTab === 'sentiment' && <SentimentTab symbol={selected.symbol} asof={signalDate} />}
                    {detailTab === 'holders' && <HoldersTab symbol={selected.symbol} asof={signalDate} />}
                    {detailTab === 'news' && <NewsTab symbol={selected.symbol} />}
                    {detailTab === 'l2' && <L2FeatureCard l2={profile?.l2_features ?? null} signalDate={profile?.signal_date ?? null} />}
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
