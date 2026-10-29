/**
 * 大盘情绪 · 成交量 × 主力加权系数曲线。
 *
 * 上：全市场成交额柱（红=成交额加权偏涨、绿=偏跌）。
 * 下：实线=成交额加权涨跌系数 (amt_up−amt_down)/amt_total，−1~1；
 *     虚线=主力成交额加权广度（主力净流入为正的成交额占比），0~1。
 * 全部为当日可得数据，不参与评分；主力=超大单+大单，不是散户资金。
 */
import React, { useMemo, useState } from 'react';

import type { SentimentVolume } from '../services/sentimentService';

const fmtTurnover = (v: number | null | undefined): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return v >= 10000 ? `${(v / 10000).toFixed(2)} 万亿` : `${v.toFixed(0)} 亿`;
};
const fmtSigned = (v: number | null | undefined, digits = 3): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}`;
};
const fmtPct100 = (v: number | null | undefined, digits = 1): string => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
};

export const VolumeForceChart: React.FC<{ volume: SentimentVolume }> = ({ volume }) => {
  const [days, setDays] = useState<60 | 120>(60);
  const n = volume.dates.length;
  const start = Math.max(0, n - days);
  const dates = volume.dates.slice(start);
  const turnover = volume.turnover_yi.slice(start);
  const vw = volume.vw_breadth.slice(start);
  const mainAmt = volume.main_breadth_amt.slice(start);

  const maxTurn = Math.max(...turnover.filter((v): v is number => typeof v === 'number' && Number.isFinite(v)), 1);
  const width = 820;
  const left = 30;
  const right = 14;
  const plotW = width - left - right;
  const barW = plotW / Math.max(dates.length, 1);
  const topBase = 96;
  const topH = 76;
  const mid = 150;
  const span = 32;

  const pathOf = (values: (number | null)[], yOf: (v: number) => number): string => {
    let d = '';
    let open = false;
    values.forEach((v, i) => {
      if (typeof v !== 'number' || !Number.isFinite(v)) {
        open = false;
        return;
      }
      const x = left + (i + 0.5) * barW;
      const y = yOf(v);
      d += `${open ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `;
      open = true;
    });
    return d.trim();
  };
  const vwPath = useMemo(
    () => pathOf(vw, (v) => mid - Math.max(-1, Math.min(1, v)) * span),
    [vw, barW, mid, span],
  );
  const mainPath = useMemo(
    () => pathOf(mainAmt, (v) => 190 - Math.max(0, Math.min(1, v)) * 72),
    [mainAmt, barW],
  );

  const latest = volume.latest ?? {};
  const lastVw = latest.vw_breadth ?? [...vw].reverse().find((v) => typeof v === 'number') ?? null;
  const lastMain = latest.main_breadth_amt ?? [...mainAmt].reverse().find((v) => typeof v === 'number') ?? null;

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm px-4 py-3 flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-extrabold text-slate-800">成交量 × 主力加权系数</span>
          <span className="text-[10px] text-slate-400">柱=全市场成交额 · 实线=成交额加权涨跌 · 虚线=主力成交额加权广度</span>
        </div>
        <div className="flex items-center gap-1">
          {([60, 120] as const).map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-2 py-0.5 rounded-full text-[10px] font-extrabold ${
                days === d ? 'bg-slate-800 text-white' : 'bg-slate-100 text-slate-500 hover:bg-slate-200'
              }`}
            >
              近{d}日
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-1.5 text-[10px]">
        <div className="rounded-lg bg-slate-50 border border-slate-100 px-2 py-1">
          <div className="text-slate-400">成交额</div>
          <div className="font-mono font-extrabold text-slate-800">{fmtTurnover(latest.turnover_yi)}</div>
        </div>
        <div className="rounded-lg bg-red-50/50 border border-red-100 px-2 py-1">
          <div className="text-slate-400">成交额加权系数</div>
          <div className={`font-mono font-extrabold ${(lastVw ?? 0) >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>{fmtSigned(lastVw)}</div>
        </div>
        <div className="rounded-lg bg-red-50/50 border border-red-100 px-2 py-1">
          <div className="text-slate-400">主力净额 / 成交额</div>
          <div className={`font-mono font-extrabold ${(latest.main_net_ratio ?? 0) >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
            {fmtPct100(latest.main_net_ratio, 2)}
          </div>
        </div>
        <div className="rounded-lg bg-purple-50/50 border border-purple-100 px-2 py-1">
          <div className="text-slate-400">主力广度（等权 / 额加权）</div>
          <div className="font-mono font-extrabold text-slate-700">
            {fmtPct100(latest.main_breadth, 0)} / {fmtPct100(lastMain, 0)}
          </div>
        </div>
        <div className="rounded-lg bg-slate-50 border border-slate-100 px-2 py-1">
          <div className="text-slate-400">主力净额</div>
          <div className={`font-mono font-extrabold ${(latest.main_net_yi ?? 0) >= 0 ? 'text-red-600' : 'text-emerald-600'}`}>
            {latest.main_net_yi == null ? '—' : `${latest.main_net_yi > 0 ? '+' : ''}${latest.main_net_yi.toFixed(1)} 亿`}
          </div>
        </div>
      </div>

      <svg viewBox={`0 0 ${width} 200`} className="w-full" role="img" aria-label="成交量与主力加权系数曲线">
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <line key={f} x1={left} x2={width - right} y1={topBase - f * topH} y2={topBase - f * topH} stroke="#f1f5f9" strokeWidth="1" />
        ))}
        {turnover.map((v, i) => {
          const h = typeof v === 'number' && Number.isFinite(v) ? Math.max(1, (v / maxTurn) * topH) : 0;
          const coef = vw[i];
          const color = coef == null ? '#cbd5e1' : coef >= 0 ? '#ef4444' : '#10b981';
          const cx = left + i * barW + barW * 0.18;
          const bw = Math.max(1, barW * 0.64);
          return (
            <rect
              key={dates[i] ?? i}
              x={cx}
              y={topBase - h}
              width={bw}
              height={h}
              fill={color}
              opacity={0.72}
              rx={0.5}
            >
              <title>
                {`${dates[i]} · 成交额 ${fmtTurnover(v)} · 成交额加权系数 ${fmtSigned(coef)} · 主力广度(额) ${fmtPct100(mainAmt[i], 0)}`}
              </title>
            </rect>
          );
        })}
        <line x1={left} x2={width - right} y1={mid} y2={mid} stroke="#cbd5e1" strokeWidth="1" />
        <path d={vwPath} fill="none" stroke="#f59e0b" strokeWidth="1.8" />
        <path d={mainPath} fill="none" stroke="#7c3aed" strokeWidth="1.6" strokeDasharray="4 3" />
        <text x={2} y={topBase - topH + 4} fontSize="8" fill="#94a3b8">{fmtTurnover(maxTurn)}</text>
        <text x={2} y={topBase + 3} fontSize="8" fill="#94a3b8">0</text>
        <text x={2} y={mid + 3} fontSize="8" fill="#94a3b8">0</text>
        <text x={2} y={mid - span + 3} fontSize="8" fill="#94a3b8">+1</text>
        <text x={2} y={mid + span + 3} fontSize="8" fill="#94a3b8">−1</text>
        {[0, Math.floor(dates.length / 2), dates.length - 1].map((i) =>
          i >= 0 && dates[i] ? (
            <text
              key={`x-${i}`}
              x={left + (i + 0.5) * barW}
              y={198}
              fontSize="8"
              fill="#94a3b8"
              textAnchor={i === 0 ? 'start' : i === dates.length - 1 ? 'end' : 'middle'}
            >
              {dates[i].slice(5)}
            </text>
          ) : null,
        )}
      </svg>

      <div className="flex items-center gap-3 text-[9px] text-slate-400 flex-wrap">
        <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-red-500/80" />成交额（偏涨）</span>
        <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-emerald-500/80" />成交额（偏跌）</span>
        <span className="inline-flex items-center gap-1"><span className="w-4 h-0.5 bg-amber-500" />成交额加权涨跌系数（−1~1）</span>
        <span className="inline-flex items-center gap-1"><span className="w-4 h-0.5 bg-purple-600 border-dashed" />主力成交额加权广度（0~1）</span>
        <span className="ml-auto">主力=超大单+大单；描述当前结构，不是方向预测</span>
      </div>
    </div>
  );
};
