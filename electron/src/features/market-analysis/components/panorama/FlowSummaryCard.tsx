import React, { useMemo } from 'react';
import ReactECharts from 'echarts-for-react';
import { ArrowUpRight, Network } from 'lucide-react';
import type { MarketSentiment } from '../../services/sentimentService';
import type { FlowPanoramaResponse } from '../../services/moneyFlowService';
import { fmtRatioPct, fmtYi, fmtYiFromYuan, isNum, toneBySign } from './format';

export function FlowSummaryCard({
  sentiment,
  flow,
  loading,
  onOpen,
}: {
  sentiment: MarketSentiment | null;
  flow: FlowPanoramaResponse | null;
  loading?: boolean;
  onOpen: () => void;
}): React.ReactElement {
  const latest = sentiment?.volume?.latest;
  const sectors = flow?.sectors ?? [];
  const sampleNetYi = sectors.length
    ? sectors.reduce((s, x) => s + (isNum(x.net_main) ? x.net_main : 0), 0) / 1e8
    : null;
  const wholeNetYi = isNum(latest?.main_net_yi) ? latest.main_net_yi : null;
  const mainNetYi = wholeNetYi ?? sampleNetYi;
  const sampleBased = wholeNetYi === null && sampleNetYi !== null;

  const sorted = [...sectors].sort((a, b) => b.net_main - a.net_main);
  const topIn = sorted.filter((s) => s.net_main > 0).slice(0, 3);
  const topOut = sorted.filter((s) => s.net_main < 0).slice(-3).reverse();
  const rows = [...topIn, ...topOut];
  const maxAbsYi = Math.max(...rows.map((r) => Math.abs(r.net_main) / 1e8), 1);
  const inflowCount = sorted.filter((s) => s.net_main > 0).length;

  const topOutNames = topOut.map((s) => s.name).join('、');
  const topInNames = topIn.map((s) => s.name).join('、');
  const flowText = sectors.length
    ? `样本 ${sectors.length} 个板块中 ${inflowCount} 个净流入` +
      (topOutNames ? `；流出集中在 ${topOutNames}` : '') +
      (topInNames ? `；流入集中在 ${topInNames}` : '') +
      '。'
    : '资金流板块样本不可用，等待当日 L2 资金流落库。';

  const netOption = useMemo<Record<string, unknown> | null>(() => {
    const vol = sentiment?.volume;
    if (!vol?.dates?.length) return null;
    const start = Math.max(0, vol.dates.length - 20);
    const dates = vol.dates.slice(start).map((d) => d.slice(5));
    const values = (vol.main_net_yi ?? []).slice(start);
    if (!values.length) return null;
    return {
      animation: false,
      grid: { left: 2, right: 2, top: 8, bottom: 2 },
      xAxis: { type: 'category', data: dates, show: false },
      yAxis: { type: 'value', show: false },
      tooltip: {
        trigger: 'axis',
        formatter: (p: unknown) => {
          const arr = p as { name?: string; value?: number }[];
          const v = arr?.[0];
          return v ? `${v.name}<br/>主力净额 ${Number(v.value).toFixed(1)} 亿` : '';
        },
      },
      series: [
        {
          type: 'bar',
          data: values,
          barWidth: '62%',
          itemStyle: {
            color: (p: { value?: number }) =>
              (p.value ?? 0) >= 0 ? '#e11d48' : '#059669',
            borderRadius: 1,
          },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { color: '#cbd5e1', width: 1 },
            data: [{ yAxis: 0 }],
          },
        },
      ],
    };
  }, [sentiment]);

  return (
    <div className="relative flex h-full min-h-[380px] flex-col overflow-hidden rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm">
      <div className="absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r from-rose-400 via-slate-300 to-emerald-400" />
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Network className="h-3.5 w-3.5 text-rose-500" />
          <h3 className="text-xs font-extrabold text-slate-800">资金流全景</h3>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[9px] font-bold text-slate-500">
            1日 · 行业二级
          </span>
        </div>
        <button
          type="button"
          onClick={onOpen}
          className="flex items-center gap-0.5 text-[10px] font-bold text-rose-600 hover:underline"
        >
          资金流全景 <ArrowUpRight className="h-3 w-3" />
        </button>
      </div>

      {!flow && !sentiment ? (
        <div className="grid flex-1 place-items-center text-[11px] font-bold text-slate-300">
          {loading ? '加载中…' : '资金流数据不可用'}
        </div>
      ) : (
        <>
          <div className="mt-2 flex items-end justify-between gap-2">
            <div>
              <div className="text-[9px] font-bold text-slate-400">
                主力净流入{sampleBased ? '（样本合计）' : '（全市场）'}
              </div>
              <div className={`font-mono text-2xl font-black leading-tight ${toneBySign(mainNetYi)}`}>
                {fmtYi(mainNetYi)}
              </div>
            </div>
            <div className="text-right text-[9px] leading-relaxed text-slate-400">
              <div>
                占成交额{' '}
                <span className={`font-mono font-bold ${toneBySign(latest?.main_net_ratio)}`}>
                  {fmtRatioPct(latest?.main_net_ratio)}
                </span>
              </div>
              <div>
                主力覆盖{' '}
                <span className="font-mono font-bold text-slate-600">
                  {fmtRatioPct(latest?.main_breadth)}
                </span>
              </div>
            </div>
          </div>

          <div className="mt-2 flex items-center justify-between text-[9px] font-bold">
            <span className="text-rose-500">净流入 Top {topIn.length}</span>
            <span className="text-emerald-600">净流出 Top {topOut.length}</span>
          </div>
          <div className="mt-1 flex flex-col justify-center gap-1">
            {rows.length ? (
              rows.map((s) => {
                const yi = s.net_main / 1e8;
                const w = (Math.abs(yi) / maxAbsYi) * 50;
                const positive = yi >= 0;
                return (
                  <div key={`${s.name}-${positive}`} className="flex items-center gap-1.5">
                    <span className="w-14 shrink-0 truncate text-[9px] text-slate-600" title={s.name}>
                      {s.name}
                    </span>
                    <div className="relative h-2 flex-1 rounded-full bg-slate-100">
                      <div
                        className={`absolute top-0 h-full rounded-full ${positive ? 'bg-rose-500' : 'bg-emerald-500'}`}
                        style={{
                          left: positive ? '50%' : `${50 - w}%`,
                          width: `${w}%`,
                        }}
                      />
                      <div className="absolute left-1/2 top-[-1px] h-[10px] w-px bg-slate-300" />
                    </div>
                    <span
                      className={`w-12 shrink-0 text-right font-mono text-[9px] font-bold ${toneBySign(yi)}`}
                    >
                      {fmtYiFromYuan(s.net_main)}
                    </span>
                  </div>
                );
              })
            ) : (
              <div className="text-[10px] text-slate-400">暂无板块资金流样本</div>
            )}
          </div>

          <div className="mt-2 flex min-h-[80px] flex-1 flex-col">
            <div className="flex items-center justify-between text-[9px] font-bold text-slate-400">
              <span>主力净额 · 20日</span>
              <span className="font-mono">{isNum(mainNetYi) ? fmtYi(mainNetYi) : '—'}</span>
            </div>
            <div className="min-h-[56px] flex-1">
              {netOption ? (
                <ReactECharts
                  option={netOption}
                  notMerge
                  lazyUpdate
                  style={{ height: '100%', minHeight: 56 }}
                />
              ) : (
                <div className="grid h-full place-items-center text-[9px] text-slate-300">
                  主力净额历史不足
                </div>
              )}
            </div>
          </div>

          <div className="mt-2 border-t border-slate-100 pt-2">
            <div className="flex items-center justify-between text-[9px] text-slate-400">
              <span>
                板块净流入{' '}
                <span className="font-mono font-bold text-slate-600">
                  {sectors.length ? `${inflowCount}/${sectors.length}` : '—'}
                </span>
              </span>
              <span>{flow?.trade_date ?? sentiment?.trade_date ?? '—'}</span>
            </div>
            <p className="mt-1 line-clamp-3 text-[10px] leading-relaxed text-slate-500">
              {flowText}
            </p>
          </div>
        </>
      )}
    </div>
  );
}
