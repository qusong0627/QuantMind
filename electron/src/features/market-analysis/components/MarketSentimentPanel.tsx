import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, AlertTriangle, CalendarDays, Gauge, Info, RefreshCw, TrendingDown, TrendingUp } from 'lucide-react';
import { DatePicker } from 'antd';
import dayjs from 'dayjs';
import {
  fetchMarketSentiment,
  type IndexSentiment,
  type MarketSentiment,
} from '../services/sentimentService';
import { SentimentFanChart } from './SentimentFanChart';
import { BreadthFactorBars } from './BreadthFactorBars';
import { BreadthDistributionBar } from './BreadthDistributionBar';
import { PredictionReview } from './PredictionReview';
import { FactorHeatmap } from './FactorHeatmap';
import { BreadthGlossaryModal } from './BreadthGlossaryModal';

/** 展示顺序与后端 `TARGETS` 一致：三个老指数 + 深证成指 + 创业板指 + 全A合成。 */
const INDEX_ORDER = ['000001.SH', '399001.SZ', '399006.SZ', '000300.SH', '000852.SH', 'ALL.A'] as const;
const INDEX_COLORS: Record<string, string> = {
  '000001.SH': '#6366f1',
  '399001.SZ': '#8b5cf6',
  '399006.SZ': '#ec4899',
  '000300.SH': '#0ea5e9',
  '000852.SH': '#f59e0b',
  'ALL.A': '#14b8a6',
};

const ZONE_STYLE: Record<
  string,
  { bg: string; text: string; border: string; dot: string; label: string }
> = {
  extreme_pressure: {
    bg: 'bg-emerald-50', text: 'text-emerald-700', border: 'border-emerald-200',
    dot: 'bg-emerald-500', label: '极端压力',
  },
  pressure: {
    bg: 'bg-teal-50', text: 'text-teal-700', border: 'border-teal-200',
    dot: 'bg-teal-500', label: '承压',
  },
  neutral: {
    bg: 'bg-slate-50', text: 'text-slate-600', border: 'border-slate-200',
    dot: 'bg-slate-400', label: '中性',
  },
  strong: {
    bg: 'bg-amber-50', text: 'text-amber-700', border: 'border-amber-200',
    dot: 'bg-amber-500', label: '偏强',
  },
  overheat: {
    bg: 'bg-red-50', text: 'text-red-700', border: 'border-red-200',
    dot: 'bg-red-500', label: '过热',
  },
};

const zoneStyle = (zone: string) => ZONE_STYLE[zone] ?? ZONE_STYLE.neutral;

const pctText = (v: number | null, digits = 1): string =>
  v === null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`;

/** 方向徽章只描述「历史同状态」的中位与胜率，不构成预测承诺。 */
function biasOf(stats: IndexSentiment['stats']): { label: string; tone: string; hint: string } {
  if (stats.win >= 0.55 && stats.median > 0.3) {
    return { label: '同类情形偏多', tone: 'text-red-600 bg-red-50 border-red-200', hint: '典型涨跌为正、上涨概率过半' };
  }
  if (stats.win <= 0.45 && stats.median < -0.3) {
    return { label: '同类情形偏空', tone: 'text-emerald-600 bg-emerald-50 border-emerald-200', hint: '典型涨跌为负、上涨概率不足一半' };
  }
  return { label: '同类情形中性', tone: 'text-slate-600 bg-slate-50 border-slate-200', hint: '方向不明确，区间宽度才是主要信息' };
}

/** 回看日期写进 hash 查询串（#/market-analysis?asof=YYYY-MM-DD），可分享/可收藏。
 *  用 replaceState 而不是改 location.hash —— HashRouter 监听 hashchange，
 *  replaceState 不触发导航，不会把页面弹回首页。 */
const ASOF_RE = /^\d{4}-\d{2}-\d{2}$/;

function readAsOfFromUrl(): string | undefined {
  const hash = window.location.hash || '';
  const q = hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : '';
  const v = new URLSearchParams(q).get('asof');
  return v && ASOF_RE.test(v) ? v : undefined;
}

function writeAsOfToUrl(value?: string): void {
  const hash = window.location.hash || '';
  const base = hash.split('?')[0];
  const next = value ? `${base}?asof=${value}` : base;
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${next}`);
}

