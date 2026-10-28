import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { ActualPoint, FanPoint } from '../services/sentimentService';

/** 其他指数的中位路径（相对当前点的 %），用于三指数对比 */
export interface ComparePath {
  name: string;
  color: string;
  p50: number[];
}

interface SentimentFanChartProps {
  history: { date: string; close: number }[];
  fan: FanPoint[];
  last: number;
  horizon: number;
  height?: number;
  /** 回看模式下的真实走势（相对起点的 %）；最新一期为空。 */
  actual?: ActualPoint[];
  /** 真实未来交易日（as-of 模式）；最新一期为空时按工作日估算 */
  forwardDates?: string[];
  compare?: ComparePath[];
  /** 与历史段同源的广度压力分（0~100），叠加在价格图上、用右轴 0–100 */
  stress?: { date: string; stress: number }[];
  /** 单击历史某天：回看那一天的预测（拖动仍为平移，双击复位） */
  onPickDate?: (date: string) => void;
}

const W = 780;
const PAD = { left: 62, right: 84, top: 18, bottom: 30 };
const GRID_LINES = 4;
const UP = '#dc2626';
const DOWN = '#059669';
const HISTORY = '#334155';
const GRID = '#e2e8f0';
/** 默认视窗宽度（数据点数）：60 日历史 + 20 日前瞻 */
const DEFAULT_WINDOW = 80;

const fmtPct = (v: number): string => `${v > 0 ? '+' : ''}${v.toFixed(1)}%`;
const fmtPrice = (v: number): string => (v >= 1000 ? v.toFixed(0) : v.toFixed(1));
const mmdd = (s: string): string => (s && s.length >= 10 ? s.slice(5) : s);

