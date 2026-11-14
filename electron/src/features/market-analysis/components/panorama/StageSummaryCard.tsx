import React, { useMemo } from 'react';
import ReactECharts from 'echarts-for-react';
import { Crosshair, ArrowUpRight, ShieldAlert } from 'lucide-react';
import type { RegimeResponse } from '../../services/regimeService';
import { clamp, isNum } from './format';

const LAYER_KEYS = ['long', 'medium', 'short', 'intraday'] as const;

function scoreTone(score: number): string {
  if (score > 0) return 'text-rose-600';
  if (score < 0) return 'text-emerald-600';
  return 'text-slate-500';
}

function scoreBar(score: number): { left: number; width: number; color: string } {
  const pct = clamp(((score + 2) / 4) * 100);
  return {
    left: score >= 0 ? 50 : pct,
    width: Math.abs(pct - 50),
    color: score > 0 ? '#e11d48' : score < 0 ? '#059669' : '#94a3b8',
  };
}

export function StageSummaryCard({
  regime,
  loading,
  onOpen,
}: {
  regime: RegimeResponse | null;
  loading?: boolean;
  onOpen: () => void;
}): React.ReactElement {
  const radarOption = useMemo<Record<string, unknown> | null>(() => {
    if (!regime) return null;
    const prev =
      regime.block_history.length > 20
        ? regime.block_history[regime.block_history.length - 21]
        : null;
    return {
      animation: false,
      radar: {
        indicator: regime.blocks.map((b) => ({ name: b.label, max: 2, min: -2 })),
        shape: 'polygon',
        radius: '66%',
        center: ['50%', '50%'],
        axisName: { fontSize: 9, color: '#475569', fontWeight: 'bold' },
        splitLine: { lineStyle: { color: '#e2e8f0' } },
        splitArea: { areaStyle: { color: ['#f8fafc', '#ffffff'] } },
      },
      tooltip: { trigger: 'item' },
      series: [
        {
          type: 'radar',
          name: '当前',
          symbolSize: 3,
          data: [
            {
              value: regime.blocks.map((b) => b.score),
              name: '当前',
              areaStyle: { color: 'rgba(124,58,237,0.28)' },
              lineStyle: { color: '#7c3aed', width: 2 },
              itemStyle: { color: '#7c3aed' },
            },
          ],
        },
        ...(prev
          ? [
              {
                type: 'radar',
                name: '20日前',
                symbolSize: 2,
                data: [
                  {
                    value: prev.scores,
                    name: '20日前',
                    areaStyle: { color: 'rgba(100,116,139,0.08)' },
                    lineStyle: { color: '#64748b', width: 1.5, type: 'dashed' },
                    itemStyle: { color: '#64748b' },
                  },
                ],
              },
            ]
          : []),
      ],
    };
  }, [regime]);

  const layers = regime
    ? LAYER_KEYS.map((key) => regime.layers.find((l) => l.key === key)).filter(
        (l): l is NonNullable<typeof l> => Boolean(l),
      )
    : [];
  const exposure = regime?.exposure;
  const actualPct = isNum(exposure?.actual) ? clamp(exposure.actual * 100) : 0;
  const ceilingPct = isNum(exposure?.ceiling) ? clamp(exposure.ceiling * 100) : 0;

  return (
    <div className="relative flex h-full min-h-[380px] flex-col overflow-hidden rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm">
      <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-indigo-500 via-purple-500 to-fuchsia-400" />
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Crosshair className="h-3.5 w-3.5 text-purple-600" />
          <h3 className="text-xs font-extrabold text-slate-800">市场阶段</h3>
          <span className="rounded-full border border-purple-100 bg-purple-50 px-1.5 py-0.5 text-[9px] font-bold text-purple-600">
            {regime?.universe_label ?? '全A等权'}
          </span>
        </div>
        <button
          type="button"
          onClick={onOpen}
          className="flex items-center gap-0.5 text-[10px] font-bold text-purple-600 hover:underline"
        >
          完整判断 <ArrowUpRight className="h-3 w-3" />
        </button>
      </div>

      {!regime ? (
        <div className="grid flex-1 place-items-center text-[11px] font-bold text-slate-300">
          {loading ? '加载中…' : '阶段数据不可用'}
        </div>
      ) : (
        <>
          <div className="mt-1.5 flex items-center gap-2">
            <span className="text-lg font-black text-slate-900">{regime.summary.state}</span>
            <span className={`font-mono text-xs font-extrabold ${scoreTone(regime.summary.score)}`}>
              {regime.summary.score > 0 ? '+' : ''}
              {regime.summary.score} <span className="text-[9px] font-normal text-slate-400">/ ±12</span>
            </span>
            <span className="ml-auto text-[10px] text-slate-400">{regime.date}</span>
          </div>
          <p className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-slate-500">
            {regime.summary.one_liner}
          </p>

          <div className="mt-2 flex min-h-0 flex-1 gap-2">
            <div className="w-[44%] min-w-[150px]">
              {radarOption ? (
                <ReactECharts
                  option={radarOption}
                  notMerge
                  lazyUpdate
                  style={{ height: '100%', minHeight: 180 }}
                />
              ) : null}
            </div>
            <div className="flex min-w-0 flex-1 flex-col justify-center gap-1.5">
              {layers.map((l) => {
                const metric = l.metrics[0];
                const bar = scoreBar(l.score);
                return (
                  <div key={l.key} className="flex items-center gap-2">
                    <span className="w-7 shrink-0 text-[10px] font-bold text-slate-500">
                      {l.label}
                    </span>
                    <span className="w-20 shrink-0 truncate text-[10px] font-extrabold text-slate-800">
                      {l.stage}
                    </span>
                    <span className="relative h-1.5 w-12 shrink-0 overflow-hidden rounded-full bg-slate-100">
                      <span
                        className="absolute top-0 h-full rounded-full"
                        style={{ left: `${bar.left}%`, width: `${bar.width}%`, background: bar.color }}
                      />
                    </span>
                    <span className="truncate text-[9px] text-slate-400">
                      {metric
                        ? `${metric.label} ${metric.value}${metric.unit}`
                        : l.action}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="mt-2 grid grid-cols-3 gap-1">
            {regime.blocks.map((b) => {
              const bar = scoreBar(b.score);
              return (
                <div
                  key={b.key}
                  className="flex items-center gap-1 rounded-md border border-slate-100 bg-slate-50/60 px-1.5 py-1"
                  title={`${b.label}：${b.read}（${b.value}${b.unit}）`}
                >
                  <span className="w-10 shrink-0 truncate text-[9px] text-slate-500">
                    {b.label}
                  </span>
                  <span className="relative h-1 flex-1 rounded-full bg-slate-200/70">
                    <span
                      className="absolute top-0 h-full rounded-full"
                      style={{ left: `${bar.left}%`, width: `${bar.width}%`, background: bar.color }}
                    />
                  </span>
                  <span className={`w-4 shrink-0 text-right font-mono text-[9px] font-bold ${scoreTone(b.score)}`}>
                    {b.score > 0 ? `+${b.score}` : b.score}
                  </span>
                </div>
              );
            })}
          </div>

          <div className="mt-2 border-t border-slate-100 pt-2">
            <div className="flex items-center justify-between text-[10px]">
              <span className="font-bold text-slate-500">基础暴露</span>
              <span className="font-mono font-extrabold text-slate-800">
                {isNum(exposure?.actual) ? exposure?.actual.toFixed(1) : '—'}
                <span className="text-[9px] font-normal text-slate-400">
                  {' '}
                  / 天花板 {isNum(exposure?.ceiling) ? exposure?.ceiling.toFixed(1) : '—'}
                </span>
              </span>
            </div>
            <div className="relative mt-1 h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-purple-500"
                style={{ width: `${actualPct}%` }}
              />
              <div
                className="absolute top-[-2px] h-2.5 w-0.5 bg-slate-700"
                style={{ left: `${ceilingPct}%` }}
                title={`天花板 ${ceilingPct.toFixed(0)}%`}
              />
            </div>
            <div className="mt-1.5 flex flex-wrap gap-1">
              <span
                className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[9px] font-bold ${
                  regime.top_risk.flag
                    ? 'border-rose-200 bg-rose-50 text-rose-600'
                    : 'border-slate-200 bg-slate-50 text-slate-500'
                }`}
                title={regime.top_risk.text}
              >
                <ShieldAlert className="h-2.5 w-2.5" />
                顶风险 {regime.top_risk.flag ? '有' : '无'}
              </span>
              <span
                className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[9px] font-bold ${
                  regime.bottom.flag
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                    : 'border-slate-200 bg-slate-50 text-slate-500'
                }`}
                title={regime.bottom.text}
              >
                底部 {regime.bottom.flag ? '成立' : '无'}
              </span>
              <span
                className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[9px] font-bold ${
                  regime.events?.trin_surrender
                    ? 'border-amber-200 bg-amber-50 text-amber-700'
                    : 'border-slate-200 bg-slate-50 text-slate-500'
                }`}
                title="TRIN_量 > 2：投降/恐慌确认；历史 20 日 +1.4%~+4.0%，只做反抽/底部确认，不是趋势买点"
              >
                TRIN {isNum(regime.events?.trin_vol) ? regime.events.trin_vol?.toFixed(2) : '—'}
                {regime.events?.trin_surrender ? ' · 投降' : ''}
              </span>
              <span
                className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[9px] font-bold ${
                  regime.events?.amount_gap_risk
                    ? 'border-amber-200 bg-amber-50 text-amber-700'
                    : 'border-slate-200 bg-slate-50 text-slate-500'
                }`}
                title="额价差 = 涨额占比 − 涨家占比；>20pp 历史 40 日 −1.6~−2.5pp（t −2.4~−3.1），只作 40 日风险提示"
              >
                额价差 {isNum(regime.events?.amount_gap_pp) ? `${regime.events.amount_gap_pp > 0 ? '+' : ''}${regime.events.amount_gap_pp.toFixed(1)}pp` : '—'}
                {regime.events?.amount_gap_risk ? ' · 风险' : ''}
              </span>
            </div>

            {regime.stock_breadth?.available && regime.stock_breadth.quintiles?.length ? (
              <div className="mt-1 flex items-center gap-1" title={regime.stock_breadth.note}>
                <span className="w-16 shrink-0 text-[8px] font-bold text-slate-400">
                  10:10→收盘{regime.stock_breadth.sample === 'hot_set' ? '·热集' : ''}
                </span>
                {regime.stock_breadth.quintiles.map((q) => {
                  const positive = q.ret_after >= 0;
                  const height = Math.min(14, Math.max(3, Math.abs(q.ret_after) * 5 + 3));
                  return (
                    <span
                      key={q.q}
                      className="flex-1 text-center"
                      title={`${q.q}（${q.n} 只）：10:10 ${q.ret_1010 > 0 ? '+' : ''}${q.ret_1010.toFixed(2)}% → 收盘中位 ${q.ret_after > 0 ? '+' : ''}${q.ret_after.toFixed(2)}%；收涨占比 ${q.up_share_after}%`}
                    >
                      <span
                        className={`mx-auto block w-2 rounded-sm ${positive ? 'bg-rose-400' : 'bg-emerald-400'}`}
                        style={{ height }}
                      />
                      <span className={`block font-mono text-[8px] ${positive ? 'text-rose-500' : 'text-emerald-600'}`}>
                        {q.ret_after > 0 ? '+' : ''}
                        {q.ret_after.toFixed(1)}
                      </span>
                    </span>
                  );
                })}
              </div>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}
