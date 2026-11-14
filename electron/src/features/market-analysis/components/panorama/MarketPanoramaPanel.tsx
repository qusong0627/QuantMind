import React, { useEffect, useMemo, useState } from 'react';
import { RefreshCw, TriangleAlert } from 'lucide-react';
import type { IndexItem } from '../BroadMarketHeader';
import { HeatmapDrillCard } from '../HeatmapDrillCard';
import type { SectorHeatmapItem } from '../../../market-analysis-shared/SectorHeatmapChart';
import { fetchMarketRegime, type RegimeResponse } from '../../services/regimeService';
import { fetchMarketSentiment, type MarketSentiment } from '../../services/sentimentService';
import { getFlowPanorama, type FlowPanoramaResponse } from '../../services/moneyFlowService';
import { IndexTape } from './IndexTape';
import { StageSummaryCard } from './StageSummaryCard';
import { SentimentSummaryCard, type PanoramaBreadth } from './SentimentSummaryCard';
import { FlowSummaryCard } from './FlowSummaryCard';
import { isNum, toneBySign } from './format';

const STATE_TONE: Record<string, string> = {
  防守: 'text-amber-600',
  偏空: 'text-orange-600',
  震荡: 'text-slate-700',
  偏多: 'text-rose-600',
  风险偏好高: 'text-rose-600',
};

interface Light {
  label: string;
  value: string;
  tone: 'up' | 'down' | 'warn' | 'muted';
}

const LIGHT_TONE: Record<Light['tone'], string> = {
  up: 'border-rose-200 bg-rose-50 text-rose-600',
  down: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  warn: 'border-amber-200 bg-amber-50 text-amber-700',
  muted: 'border-slate-200 bg-slate-50 text-slate-500',
};