/** 无真实日历时按工作日推算（不处理节假日，因此标签带「约」）。 */
export function estimateBusinessDates(startIso: string, n: number): string[] {
  const d = new Date(`${startIso}T00:00:00`);
  const out: string[] = [];
  while (out.length < n) {
    d.setDate(d.getDate() + 1);
    const wd = d.getDay();
    if (wd === 0 || wd === 6) continue;
    out.push(
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`,
    );
  }
  return out;
}

/**
 * 未来 N 日「历史同状态路径分位」扇形图 + 压力分叠加（右轴 0–100）。
 *
 * - 压力分用**纵向渐变**描边：上绿（冷/超卖）→ 下红（热/追高），与右轴刻度同色系；
 * - 支持**按住拖动平移**时间轴（默认窗口 80 点：60 日历史 + 20 日前瞻），双击复位；
 * - 回看模式叠加黑色实线＝之后真实走势；对比指数只画中位虚线。
 */
export const SentimentFanChart: React.FC<SentimentFanChartProps> = ({
  history,
  fan,
  last,
  horizon,
  height = 250,
  actual = [],
  forwardDates = [],
  compare = [],
  stress = [],
  onPickDate,
}) => {
  const H = history.length;
  const F = fan.length;
  const total = H + F - 1;
  const windowSize = Math.min(DEFAULT_WINDOW, total);

  const [viewEnd, setViewEnd] = useState<number>(total - 1);
  useEffect(() => setViewEnd(total - 1), [total]);
  const viewEndClamped = Math.min(Math.max(viewEnd, windowSize), total - 1);
  const viewStart = Math.max(0, viewEndClamped - windowSize);

  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ x: number; y: number; end: number; span: number; moved: boolean } | null>(null);

  const plotW = W - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;

  const geom = useMemo(() => {
    const x = (i: number): number =>
      PAD.left + ((i - viewStart) / Math.max(viewEndClamped - viewStart, 1e-9)) * plotW;
    // 反转刻度：0（过热）在顶、100（超卖）在底 —— 与指数同向，价格跌、压力线也往下
    const stressY = (v: number): number => PAD.top + (v / 100) * plotH;

    const inView = (i: number): boolean => i >= viewStart - 0.5 && i <= viewEndClamped + 0.5;
    const priceAt = (pct: number): number => last * (1 + pct / 100);

    // 只按「当前视窗内」的价格定 Y 轴范围，拖动时纵向也能自适应
    const candidates: number[] = [];
    history.forEach((h, i) => {
      if (inView(i)) candidates.push(h.close);
    });
    fan.forEach((p) => {
      if (inView(H - 1 + p.k)) candidates.push(priceAt(p.p10), priceAt(p.p90));
    });
    actual.forEach((a) => {
      if (inView(H - 1 + a.k)) candidates.push(priceAt(a.pct));
    });
    compare.forEach((c) => {
      c.p50.forEach((v, i) => {
        if (inView(H - 1 + i)) candidates.push(priceAt(v));
      });
    });
    const finite = candidates.filter((v) => Number.isFinite(v));
    const rawMin = finite.length ? Math.min(...finite) : last * 0.97;
    const rawMax = finite.length ? Math.max(...finite) : last * 1.03;
    const pad = (rawMax - rawMin) * 0.1 || rawMax * 0.02 || 1;
    const min = rawMin - pad;
    const max = rawMax + pad;
    const y = (price: number): number =>
      PAD.top + (1 - (price - min) / Math.max(max - min, 1e-9)) * plotH;

    const histPath = history
      .map((h, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${y(h.close).toFixed(2)}`)
      .join(' ');

    const fanPrices = fan.map((p) => ({
      p10: priceAt(p.p10), p25: priceAt(p.p25), p50: priceAt(p.p50),
      p75: priceAt(p.p75), p90: priceAt(p.p90),
    }));

    const band = (lower: (i: number) => number, upper: (i: number) => number): string => {
      const up = fan.map((p, i) => `${x(H - 1 + p.k).toFixed(2)},${y(upper(i)).toFixed(2)}`);
      const down = fan
        .slice()
        .reverse()
        .map((p, ri) => {
          const i = F - 1 - ri;
          return `${x(H - 1 + p.k).toFixed(2)},${y(lower(i)).toFixed(2)}`;
        });
      return `M${up.join(' L')} L${down.join(' L')} Z`;
    };

    const medianPath = fan
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(H - 1 + p.k).toFixed(2)},${y(fanPrices[i].p50).toFixed(2)}`)
      .join(' ');
    const gridVals = Array.from({ length: GRID_LINES + 1 }, (_, i) => min + ((max - min) * i) / GRID_LINES);

    const actualPath = actual
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(H - 1 + p.k).toFixed(2)},${y(priceAt(p.pct)).toFixed(2)}`)
      .join(' ');
    const actualEnd = actual.length
      ? { x: x(H - 1 + actual[actual.length - 1].k), y: y(priceAt(actual[actual.length - 1].pct)) }
      : null;

    const comparePaths = compare.map((c) => ({
      ...c,
      d: c.p50
        .map((v, i) => `${i === 0 ? 'M' : 'L'}${x(H - 1 + i).toFixed(2)},${y(priceAt(v)).toFixed(2)}`)
        .join(' '),
    }));

    const stressPath = stress.length
      ? stress.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${stressY(p.stress).toFixed(2)}`).join(' ')
      : '';
    const stressArea = stress.length
      ? `${stressPath} L${x(stress.length - 1).toFixed(2)},${stressY(0).toFixed(2)} L${x(0).toFixed(2)},${stressY(0).toFixed(2)} Z`
      : '';

    return {
      x, y, stressY, histPath, medianPath, gridVals, actualPath, actualEnd, comparePaths,
      stressPath, stressArea, fanPrices,
      upperOuter: band((i) => fanPrices[i].p50, (i) => fanPrices[i].p90),
      lowerOuter: band((i) => fanPrices[i].p10, (i) => fanPrices[i].p50),
      upperInner: band((i) => fanPrices[i].p50, (i) => fanPrices[i].p75),
      lowerInner: band((i) => fanPrices[i].p25, (i) => fanPrices[i].p50),
      anchorX: x(H - 1),
      lastX: x(total - 1),
      lastFan: fanPrices[F - 1],
      lastPct: fan[F - 1],
      anchorVisible: inView(H - 1),
      fanEndVisible: inView(total - 1),
    };
  }, [history, fan, last, height, actual, compare, stress, viewStart, viewEndClamped, H, F, total, plotH, plotW]);

  // 拖动平移：像拖时间轴一样，向右拖＝回看更早
  // 单击（位移 < 3px）＝选中那一天，回看当天的模型预测与实际走势
  const onPointerDown = (e: React.PointerEvent<SVGSVGElement>): void => {
    dragRef.current = {
      x: e.clientX,
      y: e.clientY,
      end: viewEndClamped,
      span: viewEndClamped - viewStart,
      moved: false,
    };
    try {
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      // jsdom / 老浏览器没有 pointer capture：拖动仍可用，只是移出 SVG 会中断
    }
  };
  const onPointerMove = (e: React.PointerEvent<SVGSVGElement>): void => {
    const d = dragRef.current;
    const svg = svgRef.current;
    if (!d || !svg) return;
    if (Math.abs(e.clientX - d.x) > 3 || Math.abs(e.clientY - d.y) > 3) d.moved = true;
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const dxVb = ((e.clientX - d.x) * W) / rect.width;
    const dIdx = (-dxVb / plotW) * d.span;
    setViewEnd(Math.min(Math.max(d.end + dIdx, windowSize), total - 1));
  };
  const onPointerUp = (e: React.PointerEvent<SVGSVGElement>): void => {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d || d.moved || !onPickDate) return;
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const vx = ((e.clientX - rect.left) * W) / rect.width;
    if (vx < PAD.left || vx > W - PAD.right) return;
    const idx = viewStart + ((vx - PAD.left) / plotW) * (viewEndClamped - viewStart);
    const i = Math.round(idx);
    if (i >= 0 && i < H) onPickDate(history[i].date);
  };
  const cancelDrag = (): void => {
    dragRef.current = null;
  };

  // 日期轴：按当前视窗自适应
  const dates = useMemo(() => {
    const anchorDate = history[H - 1]?.date ?? '';
    const future = forwardDates.length
      ? forwardDates
      : anchorDate
        ? estimateBusinessDates(anchorDate, horizon)
        : [];
    const all = [...history.map((h) => h.date), ...future];
    const at = (i: number): string => {
      const idx = Math.min(Math.max(Math.round(i), 0), all.length - 1);
      return all[idx] ?? '';
    };
    const estimated = forwardDates.length === 0;
    const isFuture = (i: number): boolean => i > H - 1;
    const mid = Math.round((viewStart + viewEndClamped) / 2);
    return {
      left: mmdd(at(viewStart)),
      mid: mmdd(at(mid)),
      right: `${isFuture(viewEndClamped) && estimated ? '约' : ''}${mmdd(at(viewEndClamped))}`,
      anchor: mmdd(anchorDate),
    };
  }, [history, forwardDates, horizon, viewStart, viewEndClamped, H]);

  const upWider = geom.lastPct.p90 - geom.lastPct.p50 > geom.lastPct.p50 - geom.lastPct.p10;

  return (
    <div className="w-full">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${height}`}
        className="w-full h-auto select-none"
        style={{ cursor: 'grab', touchAction: 'none' }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={cancelDrag}
        onPointerCancel={cancelDrag}
        onDoubleClick={() => setViewEnd(total - 1)}
        role="img"
        data-view-start={viewStart.toFixed(1)}
        data-view-end={viewEndClamped.toFixed(1)}
        aria-label={`未来 ${horizon} 个交易日的历史同状态路径分位区间`}
      >
        <defs>
          <linearGradient id="sentiment-hist-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={HISTORY} stopOpacity="0.10" />
            <stop offset="100%" stopColor={HISTORY} stopOpacity="0" />
          </linearGradient>
          {/* 压力分纵向渐变：上绿（冷/超卖）→ 下红（热/追高），与右轴刻度一一对应 */}
          <linearGradient id="stress-grad" gradientUnits="userSpaceOnUse"
                          x1="0" y1={PAD.top} x2="0" y2={PAD.top + plotH}>
            <stop offset="0%" stopColor="#ef4444" />
            <stop offset="25%" stopColor="#f59e0b" />
            <stop offset="50%" stopColor="#94a3b8" />
            <stop offset="75%" stopColor="#14b8a6" />
            <stop offset="100%" stopColor="#10b981" />
          </linearGradient>
          <clipPath id="sentiment-plot-clip">
            <rect x={PAD.left} y={PAD.top} width={plotW} height={plotH} />
          </clipPath>
        </defs>

        {geom.gridVals.map((v) => (
          <g key={v}>
            <line x1={PAD.left} x2={W - PAD.right} y1={geom.y(v)} y2={geom.y(v)} stroke={GRID} strokeWidth={1} />
            <text x={PAD.left - 8} y={geom.y(v) + 3} textAnchor="end" fontSize={10} fill="#94a3b8"
                  fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{fmtPrice(v)}</text>
          </g>
        ))}

        <g clipPath="url(#sentiment-plot-clip)">
          <path d={`${geom.histPath} L${geom.anchorX.toFixed(2)},${PAD.top + plotH} L${PAD.left},${PAD.top + plotH} Z`}
                fill="url(#sentiment-hist-fill)" stroke="none" />
          <path d={geom.histPath} fill="none" stroke={HISTORY} strokeWidth={1.6} strokeLinejoin="round" />

          <path d={geom.upperOuter} fill={UP} fillOpacity={0.08} />
          <path d={geom.lowerOuter} fill={DOWN} fillOpacity={0.08} />
          <path d={geom.upperInner} fill={UP} fillOpacity={0.16} />
          <path d={geom.lowerInner} fill={DOWN} fillOpacity={0.16} />

          {geom.comparePaths.map((c) => (
            <path key={c.name} d={c.d} fill="none" stroke={c.color} strokeWidth={1.3} strokeDasharray="2 3" opacity={0.85} />
          ))}

          {/* 压力分叠加：右轴 0–100 线性映射，纵向渐变描边 */}
          {stress.length > 0 && (
            <>
              {[20, 40, 60, 80].map((v) => (
                <line key={v} x1={PAD.left} x2={W - PAD.right} y1={geom.stressY(v)} y2={geom.stressY(v)}
                      stroke="#a78bfa" strokeWidth={0.7} strokeDasharray="2 5" opacity={0.35} />
              ))}
              <path d={geom.stressArea} fill="url(#stress-grad)" opacity={0.06} />
              <path d={geom.stressPath} fill="none" stroke="url(#stress-grad)" strokeWidth={1.8} strokeLinejoin="round" />
              <circle cx={geom.x(stress.length - 1)} cy={geom.stressY(stress[stress.length - 1].stress)} r={2.8}
                      fill="#fff" stroke="#7c3aed" strokeWidth={2} />
            </>
          )}

          {geom.actualPath && (
            <>
              <path d={geom.actualPath} fill="none" stroke="#0f172a" strokeWidth={2.2} strokeLinejoin="round" />
              {geom.actualEnd && (
                <circle cx={geom.actualEnd.x} cy={geom.actualEnd.y} r={3.4} fill="#0f172a" stroke="#fff" strokeWidth={1.5} />
              )}
            </>
          )}

          <path d={geom.medianPath} fill="none" stroke={geom.lastPct.p50 >= 0 ? UP : DOWN}
                strokeWidth={2.2} strokeDasharray="5 4" strokeLinejoin="round" />
          <circle cx={geom.lastX} cy={geom.y(geom.lastFan.p50)} r={3.6} fill="#fff"
                  stroke={geom.lastPct.p50 >= 0 ? UP : DOWN} strokeWidth={2} />

          {geom.anchorVisible && (
            <line x1={geom.anchorX} x2={geom.anchorX} y1={PAD.top} y2={PAD.top + plotH}
                  stroke="#94a3b8" strokeWidth={1} strokeDasharray="3 3" />
          )}
        </g>

        {geom.anchorVisible && (
          <text x={geom.anchorX} y={PAD.top - 5} textAnchor="middle" fontSize={9} fill="#64748b" fontWeight={700}>
            今日
          </text>
        )}

        {/* 右轴：压力分 0–100（画在绘图区内侧，白描边保证压在扇形带上也读得清） */}
        {stress.length > 0 && (
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
            {[0, 25, 50, 75, 100].map((v) => (
              <text key={v} x={W - PAD.right - 2} y={geom.stressY(v) + 3} textAnchor="end" fontSize={8}
                    fill="#7c3aed" fontWeight={700} opacity={0.85}
                    paintOrder="stroke" stroke="#ffffff" strokeWidth={2.5} strokeLinejoin="round">
                {v}
              </text>
            ))}
            <text x={W - PAD.right - 2} y={geom.stressY(0) + 12} textAnchor="end" fontSize={7} fill="#ef4444"
                  fontWeight={700} opacity={0.8}>过热</text>
            <text x={W - PAD.right - 2} y={geom.stressY(100) - 5} textAnchor="end" fontSize={7} fill="#10b981"
                  fontWeight={700} opacity={0.85}>超卖</text>
          </g>
        )}

        {/* 扇形末端读数（仅在视窗包含末端时显示） */}
        {geom.fanEndVisible && (
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
            {[
              { v: geom.lastFan.p90, pct: geom.lastPct.p90, color: UP },
              { v: geom.lastFan.p50, pct: geom.lastPct.p50, color: geom.lastPct.p50 >= 0 ? UP : DOWN },
              { v: geom.lastFan.p10, pct: geom.lastPct.p10, color: DOWN },
            ].map((it) => (
              <g key={it.pct}>
                <line x1={geom.lastX + 6} x2={geom.lastX + 16} y1={geom.y(it.v)} y2={geom.y(it.v)}
                      stroke={it.color} strokeWidth={1} strokeOpacity={0.5} />
                <text x={geom.lastX + 20} y={geom.y(it.v) + 4}
                      fontSize={it.pct === geom.lastPct.p50 ? 12 : 10} fill={it.color} fontWeight={800}
                      paintOrder="stroke" stroke="#ffffff" strokeWidth={3} strokeLinejoin="round">
                  {it.pct === geom.lastPct.p50 ? `典型 ${fmtPct(it.pct)}` : fmtPct(it.pct)}
                </text>
              </g>
            ))}
          </g>
        )}

        {/* 日期轴：随视窗移动 */}
        <text x={PAD.left} y={height - 8} fontSize={9} fill="#94a3b8"
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{dates.left}</text>
        <text x={PAD.left + plotW / 2} y={height - 8} textAnchor="middle" fontSize={9} fill="#94a3b8"
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{dates.mid}</text>
        <text x={W - PAD.right} y={height - 8} textAnchor="end" fontSize={9} fill="#94a3b8"
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{dates.right}</text>
        {geom.anchorVisible && (
          <text x={geom.anchorX} y={height - 8} textAnchor="middle" fontSize={9} fill="#64748b" fontWeight={700}
                fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{dates.anchor}</text>
        )}
      </svg>

      <div className="flex items-center gap-4 px-1 -mt-1 text-[10px] text-slate-500 flex-wrap">
        <span className="text-slate-400">
          {onPickDate
            ? '按住拖动＝平移；单击历史某天＝回看当天预测（双击复位）'
            : '按住图表左右拖动＝回看更早（双击复位）'}
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-2 rounded-sm" style={{ background: `${UP}29` }} />
          红色区域 ＝ 上涨空间
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-2 rounded-sm" style={{ background: `${DOWN}29` }} />
          绿色区域 ＝ 下跌空间
        </span>
        {stress.length > 0 && (
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-3 h-0.5 rounded-sm"
                  style={{ background: 'linear-gradient(90deg,#10b981,#94a3b8,#ef4444)' }} />
            压力分（右轴：上=过热 下=超卖）
          </span>
        )}
        {actual.length > 0 && (
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-3 h-0.5 rounded-sm bg-slate-900" />
            黑线 ＝ 之后实际走势
          </span>
        )}
        {compare.map((c) => (
          <span key={c.name} className="flex items-center gap-1.5">
            <span className="inline-block w-3 h-0.5" style={{ background: c.color }} />
            {c.name} 典型路径
          </span>
        ))}
        <span className="ml-auto">
          浅色带 = 历史同类情形里八成落在这；{upWider ? '向上空间更宽' : '向下空间更宽'}
        </span>
      </div>
    </div>
  );
};
