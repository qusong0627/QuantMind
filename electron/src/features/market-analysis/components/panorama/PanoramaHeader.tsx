import React from 'react';
import { RefreshCw, TriangleAlert } from 'lucide-react';
import type { RegimeResponse } from '../../services/regimeService';
import type { MarketSentiment } from '../../services/sentimentService';
import type { PanoramaBreadth } from './SentimentSummaryCard';
import { clamp, fmtCount, fmtYi, isNum, toneBySign } from './format';

const STATE_TONE: Record<string, string> = {
  防守: 'text-amber-600',
  偏空: 'text-orange-600',
  震荡: 'text-slate-700',
  偏多: 'text-rose-600',
  风险偏好高: 'text-rose-600',
};

const SCORE_ZONES = [
  { from: -12, to: -5, label: '防守', color: '#059669' },
  { from: -5, to: -1, label: '偏空', color: '#34d399' },
  { from: -1, to: 1, label: '震荡', color: '#cbd5e1' },
  { from: 1, to: 5, label: '偏多', color: '#fb7185' },
  { from: 5, to: 12, label: '风险偏好高', color: '#e11d48' },
];

function ScoreGauge({ score }: { score: number | null }): React.ReactElement {
  const v = isNum(score) ? clamp(score, -12, 12) : null;
  const pct = v === null ? 50 : ((v + 12) / 24) * 100;
  return (
    <div className="min-w-0 flex-1">
      <div className="relative">
        <div className="flex h-2.5 overflow-hidden rounded-full">
          {SCORE_ZONES.map((z) => (
            <div
              key={z.label}
              style={{
                width: `${((z.to - z.from) / 24) * 100}%`,
                background: z.color,
              }}
            />
          ))}
        </div>
        <div
          className="absolute -top-1 h-4 w-1 -translate-x-1/2 rounded-full border border-white bg-slate-900 shadow"
          style={{ left: `${pct}%` }}
        />
      </div>
      <div className="mt-0.5 flex text-[8px] font-bold text-slate-400">
        {SCORE_ZONES.map((z) => (
          <span
            key={z.label}
            className="truncate text-center"
            style={{ width: `${((z.to - z.from) / 24) * 100}%` }}
          >
            {z.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function LayerLadder({ regime }: { regime: RegimeResponse | null }): React.ReactElement {
  const keys = ['long', 'medium', 'short', 'intraday'];
  const layers = regime
    ? keys
        .map((k) => regime.layers.find((l) => l.key === k))
        .filter((l): l is NonNullable<typeof l> => Boolean(l))
    : [];
  if (!layers.length) {
    return <div className="text-[9px] text-slate-300">阶段数据加载中…</div>;
  }
  return (
    <div className="flex flex-col gap-1">
      {layers.map((l) => {
        const score = clamp(l.score, -2, 2);
        const width = (Math.abs(score) / 2) * 50;
        const color = score > 0 ? '#e11d48' : score < 0 ? '#059669' : '#94a3b8';
        return (
          <div key={l.key} className="flex items-center gap-1.5">
            <span className="w-7 shrink-0 text-[9px] font-bold text-slate-500">{l.label}</span>
            <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
              <span className="absolute left-1/2 top-0 h-full w-px bg-slate-300" />
              <span
                className="absolute top-0 h-full rounded-full"
                style={{
                  left: score >= 0 ? '50%' : `${50 - width}%`,
                  width: `${width}%`,
                  background: color,
                }}
              />
            </div>
            <span
              className="w-16 shrink-0 truncate text-[9px] font-bold text-slate-700"
              title={l.stage}
            >
              {l.stage}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function StressMeter({
  value,
  label,
}: {
  value: number | null;
  label?: string;
}): React.ReactElement {
  return (
    <div>
      <div className="flex items-center justify-between text-[9px]">
        <span className="font-bold text-slate-500">情绪压力</span>
        <span className="font-mono font-bold text-slate-700">
          {isNum(value) ? value.toFixed(1) : '—'}
          {label ? <span className="ml-1 font-sans text-slate-400">{label}</span> : null}
        </span>
      </div>
      <div className="relative mt-0.5 h-1.5 rounded-full">
        <div
          className="absolute inset-0 rounded-full"
          style={{
            background:
              'linear-gradient(90deg,#dc2626 0%,#f59e0b 22%,#e2e8f0 50%,#14b8a6 78%,#059669 100%)',
          }}
        />
        {isNum(value) && (
          <div
            className="absolute -top-1 h-3.5 w-1 -translate-x-1/2 rounded-full border border-white bg-slate-900"
            style={{ left: `${clamp(value)}%` }}
          />
        )}
      </div>
    </div>
  );
}

function ProfitMeter({ value }: { value: number | null }): React.ReactElement {
  const tone = !isNum(value)
    ? 'text-slate-400'
    : value >= 60
      ? 'text-rose-600'
      : value >= 30
        ? 'text-amber-600'
        : 'text-emerald-600';
  return (
    <div>
      <div className="flex items-center justify-between text-[9px]">
        <span className="font-bold text-slate-500">赚钱效应</span>
        <span className={`font-mono font-bold ${tone}`}>
          {isNum(value) ? value.toFixed(1) : '—'}
          <span className="ml-0.5 font-sans text-[8px] text-slate-400">/100</span>
        </span>
      </div>
      <div className="relative mt-0.5 h-1.5 rounded-full">
        <div
          className="absolute inset-0 rounded-full"
          style={{
            background: 'linear-gradient(90deg,#059669 0%,#cbd5e1 50%,#e11d48 100%)',
          }}
        />
        {isNum(value) && (
          <div
            className="absolute -top-1 h-3.5 w-1 -translate-x-1/2 rounded-full border border-white bg-slate-900"
            style={{ left: `${clamp(value)}%` }}
          />
        )}
      </div>
    </div>
  );
}

function MainFlowBar({ value }: { value: number | null }): React.ReactElement {
  const w = isNum(value) ? (Math.min(Math.abs(value), 50) / 50) * 50 : 0;
  const positive = isNum(value) && value >= 0;
  return (
    <div>
      <div className="flex items-center justify-between text-[9px]">
        <span className="font-bold text-slate-500">主力净流入</span>
        <span className={`font-mono font-bold ${toneBySign(value)}`}>
          {isNum(value) ? fmtYi(value) : '—'}
        </span>
      </div>
      <div className="relative mt-0.5 h-1.5 rounded-full bg-slate-100">
        <span className="absolute left-1/2 top-0 h-full w-px bg-slate-300" />
        {isNum(value) && (
          <span
            className={`absolute top-0 h-full rounded-full ${positive ? 'bg-rose-500' : 'bg-emerald-500'}`}
            style={{ left: positive ? '50%' : `${50 - w}%`, width: `${w}%` }}
          />
        )}
      </div>
    </div>
  );
}

function BreadthRibbon({
  up,
  down,
  flat,
}: {
  up: number | null;
  down: number | null;
  flat: number | null;
}): React.ReactElement {
  const total = (up ?? 0) + (down ?? 0) + (flat ?? 0);
  const upPct = total > 0 && isNum(up) ? (up / total) * 100 : 0;
  const flatPct = total > 0 && isNum(flat) ? (flat / total) * 100 : 0;
  const downPct = total > 0 && isNum(down) ? (down / total) * 100 : 0;
  return (
    <div className="min-w-0 flex-1">
      <div className="flex items-center justify-between text-[9px] font-bold">
        <span className="text-rose-600">
          涨 {fmtCount(up)} <span className="text-rose-400">({upPct.toFixed(0)}%)</span>
        </span>
        <span className="text-slate-400">平 {fmtCount(flat)}</span>
        <span className="text-emerald-600">
          跌 {fmtCount(down)} <span className="text-emerald-500">({downPct.toFixed(0)}%)</span>
        </span>
      </div>
      <div className="mt-0.5 flex h-2 overflow-hidden rounded-full bg-slate-100">
        <div className="h-full bg-rose-500" style={{ width: `${upPct}%` }} />
        <div className="h-full bg-slate-300" style={{ width: `${flatPct}%` }} />
        <div className="h-full bg-emerald-500" style={{ width: `${downPct}%` }} />
      </div>
    </div>
  );
}

function ExposureBar({
  actual,
  ceiling,
}: {
  actual?: number;
  ceiling?: number;
}): React.ReactElement {
  const a = isNum(actual) ? clamp(actual * 100) : 0;
  const c = isNum(ceiling) ? clamp(ceiling * 100) : 0;
  return (
    <div>
      <div className="flex items-center justify-between text-[9px]">
        <span className="font-bold text-slate-500">基础暴露</span>
        <span className="font-mono font-bold text-slate-700">
          {isNum(actual) ? actual.toFixed(1) : '—'}
          <span className="text-slate-400"> / {isNum(ceiling) ? ceiling.toFixed(1) : '—'}</span>
        </span>
      </div>
      <div className="relative mt-0.5 h-1.5 rounded-full bg-slate-100">
        <div
          className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-purple-500"
          style={{ width: `${a}%` }}
        />
        {isNum(ceiling) && (
          <span
            className="absolute top-[-2px] h-2.5 w-0.5 bg-slate-700"
            style={{ left: `${c}%` }}
          />
        )}
      </div>
    </div>
  );
}

function lightTone(tone: 'up' | 'down' | 'warn' | 'muted'): string {
  return {
    up: 'border-rose-200 bg-rose-50 text-rose-600',
    down: 'border-emerald-200 bg-emerald-50 text-emerald-700',
    warn: 'border-amber-200 bg-amber-50 text-amber-700',
    muted: 'border-slate-200 bg-slate-50 text-slate-500',
  }[tone];
}

export function PanoramaHeader({
  date,
  regime,
  sentiment,
  breadth,
  oneLiner,
  snapshotMissing,
  snapshotFrom,
  failed,
  loading,
  onRefresh,
}: {
  date?: string;
  regime: RegimeResponse | null;
  sentiment: MarketSentiment | null;
  breadth: PanoramaBreadth | null;
  oneLiner: string;
  snapshotMissing?: boolean;
  snapshotFrom?: string;
  failed: number;
  loading: boolean;
  onRefresh: () => void;
}): React.ReactElement {
  const state = regime?.summary.state ?? '—';
  const score = regime?.summary.score ?? null;
  const stress = sentiment?.stress;
  const mainNetYi = sentiment?.volume?.latest?.main_net_yi ?? null;
  const profit = breadth?.profit_effect ?? null;
  const dist = sentiment?.distribution;
  const distUp = dist
    ? (dist.dist_limit_up ?? 0) + (dist.dist_up_big ?? 0) + (dist.dist_up_small ?? 0)
    : undefined;
  const distDown = dist
    ? (dist.dist_limit_down ?? 0) +
      (dist.dist_down_big ?? 0) +
      (dist.dist_down_small ?? 0)
    : undefined;
  const up = breadth?.advance_count ?? distUp ?? null;
  const down = breadth?.decline_count ?? distDown ?? null;
  const flat = breadth?.flat_count ?? dist?.dist_flat ?? null;

  const lights: Array<{ label: string; value: string; tone: 'up' | 'down' | 'warn' | 'muted' }> = [
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

  return (
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
      <div className="relative">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <div className="text-[9px] font-bold tracking-[0.28em] text-slate-400">
            MARKET PANORAMA
          </div>
          <div className="text-[9px] font-mono text-slate-400">
            {regime?.date ?? date ?? '最新'} · {regime?.universe_label ?? '全A等权'}
          </div>
          <div className="min-w-[200px] flex-1 truncate text-[10px] text-slate-500" title={oneLiner}>
            {oneLiner}
          </div>
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
            onClick={onRefresh}
            className="inline-flex h-6 w-6 items-center justify-center rounded-md border border-slate-200 bg-white/80 text-slate-500 transition hover:bg-slate-50"
            title="重新汇总"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        <div className="mt-2 grid grid-cols-1 gap-3 lg:grid-cols-12">
          <div className="lg:col-span-4">
            <div className="flex items-baseline gap-2">
              <span className={`text-2xl font-black tracking-tight ${STATE_TONE[state] ?? 'text-slate-700'}`}>
                {state}
              </span>
              <span className={`font-mono text-xs font-bold ${toneBySign(score)}`}>
                {isNum(score) ? `${score > 0 ? '+' : ''}${score}` : '—'}
                <span className="text-[9px] font-normal text-slate-400"> / ±12</span>
              </span>
            </div>
            <div className="mt-1">
              <ScoreGauge score={score} />
            </div>
          </div>

          <div className="lg:col-span-4">
            <div className="mb-1 flex items-center justify-between text-[9px] font-bold text-slate-400">
              <span>五层结构</span>
              <span>左=弱 右=强</span>
            </div>
            <LayerLadder regime={regime} />
          </div>

          <div className="flex flex-col justify-center gap-1.5 lg:col-span-4">
            <ExposureBar
              actual={regime?.exposure.actual}
              ceiling={regime?.exposure.ceiling}
            />
            <StressMeter value={isNum(stress?.score) ? stress.score : null} label={stress?.zone_label} />
            <ProfitMeter value={isNum(profit) ? profit : null} />
            <MainFlowBar value={isNum(mainNetYi) ? mainNetYi : null} />
          </div>
        </div>

        <div className="mt-2 flex flex-col gap-1.5 border-t border-slate-200/70 pt-2 lg:flex-row lg:items-center lg:gap-4">
          <BreadthRibbon
            up={isNum(up) ? up : null}
            down={isNum(down) ? down : null}
            flat={isNum(flat) ? flat : null}
          />
          <div className="flex flex-wrap items-center gap-1.5">
            {lights.map((l) => (
              <span
                key={l.label}
                className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[9px] font-bold ${lightTone(l.tone)}`}
              >
                <i className="h-1 w-1 rounded-full bg-current opacity-70" />
                <span className="opacity-70">{l.label}</span>
                <span>{l.value}</span>
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
