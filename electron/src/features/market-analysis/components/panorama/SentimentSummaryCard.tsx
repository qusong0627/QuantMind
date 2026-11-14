import React, { useMemo } from 'react';
import ReactECharts from 'echarts-for-react';
import { Activity, ArrowUpRight, Thermometer } from 'lucide-react';
import type { MarketSentiment } from '../../services/sentimentService';
import { clamp, fmtCount, isNum } from './format';

export interface PanoramaBreadth {
  advance_count?: number | null;
  decline_count?: number | null;
  flat_count?: number | null;
  limit_up_count?: number | null;
  limit_down_count?: number | null;
  total_turnover_yi?: number | null;
  profit_effect?: number | null;
  limit_up_broken_ratio?: number | null;
}

const ZONE: Record<string, { text: string; bg: string; border: string; dot: string }> = {
  extreme_pressure: {
    text: 'text-emerald-700',
    bg: 'bg-emerald-50',
    border: 'border-emerald-200',
    dot: 'bg-emerald-500',
  },
  pressure: { text: 'text-teal-700', bg: 'bg-teal-50', border: 'border-teal-200', dot: 'bg-teal-500' },
  neutral: { text: 'text-slate-600', bg: 'bg-slate-50', border: 'border-slate-200', dot: 'bg-slate-400' },
  strong: { text: 'text-amber-700', bg: 'bg-amber-50', border: 'border-amber-200', dot: 'bg-amber-500' },
  overheat: { text: 'text-rose-700', bg: 'bg-rose-50', border: 'border-rose-200', dot: 'bg-rose-500' },
};

const DIST_BUCKETS = [
  { key: 'dist_limit_up', label: '涨停', color: '#be123c' },
  { key: 'dist_up_big', label: '涨>5%', color: '#f43f5e' },
  { key: 'dist_up_small', label: '涨0~5%', color: '#fda4af' },
  { key: 'dist_flat', label: '平', color: '#cbd5e1' },
  { key: 'dist_down_small', label: '跌0~5%', color: '#6ee7b7' },
  { key: 'dist_down_big', label: '跌>5%', color: '#10b981' },
  { key: 'dist_limit_down', label: '跌停', color: '#047857' },
] as const;

function profitTone(v: number | null | undefined): string {
  if (!isNum(v)) return 'text-slate-400';
  if (v >= 60) return 'text-rose-600';
  if (v >= 30) return 'text-amber-600';
  return 'text-emerald-600';
}