const Skeleton: React.FC = () => (
  <div className="animate-pulse space-y-3">
    <div className="h-6 w-56 bg-slate-100 rounded" />
    <div className="h-64 bg-slate-50 rounded-2xl" />
  </div>
);

export const MarketSentimentPanel: React.FC = () => {
  const [data, setData] = useState<MarketSentiment | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [indexKey, setIndexKey] = useState<string>('000300.SH');
  const [asOf, setAsOfState] = useState<string | undefined>(() => readAsOfFromUrl());
  const [glossaryOpen, setGlossaryOpen] = useState(false);
  const [mainForceGlossaryOpen, setMainForceGlossaryOpen] = useState(false);
  const [retailGlossaryOpen, setRetailGlossaryOpen] = useState(false);
  const [fanSolo, setFanSolo] = useState(false);
  const [volumeMode, setVolumeMode] = useState<'market' | 'main' | 'retail'>('market');
  const [fanCompare, setFanCompare] = useState(false);
  const setAsOf = useCallback((value?: string) => {
    setAsOfState(value);
    writeAsOfToUrl(value);
  }, []);

  const load = useCallback(async (date?: string) => {
    setLoading(true);
    setError(null);
    try {
      const next = await fetchMarketSentiment(date);
      setData(next);
      if (!next.indices[indexKey]) {
        const first = INDEX_ORDER.find((k) => next.indices[k]) ?? Object.keys(next.indices)[0];
        if (first) setIndexKey(first);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '大盘情绪数据加载失败');
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void load(asOf);
  }, [asOf, load]);

  const selected = data?.indices[indexKey] ?? null;
  const bias = selected ? biasOf(selected.stats) : null;

  if (loading && !data) {
    return <div className="bg-white rounded-2xl border border-slate-200 p-5"><Skeleton /></div>;
  }

  if (error && !data) {
    return (
      <div className="bg-white rounded-2xl border border-amber-200 p-6 flex items-start gap-3">
        <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <div className="text-sm font-bold text-slate-800">大盘情绪快照不可用</div>
          <div className="text-xs text-slate-500">{error}</div>
          <code className="block mt-2 text-[11px] bg-slate-50 border border-slate-200 rounded-lg px-2 py-1.5 font-mono text-slate-600">
            python -m backend.scripts.market_sentiment_snapshot
          </code>
        </div>
      </div>
    );
  }

  if (!data || !selected) return null;

  /* 分市场（2026-10-28）：每个指数用自己的广度宇宙算压力分；旧快照没有
     per-index 字段时回落到全A口径，保证面板不因数据版本白屏。 */
  const stress = selected.stress ?? data.stress;
  const indicators = selected.indicators ?? data.indicators;
  const stressHistory = selected.stress_history ?? data.stress_history;
  const factorHistory = selected.factor_history ?? data.factor_history;
  const dispersion = selected.dispersion ?? data.dispersion;
  const universe = selected.universe;
  const zone = zoneStyle(stress.zone);
  const score = stress.score;
  const percentile =
    typeof stress.percentile === 'number' ? Math.round(stress.percentile * 100) : null;
  const stats = selected.stats;
  const hasStats = typeof stats?.win === 'number';

  const actualPts = selected.actual ?? [];
  const actualK = actualPts.length ? actualPts[actualPts.length - 1].k : 0;
  const actualPct = actualPts.length ? actualPts[actualPts.length - 1].pct : null;
  // 不足 20 天时用「已走到的那一天」的扇形区间对照，别拿 T+20 的区间判 T+9 的结果
  const bandPoint =
    actualK > 0 ? selected.fan[Math.min(actualK, selected.fan.length - 1)] : null;
  const verdict = (() => {
    if (actualPct === null || !hasStats || !bandPoint) return null;
    const inBand = actualPct >= bandPoint.p10 && actualPct <= bandPoint.p90;
    const sameDir = (actualPct >= 0) === (bandPoint.p50 >= 0);
    const tone =
      inBand && sameDir
        ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
        : inBand
          ? 'border-amber-200 bg-amber-50 text-amber-800'
          : 'border-red-200 bg-red-50 text-red-800';
    const text =
      inBand && sameDir
        ? `落在当时的八成区间内，方向也一致（当时典型 ${pctText(bandPoint.p50)}）`
        : inBand
          ? `落在当时的八成区间内，但方向与当时典型相反（当时典型 ${pctText(bandPoint.p50)}）`
          : `跑出当时的八成区间 ${pctText(bandPoint.p10)} ~ ${pctText(bandPoint.p90)}`;
    return { tone, text, inBand, sameDir };
  })();

  return (
    <div className="flex flex-col gap-3">
      {/* 回看日期 */}
      <div className="flex items-center justify-between gap-3 flex-wrap bg-white rounded-xl border border-slate-200 px-3 py-2">
        <div className="flex items-center gap-2">
          <CalendarDays className="w-3.5 h-3.5 text-violet-600" />
          <span className="text-[11px] font-bold text-slate-600">回看日期</span>
          <DatePicker
            size="small"
            allowClear
            value={asOf ? dayjs(asOf) : null}
            placeholder="最新"
            className="!w-36 sentiment-asof-picker"
            onChange={(d) => setAsOf(d ? d.format('YYYY-MM-DD') : undefined)}
            disabledDate={(d) => {
              // 快照缺边界时一律可选（否则 dayjs(undefined)=今天，会把所有历史日期锁死）
              if (!data.available_from || !data.available_to) return false;
              return d.isAfter(dayjs(data.available_to)) || d.isBefore(dayjs(data.available_from));
            }}
          />
          {stress.phase && (
            <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-violet-50 border border-violet-200 text-[11px] font-extrabold text-violet-700">
              {selected.name}：{stress.phase}
            </span>
          )}
          {(asOf || data.as_of) && (
            <button
              onClick={() => {
                // asOf 有值时走 effect 重取；没有值时（服务端返回回看态）直接拉最新
                const hadAsOf = Boolean(asOf);
                setAsOf(undefined);
                if (!hadAsOf) void load(undefined);
              }}
              className="text-[11px] font-bold text-violet-600 hover:underline"
            >
              回到最新
            </button>
          )}
        </div>
        <span className={`text-[11px] font-bold ${data.as_of ? 'text-amber-600' : 'text-slate-400'}`}>
          {data.as_of
            ? `回看模式 · 信号只用到 ${data.trade_date}（之后的走势仅作对照）`
            : `最新数据 ${data.trade_date}`}
        </span>
      </div>

      {/* 分指数压力：同一算法、各自宇宙 —— 一眼看出「指数涨、多数票跌」这类分歧 */}
      <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-2">
        {INDEX_ORDER.filter((k) => data.indices[k]).map((k) => {
          const it = data.indices[k];
          const st = it.stress ?? data.stress;
          const z = zoneStyle(st.zone);
          const active = k === indexKey;
          const up = (it.change_pct ?? 0) >= 0;
          return (
            <button
              key={k}
              onClick={() => setIndexKey(k)}
              className={`text-left rounded-xl border px-2.5 py-2 transition-all ${
                active
                  ? 'bg-white border-violet-300 shadow-sm ring-1 ring-violet-100'
                  : 'bg-white/70 border-slate-200 hover:border-slate-300'
              }`}
            >
              <div className="flex items-center justify-between gap-1">
                <span className="text-[11px] font-bold text-slate-700 truncate">{it.name}</span>
                <span className={`text-[10px] font-mono shrink-0 ${up ? 'text-red-500' : 'text-emerald-600'}`}>
                  {it.change_pct === null ? '—' : `${up ? '+' : ''}${it.change_pct.toFixed(2)}%`}
                </span>
              </div>
              <div className="flex items-baseline gap-1 mt-0.5">
                <span className="text-xl font-black font-mono leading-none text-slate-900 tabular-nums">
                  {st.score.toFixed(1)}
                </span>
                <span className={`text-[10px] font-bold ${z.text}`}>{z.label}</span>
                <span className="ml-auto text-[9px] font-mono text-slate-400" title="这种压力在过去历史中的分位">
                  {typeof st.percentile === 'number' ? `P${Math.round(st.percentile * 100)}` : '—'}
                </span>
              </div>
              {it.forecast && (
                <div
                  className="grid grid-cols-3 gap-0.5 mt-1 pt-1 border-t border-slate-100"
                  title="多周期模型预测（广度 + 量能）；概率≈50% ＝ 没有方向优势"
                >
                  {[5, 10, 20].map((h) => {
                    const f = it.forecast?.[String(h)];
                    const p = f?.pred ?? null;
                    return (
                      <div key={h} className="text-center leading-tight">
                        <div className="text-[8px] text-slate-400 font-mono">T+{h}</div>
                        <div
                          className={`text-[9px] font-mono font-extrabold ${
                            p === null
                              ? 'text-slate-300'
                              : p >= 0
                                ? 'text-red-600'
                                : 'text-emerald-600'
                          }`}
                        >
                          {p === null ? '—' : `${p > 0 ? '+' : ''}${p.toFixed(2)}%`}
                        </div>
                        <div className="text-[8px] text-slate-400">
                          涨{f?.prob_up == null ? '—' : Math.round(f.prob_up * 100)}%
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </button>
          );
        })}
      </div>

      {/* 顶部：分数 + 扇形 */}
      <div className="grid grid-cols-1 xl:grid-cols-[300px_1fr] gap-3 items-stretch">
        {/* 左栏：压力分 */}
        <aside className="bg-white rounded-2xl border border-slate-200 shadow-sm p-4 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
              <Gauge className="w-3.5 h-3.5 text-violet-600" />
              广度压力分 · {selected.name}
            </h3>
            <button
              onClick={() => void load(asOf)}
              className="p-1 rounded-lg text-slate-400 hover:text-violet-600 hover:bg-violet-50 transition-colors"
              title="刷新"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>

          <div className="flex items-end gap-2">
            <span className="text-5xl font-black font-mono leading-none text-slate-900 tabular-nums">
              {score.toFixed(1)}
            </span>
            <span className="text-sm text-slate-400 font-mono mb-1">/100</span>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-[11px] font-extrabold ${zone.bg} ${zone.text} ${zone.border}`}>
              <span className={`w-1.5 h-1.5 rounded-full ${zone.dot}`} />
              {zone.label}
            </span>
            <span className="text-[11px] text-slate-500 font-mono">
              {percentile === null ? '历史样本不足' : `比历史 ${percentile}% 的交易日更冷`}
            </span>
          </div>

          {/* 连续量表 */}
          <div className="pt-1">
            <div
              className="relative h-2.5 rounded-full"
              style={{
                background:
                  // 左端 0=过热（红）、右端 100=极端压力（绿），与 A 股红涨绿跌及区间徽章同向
                  'linear-gradient(90deg,#ef4444 0%,#f59e0b 25%,#94a3b8 50%,#14b8a6 75%,#10b981 100%)',
              }}
            >
              <div
                className="absolute -top-[5px] w-4 h-4 rounded-full bg-white border-2 border-slate-900 shadow-md transition-all"
                style={{ left: `calc(${Math.min(Math.max(score, 0), 100)}% - 8px)` }}
              />
            </div>
            <div className="mt-1.5 flex justify-between text-[9px] font-bold text-slate-400">
              <span className="text-red-500">0 过热</span>
              <span>40</span>
              <span>60</span>
              <span className="text-emerald-600">100 极压</span>
            </div>
          </div>

          <div className="text-[11px] leading-relaxed text-slate-500 border-t border-slate-100 pt-2 space-y-1">
            <p>分数越高 ＝ 越多股票在跌、市场越冷；<span className="font-bold text-slate-700">80 以上属于极端超卖区</span>。</p>
            <p className="text-slate-400">{stress.phase_detail ?? stress.zone_note}</p>
          </div>

          {typeof dispersion?.value === 'number' && (
            <p className="text-[10px] text-slate-500 border-t border-slate-100 pt-2">
              个股分化度 <span className="font-mono font-bold text-slate-700">{dispersion.value.toFixed(2)}%</span>
              {typeof dispersion.pct === 'number' && (
                <span className="text-slate-400"> · 高于历史 {Math.round(dispersion.pct * 100)}% 的时间（越高＝个股走势越分化）</span>
              )}
            </p>
          )}

          {(selected.distribution ?? data.distribution) && (
            <div className="border-t border-slate-100 pt-2">
              <BreadthDistributionBar
                dist={selected.distribution ?? data.distribution}
                total={universe?.n_last}
                date={data.trade_date}
              />
            </div>
          )}

          <div className="border-t border-slate-100 pt-2">
            <div className="flex items-center justify-between mb-1">
              <span className="text-[10px] font-extrabold text-slate-600">广度分项 · 冷热</span>
              <button
                onClick={() => setGlossaryOpen(true)}
                className="text-[9px] font-bold text-violet-600 hover:underline"
              >
                指标说明 ?
              </button>
            </div>
            <BreadthFactorBars items={indicators} compact />
          </div>

          {data.main_force?.indicators?.length ? (
            <div className="border-t border-slate-100 pt-2">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] font-extrabold text-slate-600">主力视角 · 冷热</span>
                <button
                  onClick={() => setMainForceGlossaryOpen(true)}
                  className="text-[9px] font-bold text-violet-600 hover:underline"
                >
                  指标说明 ?
                </button>
              </div>
              <p className="text-[9px] text-slate-400 leading-snug mb-0.5">
                主力=超大单+大单（不是散户）。强度=主力净额/成交额；广度=主力净买入的股票占比；额广度=按成交额加权，更接近主力视角。
              </p>
              <BreadthFactorBars items={data.main_force.indicators} compact />
              <p className="text-[9px] text-slate-400 mt-1">{data.main_force.disclosure}</p>
            </div>
          ) : null}

          {data.retail?.indicators?.length ? (
            <div className="border-t border-slate-100 pt-2">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] font-extrabold text-slate-600">散户视角 · 冷热</span>
                <button
                  onClick={() => setRetailGlossaryOpen(true)}
                  className="text-[9px] font-bold text-violet-600 hover:underline"
                >
                  指标说明 ?
                </button>
              </div>
              <div className={`rounded-lg border px-2 py-1 mb-1 ${
                data.retail.verdict.tone === 'warn'
                  ? 'border-amber-200 bg-amber-50 text-amber-800'
                  : data.retail.verdict.tone === 'good'
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                    : 'border-slate-200 bg-slate-50 text-slate-600'
              }`}>
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] font-extrabold">{data.retail.verdict.label}</span>
                  <span className="text-[9px] font-mono opacity-70">{data.retail.score == null ? '—' : data.retail.score.toFixed(0)}/100</span>
                </div>
                <div className="text-[9px] leading-snug">{data.retail.verdict.hint}</div>
                {data.retail.score != null && (
                  <div className="relative h-1.5 mt-1 rounded-full bg-gradient-to-r from-emerald-500 via-slate-300 to-red-500">
                    <span
                      className="absolute top-1/2 -translate-y-1/2 h-2 w-2 rounded-full border border-white bg-slate-800 shadow"
                      style={{ left: `calc(${Math.min(100, Math.max(0, data.retail.score))}% - 4px)` }}
                    />
                  </div>
                )}
              </div>
              <BreadthFactorBars items={data.retail.indicators} compact />
              <p className="text-[9px] text-slate-400 mt-1">{data.retail.disclosure}</p>
            </div>
          ) : null}

          {universe && (
            <p className="text-[10px] text-slate-400 border-t border-slate-100 pt-2 leading-relaxed">
              广度口径：{universe.note}
              {universe.mode === 'rank' && ' · 用前一交易日流通市值排名，无前视'}
            </p>
          )}
        </aside>

        {/* 右栏：扇形预测 */}
        <section className="bg-white rounded-2xl border border-slate-200 shadow-sm p-4 flex flex-col gap-2 min-w-0">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-xs font-extrabold text-slate-800 flex items-center gap-1.5">
                <Activity className="w-3.5 h-3.5 text-violet-600" />
                未来 {data.horizon} 个交易日 · 同类情形的可能走势
              </h3>
              {bias && (
                <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-extrabold ${bias.tone}`}>
                  {selected.stats.median >= 0 ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                  {bias.label}
                </span>
              )}
            </div>
            <div className="flex items-center gap-1.5">
              <div className="flex rounded-full bg-slate-100 p-0.5">
                <button
                  onClick={() => setVolumeMode('market')}
                  className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold ${
                    volumeMode === 'market' ? 'bg-white text-slate-800 shadow-2xs' : 'text-slate-500'
                  }`}
                  title="成交量柱=所选指数/宇宙的成交额"
                >
                  大盘柱
                </button>
                <button
                  onClick={() => setVolumeMode('main')}
                  className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold ${
                    volumeMode === 'main' ? 'bg-white text-slate-800 shadow-2xs' : 'text-slate-500'
                  }`}
                  title="成交量柱=全市场主力净额（超大单+大单）"
                >
                  主力柱
                </button>
                <button
                  onClick={() => setVolumeMode('retail')}
                  className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold ${
                    volumeMode === 'retail' ? 'bg-white text-slate-800 shadow-2xs' : 'text-slate-500'
                  }`}
                  title="成交量柱=全市场散户净额（中单+小单）"
                >
                  散户柱
                </button>
              </div>
              <button
                onClick={() => setFanSolo((v) => !v)}
                className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold border transition-colors ${
                  fanSolo
                    ? 'bg-violet-600 text-white border-violet-600'
                    : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'
                }`}
                title="隐藏成交量/压力副图，只保留扇形主图"
              >
                {fanSolo ? '恢复副图' : '单看走势'}
              </button>
              <button
                onClick={() => setFanCompare((v) => !v)}
                className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold border transition-colors ${
                  fanCompare
                    ? 'bg-slate-800 text-white border-slate-800'
                    : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'
                }`}
                title="叠加其它指数的历史同状态中位路径"
              >
                {fanCompare ? '隐藏对比' : '对比指数'}
              </button>
              {universe && (
                <span
                  className="px-2 py-0.5 rounded-full border border-slate-200 bg-slate-50 text-[10px] font-bold text-slate-500"
                  title={universe.note}
                >
                  广度口径：{universe.label}
                  {universe.n_last ? ` · ${universe.n_last} 只` : ''}
                </span>
              )}
            </div>
          </div>

          <SentimentFanChart
            history={selected.history}
            fan={selected.fan}
            last={selected.last}
            horizon={data.horizon}
            actual={actualPts}
            forwardDates={selected.future_dates ?? []}
            stress={fanSolo ? [] : selected.history
              .map((h) => {
                const hit = stressHistory.find((p) => p.date === h.date);
                return hit ? { date: h.date, stress: hit.stress } : null;
              })
              .filter((v): v is { date: string; stress: number } => v !== null)}
            volume={
              fanSolo
                ? undefined
                : volumeMode === 'main' && data.volume
                  ? { ...data.volume, scope: '全市场', barMode: 'main' as const }
                  : volumeMode === 'retail' && data.volume
                    ? { ...data.volume, scope: '全市场', barMode: 'retail' as const }
                    : selected.volume ?? data.volume
            }
            compare={fanSolo || !fanCompare ? [] : INDEX_ORDER.filter((k) => k !== indexKey && data.indices[k]).map((k) => ({
              name: data.indices[k].name,
              color: INDEX_COLORS[k] ?? '#94a3b8',
              p50: data.indices[k].fan.map((p) => p.p50),
            }))}
            onPickDate={(d) => {
              // 单击历史某天 = 回看当天：扇形、模型预测、实际走势全部重锚到那一天
              if (!data.as_of && d === data.trade_date) {
                setAsOf(undefined);
                return;
              }
              setAsOf(d);
            }}
          />

          {data.as_of && verdict && (
            <div className={`rounded-xl border px-3 py-2 flex items-center gap-3 ${verdict.tone}`}>
              <div className="text-2xl font-black font-mono tabular-nums leading-none">
                {actualPct! > 0 ? '+' : ''}
                {actualPct!.toFixed(2)}%
              </div>
              <div className="min-w-0">
                <div className="text-[11px] font-extrabold">
                  之后 {actualK} 天实际走势
                  {actualK < data.horizon && (
                    <span className="font-normal text-slate-500"> · 已走 {actualK}/{data.horizon} 天</span>
                  )}
                </div>
                <div className="text-[10px] leading-snug">{verdict.text}</div>
              </div>
              <div className="ml-auto text-[10px] font-bold shrink-0">
                {verdict.inBand ? (verdict.sameDir ? '✓ 区间与方向都对' : '△ 区间对、方向反了') : '✗ 跑出区间'}
              </div>
            </div>
          )}

          <PredictionReview block={selected} asOf={data.as_of} horizon={data.horizon} />

          {!hasStats && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
              该状态下历史样本不足（有效路径少于 20 条），暂不给出条件统计 —— 不拿 0 冒充。
            </div>
          )}
          {hasStats && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-1">
            <div className="rounded-xl bg-slate-50 border border-slate-100 px-3 py-2">
              <div className="text-[10px] text-slate-400 font-bold">典型涨跌</div>
              <div className={`text-lg font-extrabold font-mono ${selected.stats.median >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                {pctText(selected.stats.median)}
              </div>
            </div>
            <div className="rounded-xl bg-slate-50 border border-slate-100 px-3 py-2">
              <div className="text-[10px] text-slate-400 font-bold">上涨概率</div>
              <div className="text-lg font-extrabold font-mono text-slate-800">
                {(selected.stats.win * 100).toFixed(0)}%
                <span className="text-[10px] text-slate-400 font-normal ml-1">平时 {(selected.baseline.win * 100).toFixed(0)}%</span>
              </div>
            </div>
            <div className="rounded-xl bg-slate-50 border border-slate-100 px-3 py-2">
              <div className="text-[10px] text-slate-400 font-bold">八成情况落在</div>
              <div className="text-lg font-extrabold font-mono text-slate-800">
                {pctText(selected.stats.p10)} ~ {pctText(selected.stats.p90)}
              </div>
            </div>
            <div className="rounded-xl bg-slate-50 border border-slate-100 px-3 py-2">
              <div className="text-[10px] text-slate-400 font-bold">大回撤概率（≥10%）</div>
              <div className="text-lg font-extrabold font-mono text-slate-800">
                {(selected.stats.p_drop10 * 100).toFixed(1)}%
                <span className="text-[10px] text-slate-400 font-normal ml-1">平时 {(selected.baseline.p_drop10 * 100).toFixed(1)}%</span>
              </div>
            </div>
          </div>
          )}

          <div className="flex items-center justify-between gap-3 flex-wrap pt-1 text-[11px] text-slate-500">
            <span className="font-mono">
              历史同类情形 {selected.stats.n_clusters} 次 · 平均涨跌 {pctText(selected.baseline.mean, 2)}（平时水平）
            </span>
            {bias && <span className="text-slate-400">{bias.hint}</span>}
          </div>
        </section>
      </div>

      {/* 底部：分项冷热热力图 */}
      <div className="bg-white rounded-2xl border border-slate-200 shadow-sm px-4 py-3">
        <FactorHeatmap dates={factorHistory?.dates ?? []} items={factorHistory?.items ?? []} />
      </div>

      <BreadthGlossaryModal
        open={glossaryOpen}
        onClose={() => setGlossaryOpen(false)}
        items={indicators}
      />

      <BreadthGlossaryModal
        open={mainForceGlossaryOpen}
        onClose={() => setMainForceGlossaryOpen(false)}
        items={data.main_force?.indicators ?? []}
        keys={['main_net_ratio', 'main_breadth', 'main_breadth_amt', 'vw_breadth']}
        title="主力视角 · 这些数字是什么意思"
        intro="主力=超大单+大单（不是中单/小单的散户资金）。这里看的是主力净额强度、主力覆盖广度，以及按成交额加权的资金方向。"
        footer="主力视角只描述当前资金结构，不构成方向预测；冷热条方向已统一，越靠右越冷。L2 资金流自 2022 年起可用。"
      />

      <BreadthGlossaryModal
        open={retailGlossaryOpen}
        onClose={() => setRetailGlossaryOpen(false)}
        items={data.retail?.indicators ?? []}
        keys={['retail_chase_corr', 'retail_ratio', 'retail_breadth_amt', 'retail_divergence']}
        title="散户视角 · 怎么识别散户情绪、怎么做反向"
        intro="散户=中单+小单。不是所有散户指标都能反向：经 2022-2026 验证，只有「散户追涨相关」有稳定反向价值；散户净流入/广度更像同步动量。"
        footer="散户追涨相关 >0.25：散户在追近期赢家，之后偏弱（反向警惕）；<0.10：散户不追/回避，之后偏强（反向关注）。历史统计，不是未来保证。"
      />

      {/* 免责 */}
      <div className="flex items-start gap-2 text-[11px] text-slate-400 px-1">
        <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" />
        <p className="leading-relaxed">
          扇形是「历史上和现在同样冷的时候，之后 20 天都怎么走」的分布，不是对未来的点预测；区间越宽表示越说不准。
          这类指标判断方向的能力本来就弱，本面板用于识别市场冷热与风险，不构成投资建议。
        </p>
      </div>
    </div>
  );
};