export function MarketPanoramaPanel({
  date,
  indices,
  breadth,
  heatmap,
  loading: pageLoading,
  snapshotMissing,
  snapshotFrom,
  onNavigate,
  onOpenStock,
}: {
  date?: string;
  indices: IndexItem[];
  breadth: PanoramaBreadth | null;
  heatmap: SectorHeatmapItem[];
  loading?: boolean;
  snapshotMissing?: boolean;
  snapshotFrom?: string;
  onNavigate: (tab: string) => void;
  onOpenStock?: (symbol: string) => void;
}): React.ReactElement {
  const [regime, setRegime] = useState<RegimeResponse | null>(null);
  const [sentiment, setSentiment] = useState<MarketSentiment | null>(null);
  const [flow, setFlow] = useState<FlowPanoramaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(0);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(0);
    Promise.allSettled([
      fetchMarketRegime(date),
      fetchMarketSentiment(date),
      getFlowPanorama({
        date,
        period: '1d',
        sectorSet: 'industry2',
        topSectors: 30,
        stocksPerSector: 3,
        direction: 'all',
      }),
    ]).then(([r, s, f]) => {
      if (cancelled) return;
      const failures = [r, s, f].filter((x) => x.status === 'rejected').length;
      setFailed(failures);
      if (r.status === 'fulfilled') setRegime(r.value);
      if (s.status === 'fulfilled') setSentiment(s.value);
      if (f.status === 'fulfilled') setFlow(f.value);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [date, tick]);

  const stress = sentiment?.stress;
  const mainNetYi = sentiment?.volume?.latest?.main_net_yi;
  const dist = sentiment?.distribution;
  const distUp = dist
    ? (dist.dist_limit_up ?? 0) + (dist.dist_up_big ?? 0) + (dist.dist_up_small ?? 0)
    : undefined;
  const distDown = dist
    ? (dist.dist_limit_down ?? 0) +
      (dist.dist_down_big ?? 0) +
      (dist.dist_down_small ?? 0)
    : undefined;
  const upCount = breadth?.advance_count ?? distUp;
  const downCount = breadth?.decline_count ?? distDown;
  const state = regime?.summary.state ?? '—';
  const score = regime?.summary.score;

  const lights = useMemo<Light[]>(() => {
    const list: Light[] = [
      {
        label: '顶风险',
        value: regime?.top_risk.flag ? '有' : regime ? '无' : '—',
        tone: regime?.top_risk.flag ? 'warn' : 'muted',
      },
      {
        label: '底部',
        value: regime?.bottom.flag ? '成立' : regime ? '无' : '—',
        tone: regime?.bottom.flag ? 'up' : 'muted',
      },
      {
        label: '情绪',
        value: stress?.zone_label ?? '—',
        tone:
          stress?.zone === 'extreme_pressure'
            ? 'down'
            : stress?.zone === 'pressure'
              ? 'warn'
              : stress?.zone === 'overheat'
                ? 'up'
                : 'muted',
      },
      {
        label: '主力',
        value: isNum(mainNetYi) ? (mainNetYi >= 0 ? '净流入' : '净流出') : '—',
        tone: isNum(mainNetYi) ? (mainNetYi >= 0 ? 'up' : 'down') : 'muted',
      },
    ];
    return list;
  }, [regime, stress, mainNetYi]);

  const oneLiner = useMemo(() => {
    const parts = [regime?.summary.one_liner];
    if (stress?.zone_label) parts.push(`情绪${stress.zone_label}`);
    if (isNum(mainNetYi)) parts.push(`主力${mainNetYi >= 0 ? '净流入' : '净流出'}`);
    if (isNum(upCount) && isNum(downCount)) {
      parts.push(`涨${upCount}/跌${downCount}`);
    }
    if (isNum(breadth?.profit_effect)) {
      parts.push(`赚钱效应 ${breadth.profit_effect.toFixed(1)}`);
    }
    return parts.filter(Boolean).join('；');
  }, [regime, stress, mainNetYi, breadth, upCount, downCount]);

  return (
    <div className="flex min-h-full flex-col gap-3">
      <div className="relative shrink-0 overflow-hidden rounded-2xl border border-slate-200/80 bg-gradient-to-br from-indigo-50/90 via-white to-rose-50/80 px-5 py-3.5 shadow-sm">
        <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-indigo-400 via-purple-400 to-rose-400" />
        <div
          className="pointer-events-none absolute inset-0 opacity-50"
          style={{
            backgroundImage:
              'linear-gradient(rgba(100,116,139,0.07) 1px, transparent 1px), linear-gradient(90deg, rgba(100,116,139,0.07) 1px, transparent 1px)',
            backgroundSize: '24px 24px',
          }}
        />
        <div
          className="pointer-events-none absolute -right-20 -top-24 h-56 w-56 rounded-full blur-3xl"
          style={{ background: 'radial-gradient(circle, rgba(124,58,237,0.14), transparent 70%)' }}
        />
        <div className="relative flex flex-wrap items-center gap-x-6 gap-y-3">
          <div className="min-w-[170px]">
            <div className="text-[9px] font-bold tracking-[0.28em] text-slate-400">
              MARKET PANORAMA
            </div>
            <div className="mt-0.5 flex items-baseline gap-2">
              <span className={`text-2xl font-black tracking-tight ${STATE_TONE[state] ?? 'text-slate-700'}`}>
                {state}
              </span>
              <span className={`font-mono text-xs font-bold ${toneBySign(score)}`}>
                {isNum(score) ? `${score > 0 ? '+' : ''}${score}` : '—'}
                <span className="text-[9px] font-normal text-slate-400"> / ±12</span>
              </span>
            </div>
            <div className="mt-0.5 text-[9px] font-mono text-slate-400">
              {regime?.date ?? date ?? '最新'} · {regime?.universe_label ?? '全A等权'}
            </div>
          </div>

          <div className="min-w-[240px] flex-1 text-[11px] leading-relaxed text-slate-600">
            {oneLiner || '正在汇总市场阶段、大盘情绪与资金流…'}
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {lights.map((l) => (
              <span
                key={l.label}
                className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-1 text-[9px] font-bold ${LIGHT_TONE[l.tone]}`}
              >
                <i className="h-1 w-1 rounded-full bg-current opacity-70" />
                <span className="opacity-70">{l.label}</span>
                <span>{l.value}</span>
              </span>
            ))}
            {snapshotMissing && (
              <span
                className="inline-flex items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-1.5 py-1 text-[9px] font-bold text-amber-700"
                title={`该日期无行情快照：阶段/情绪/资金流为历史回看；指数带、广度、方块图仅支持 ${snapshotFrom ?? '快照日'} 起的快照日`}
              >
                <TriangleAlert className="h-2.5 w-2.5" />
                无行情快照
              </span>
            )}
            {failed > 0 && (
              <span
                className="inline-flex items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-1.5 py-1 text-[9px] font-bold text-amber-700"
                title={`${failed} 个数据源暂不可用`}
              >
                <TriangleAlert className="h-2.5 w-2.5" />
                {failed} 源缺失
              </span>
            )}
            <button
              type="button"
              onClick={() => setTick((t) => t + 1)}
              className="inline-flex h-6 w-6 items-center justify-center rounded-md border border-slate-200 bg-white/80 text-slate-500 transition hover:bg-slate-50"
              title="重新汇总"
            >
              <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>
      </div>

      <div className="shrink-0">
        <IndexTape
          indices={indices}
          loading={pageLoading}
          note={
            snapshotMissing
              ? `该日期无行情快照：指数带仅支持 ${snapshotFrom ?? '快照日'} 起；阶段/情绪/资金流仍按该日期回看。`
              : undefined
          }
        />
      </div>

      <div className="grid min-h-[520px] flex-1 grid-cols-1 gap-3 lg:grid-cols-12">
        <div className="min-h-0 lg:col-span-6 xl:col-span-5">
          <StageSummaryCard
            regime={regime}
            loading={loading}
            onOpen={() => onNavigate('regime')}
          />
        </div>
        <div className="min-h-0 lg:col-span-6 xl:col-span-4">
          <SentimentSummaryCard
            sentiment={sentiment}
            breadth={breadth}
            loading={loading}
            onOpen={() => onNavigate('sentiment')}
          />
        </div>
        <div className="min-h-0 lg:col-span-12 xl:col-span-3">
          <FlowSummaryCard
            sentiment={sentiment}
            flow={flow}
            loading={loading}
            onOpen={() => onNavigate('flow-panorama')}
          />
        </div>
      </div>

      <div className="shrink-0">
        {snapshotMissing ? (
          <div className="rounded-2xl border border-dashed border-amber-200 bg-amber-50/50 px-4 py-5 text-[11px] leading-relaxed text-amber-700">
            该日期无板块热力快照，方块图仅支持有行情快照的日期（{snapshotFrom ?? '快照日'} 起）。
            阶段、情绪、资金流三张卡仍按所选日期回看；切回快照日即可查看板块方块图与个股下钻。
          </div>
        ) : (
          <div className="h-[400px] [@media(min-height:1000px)]:h-[460px]">
            <HeatmapDrillCard
              topItems={heatmap}
              date={date}
              onStockClick={(symbol) => onOpenStock?.(symbol)}
            />
          </div>
        )}
      </div>
    </div>
  );
}