export function SentimentSummaryCard({
  sentiment,
  breadth,
  loading,
  onOpen,
}: {
  sentiment: MarketSentiment | null;
  breadth: PanoramaBreadth | null;
  loading?: boolean;
  onOpen: () => void;
}): React.ReactElement {
  const stress = sentiment?.stress;
  const zone = ZONE[stress?.zone ?? 'neutral'] ?? ZONE.neutral;
  const dist = sentiment?.distribution;
  const distUp = dist
    ? (dist.dist_limit_up ?? 0) + (dist.dist_up_big ?? 0) + (dist.dist_up_small ?? 0)
    : undefined;
  const distDown = dist
    ? (dist.dist_limit_down ?? 0) +
      (dist.dist_down_big ?? 0) +
      (dist.dist_down_small ?? 0)
    : undefined;
  const up = breadth?.advance_count ?? distUp;
  const down = breadth?.decline_count ?? distDown;
  const flat = breadth?.flat_count ?? dist?.dist_flat;
  const limitUp = breadth?.limit_up_count ?? dist?.dist_limit_up;
  const limitDown = breadth?.limit_down_count ?? dist?.dist_limit_down;
  const turnover = breadth?.total_turnover_yi ?? sentiment?.volume?.latest?.turnover_yi;
  const total = (up ?? 0) + (down ?? 0) + (flat ?? 0);
  const upPct = total > 0 && isNum(up) ? (up / total) * 100 : null;
  const downPct = total > 0 && isNum(down) ? (down / total) * 100 : null;
  const distTotal = dist
    ? DIST_BUCKETS.reduce((s, b) => s + (dist[b.key] ?? 0), 0)
    : 0;
  const profit = breadth?.profit_effect;

  const stressOption = useMemo<Record<string, unknown> | null>(() => {
    const hist = sentiment?.stress_history?.slice(-60) ?? [];
    if (hist.length < 3) return null;
    return {
      animation: false,
      grid: { left: 2, right: 4, top: 8, bottom: 2 },
      xAxis: {
        type: 'category',
        show: false,
        boundaryGap: false,
        data: hist.map((h) => h.date),
      },
      yAxis: { type: 'value', min: 0, max: 100, show: false },
      tooltip: {
        trigger: 'axis',
        formatter: (p: unknown) => {
          const arr = p as { name?: string; value?: number }[];
          const v = arr?.[0];
          return v ? `${v.name}<br/>广度压力 ${Number(v.value).toFixed(1)}` : '';
        },
      },
      series: [
        {
          type: 'line',
          data: hist.map((h) => h.stress),
          smooth: true,
          symbol: 'none',
          lineStyle: { color: '#f59e0b', width: 1.8 },
          areaStyle: { color: 'rgba(245,158,11,0.16)' },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { color: '#cbd5e1', width: 1, type: 'dashed' },
            data: [{ yAxis: 50 }],
          },
          markPoint: {
            symbol: 'circle',
            symbolSize: 6,
            data: [{ coord: [hist[hist.length - 1].date, hist[hist.length - 1].stress] }],
            itemStyle: { color: '#f59e0b', borderColor: '#fff', borderWidth: 1.5 },
          },
        },
      ],
    };
  }, [sentiment]);

  const fallbackText =
    isNum(up) && isNum(down)
      ? `上涨 ${fmtCount(up)} 家（${upPct?.toFixed(0)}%），下跌 ${fmtCount(down)} 家（${downPct?.toFixed(0)}%）` +
        (isNum(limitDown) && isNum(limitUp)
          ? `；跌停 ${limitDown} 家、涨停 ${limitUp} 家`
          : '') +
        (isNum(breadth?.limit_up_broken_ratio)
          ? `，炸板率 ${breadth?.limit_up_broken_ratio}%`
          : '') +
        '。'
      : '情绪数据不完整，等待下一交易日快照。';

  return (
    <div className="relative flex h-full min-h-[380px] flex-col overflow-hidden rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm">
      <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-amber-400 via-orange-400 to-rose-400" />
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Activity className="h-3.5 w-3.5 text-amber-600" />
          <h3 className="text-xs font-extrabold text-slate-800">大盘情绪</h3>
          {stress?.zone_label && (
            <span className={`rounded-full border px-1.5 py-0.5 text-[9px] font-bold ${zone.text} ${zone.bg} ${zone.border}`}>
              {stress.zone_label}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={onOpen}
          className="flex items-center gap-0.5 text-[10px] font-bold text-amber-600 hover:underline"
        >
          情绪全貌 <ArrowUpRight className="h-3 w-3" />
        </button>
      </div>

      {!sentiment && !breadth ? (
        <div className="grid flex-1 place-items-center text-[11px] font-bold text-slate-300">
          {loading ? '加载中…' : '情绪数据不可用'}
        </div>
      ) : (
        <>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <div className="rounded-xl border border-slate-100 bg-slate-50/70 px-2.5 py-1.5">
              <div className="text-[9px] font-bold text-slate-400">赚钱效应</div>
              <div className={`font-mono text-xl font-black leading-tight ${profitTone(profit)}`}>
                {isNum(profit) ? profit.toFixed(1) : '—'}
                <span className="text-[9px] font-normal text-slate-400"> /100</span>
              </div>
            </div>
            <div className={`rounded-xl border px-2.5 py-1.5 ${zone.bg} ${zone.border}`}>
              <div className={`text-[9px] font-bold ${zone.text} opacity-80`}>
                广度压力（越高越普跌）
              </div>
              <div className={`font-mono text-xl font-black leading-tight ${zone.text}`}>
                {isNum(stress?.score) ? stress.score.toFixed(1) : '—'}
                <span className="text-[9px] font-normal opacity-60"> /100</span>
              </div>
            </div>
          </div>

          <div className="mt-2.5">
            <div className="relative h-2.5 rounded-full">
              <div
                className="absolute inset-0 rounded-full"
                style={{
                  background:
                    'linear-gradient(90deg,#dc2626 0%,#f59e0b 22%,#e2e8f0 50%,#14b8a6 78%,#059669 100%)',
                }}
              />
              {isNum(stress?.score) && (
                <div
                  className="absolute top-[-3px] h-4 w-1.5 -translate-x-1/2 rounded-full border border-white bg-slate-900 shadow"
                  style={{ left: `${clamp(stress.score)}%` }}
                  title={`广度压力 ${stress.score.toFixed(1)}`}
                />
              )}
            </div>
            <div className="mt-0.5 flex justify-between text-[8px] font-bold text-slate-400">
              <span>过热 0</span>
              <span>中性 50</span>
              <span>极端压力 100</span>
            </div>
          </div>

          <div className="mt-2 flex min-h-[100px] flex-1 flex-col">
            <div className="flex items-center justify-between text-[9px] font-bold text-slate-400">
              <span>广度压力 · 60日</span>
              <span className="font-mono">
                当前 {isNum(stress?.score) ? stress.score.toFixed(1) : '—'}
              </span>
            </div>
            <div className="min-h-[76px] flex-1">
              {stressOption ? (
                <ReactECharts
                  option={stressOption}
                  notMerge
                  lazyUpdate
                  style={{ height: '100%', minHeight: 76 }}
                />
              ) : (
                <div className="grid h-full place-items-center text-[9px] text-slate-300">
                  压力历史不足
                </div>
              )}
            </div>
          </div>

          <div className="mt-2">
            {dist && distTotal > 0 ? (
              <>
                <div className="flex h-2.5 overflow-hidden rounded-full border border-slate-100">
                  {DIST_BUCKETS.map((b) => {
                    const n = dist[b.key] ?? 0;
                    const w = (n / distTotal) * 100;
                    if (w <= 0) return null;
                    return (
                      <div
                        key={b.key}
                        className="h-full first:rounded-l-full last:rounded-r-full"
                        style={{ width: `${w}%`, background: b.color }}
                        title={`${b.label} ${n} 家`}
                      />
                    );
                  })}
                </div>
                <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-[8px] text-slate-500">
                  {DIST_BUCKETS.filter((b) => (dist[b.key] ?? 0) > 0).map((b) => (
                    <span key={b.key} className="inline-flex items-center gap-0.5">
                      <i className="h-1.5 w-1.5 rounded-full" style={{ background: b.color }} />
                      {b.label} {dist[b.key]}
                    </span>
                  ))}
                </div>
              </>
            ) : (
              <div className="flex h-2.5 overflow-hidden rounded-full border border-slate-100 bg-slate-100">
                <div className="h-full bg-rose-500" style={{ width: `${upPct ?? 0}%` }} />
                <div className="h-full bg-slate-300" style={{ width: `${total > 0 && isNum(flat) ? (flat / total) * 100 : 0}%` }} />
                <div className="h-full bg-emerald-500" style={{ width: `${downPct ?? 0}%` }} />
              </div>
            )}
          </div>

          <div className="mt-2 grid grid-cols-3 gap-1.5 text-center">
            <div className="rounded-lg border border-rose-100 bg-rose-50/70 py-1">
              <div className="text-[8px] font-bold text-rose-500">上涨</div>
              <div className="font-mono text-[11px] font-extrabold text-rose-600">
                {fmtCount(up)}
                <span className="text-[8px] font-normal text-rose-400"> ({upPct?.toFixed(0) ?? '—'}%)</span>
              </div>
            </div>
            <div className="rounded-lg border border-emerald-100 bg-emerald-50/70 py-1">
              <div className="text-[8px] font-bold text-emerald-500">下跌</div>
              <div className="font-mono text-[11px] font-extrabold text-emerald-600">
                {fmtCount(down)}
                <span className="text-[8px] font-normal text-emerald-400"> ({downPct?.toFixed(0) ?? '—'}%)</span>
              </div>
            </div>
            <div className="rounded-lg border border-slate-100 bg-slate-50 py-1">
              <div className="text-[8px] font-bold text-slate-400">涨停 / 跌停</div>
              <div className="font-mono text-[11px] font-extrabold text-slate-700">
                <span className="text-rose-600">{fmtCount(limitUp)}</span>
                <span className="text-slate-300"> / </span>
                <span className="text-emerald-600">{fmtCount(limitDown)}</span>
              </div>
            </div>
          </div>

          <p className="mt-2 line-clamp-3 text-[10px] leading-relaxed text-slate-500">
            {stress?.phase_detail || stress?.phase || fallbackText}
          </p>
          <div className="mt-1 flex items-center gap-2 text-[9px] text-slate-400">
            <Thermometer className="h-2.5 w-2.5" />
            <span>炸板率 {isNum(breadth?.limit_up_broken_ratio) ? `${breadth?.limit_up_broken_ratio}%` : '—'}</span>
            <span>· 总成交 ¥{isNum(turnover) ? turnover?.toLocaleString() : '—'} 亿</span>
          </div>
        </>
      )}
    </div>
  );
}
