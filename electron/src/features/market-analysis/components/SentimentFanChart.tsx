import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { ActualPoint, FanPoint } from '../services/sentimentService';

/** 其他指数的中位路径（相对当前点的 %），用于三指数对比 */
export interface ComparePath {
  name: string;
  color: string;
  p50: number[];
}

/** 与主图日期完全对齐的成交量序列（按交易日匹配，不做重采样）。 */
export interface FanVolumeSeries {
  dates: string[];
  turnover_yi: (number | null)[];
  vw_breadth: (number | null)[];
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
  /** 与历史段同源的广度压力分（0~100），画在价格图下方细条，不再压在扇形上 */
  stress?: { date: string; stress: number }[];
  /** 成交量副图：与历史价格共用 x 轴，日期逐日对齐 */
  volume?: FanVolumeSeries;
  /** 单击历史某天：回看那一天的预测（拖动仍为平移，双击复位） */
  onPickDate?: (date: string) => void;
}

const W = 780;
const PAD = { left: 58, right: 86, top: 16, bottom: 26 };
const GRID_LINES = 4;
const UP = '#dc2626';
const DOWN = '#059669';
const HISTORY = '#334155';
const GRID = '#e2e8f0';
const DEFAULT_WINDOW = 80;

const fmtPct = (v: number): string => `${v > 0 ? '+' : ''}${v.toFixed(1)}%`;
const fmtPrice = (v: number): string => (v >= 1000 ? v.toFixed(0) : v.toFixed(1));
const fmtTurnover = (v: number): string => (v >= 10000 ? `${(v / 10000).toFixed(1)}万亿` : `${v.toFixed(0)}亿`);
const mmdd = (s: string): string => (s && s.length >= 10 ? s.slice(5) : s);

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
 * 未来 N 日「历史同状态路径分位」主图（v2 清晰版）。
 *
 * 版式：上=价格扇形（历史线 + 80%带 + 50%带 + 典型路径）；
 *       中=压力分细条（0=过热、100=超卖，不再压在价格上）；
 *       下=成交量柱（与历史日期逐日对齐，红=成交额加权偏涨、绿=偏跌）。
 * 拖动/单击/双击交互保持不变；对比指数默认由外层开关控制。
 */
export const SentimentFanChart: React.FC<SentimentFanChartProps> = ({
  history,
  fan,
  last,
  horizon,
  height = 340,
  actual = [],
  forwardDates = [],
  compare = [],
  stress = [],
  volume,
  onPickDate,
}) => {
  const H = history.length;
  const F = fan.length;
  const total = H + F - 1;
  const windowSize = Math.min(DEFAULT_WINDOW, total);

  const [viewEnd, setViewEnd] = useState<number>(total - 1);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  useEffect(() => setViewEnd(total - 1), [total]);
  const viewEndClamped = Math.min(Math.max(viewEnd, windowSize), total - 1);
  const viewStart = Math.max(0, viewEndClamped - windowSize);

  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ x: number; y: number; end: number; span: number; moved: boolean } | null>(null);

  const plotW = W - PAD.left - PAD.right;
  const hasVolume = !!volume && volume.dates.length > 0;
  const hasStress = stress.length > 0;
  const volumeH = hasVolume ? 44 : 0;
  const stressH = hasStress ? 20 : 0;
  const gap = 8;
  const priceH =
    height - PAD.top - PAD.bottom - volumeH - stressH - (hasVolume ? gap : 0) - (hasStress ? gap : 0);
  const priceBottom = PAD.top + priceH;
  const stressTop = priceBottom + (hasStress ? gap : 0);
  const stressBottom = stressTop + stressH;
  const volumeTop = stressBottom + (hasVolume ? gap : 0);
  const volumeBase = height - PAD.bottom;

  const allDates = useMemo(() => {
    const anchorDate = history[H - 1]?.date ?? '';
    const future = forwardDates.length
      ? forwardDates
      : anchorDate
        ? estimateBusinessDates(anchorDate, horizon)
        : [];
    return [...history.map((h) => h.date), ...future];
  }, [history, forwardDates, horizon, H]);

  const volMap = useMemo(() => {
    const map = new Map<string, { turnover: number | null; vw: number | null }>();
    if (volume) {
      volume.dates.forEach((d, i) =>
        map.set(d, { turnover: volume.turnover_yi[i] ?? null, vw: volume.vw_breadth[i] ?? null }),
      );
    }
    return map;
  }, [volume]);

  const stressMap = useMemo(() => new Map(stress.map((p) => [p.date, p.stress])), [stress]);

  const geom = useMemo(() => {
    const x = (i: number): number =>
      PAD.left + ((i - viewStart) / Math.max(viewEndClamped - viewStart, 1e-9)) * plotW;
    const stressY = (v: number): number => stressTop + (v / 100) * stressH;
    const inView = (i: number): boolean => i >= viewStart - 0.5 && i <= viewEndClamped + 0.5;
    const priceAt = (pct: number): number => last * (1 + pct / 100);

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
      PAD.top + (1 - (price - min) / Math.max(max - min, 1e-9)) * priceH;

    const histPath = history
      .map((h, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${y(h.close).toFixed(2)}`)
      .join(' ');
    const fanPrices = fan.map((p) => ({
      p10: priceAt(p.p10), p25: priceAt(p.p25), p50: priceAt(p.p50),
      p75: priceAt(p.p75), p90: priceAt(p.p90),
    }));
    const band = (lower: (i: number) => number, upper: (i: number) => number): string => {
      const up = fan.map((p, i) => `${x(H - 1 + p.k).toFixed(2)},${y(upper(i)).toFixed(2)}`);
      const down = fan.slice().reverse().map((p, ri) => {
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
      d: c.p50.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(H - 1 + i).toFixed(2)},${y(priceAt(v)).toFixed(2)}`).join(' '),
    }));
    const stressPath = stress.length
      ? stress.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${stressY(p.stress).toFixed(2)}`).join(' ')
      : '';

    // 成交量柱：只画历史段，日期逐日映射到价格 x 轴（未来段留空）
    const volByDate = new Map<string, { turnover: number | null; vw: number | null }>();
    if (volume) {
      volume.dates.forEach((d, i) => volByDate.set(d, { turnover: volume.turnover_yi[i] ?? null, vw: volume.vw_breadth[i] ?? null }));
    }
    const visibleVol = history
      .map((h, i) => ({ i, ...(volByDate.get(h.date) ?? { turnover: null, vw: null }) }))
      .filter((v) => v.i >= Math.max(0, Math.floor(viewStart)) && v.i <= Math.min(H - 1, Math.ceil(viewEndClamped)));
    const maxTurn = Math.max(...visibleVol.map((v) => v.turnover ?? 0), 1);
    const barW = Math.max(1, (plotW / Math.max(viewEndClamped - viewStart, 1)) * 0.58);
    const volBars = visibleVol.map((v) => {
      const hgt = v.turnover == null ? 0 : Math.max(1, (v.turnover / maxTurn) * (volumeH - 4));
      return {
        key: history[v.i].date,
        x: x(v.i) - barW / 2,
        y: volumeBase - hgt,
        h: hgt,
        w: barW,
        color: v.vw == null ? '#cbd5e1' : v.vw >= 0 ? UP : DOWN,
        title: `${history[v.i].date} · 成交额 ${v.turnover == null ? '—' : fmtTurnover(v.turnover)} · 成交额加权系数 ${v.vw == null ? '—' : (v.vw > 0 ? '+' : '') + v.vw.toFixed(3)}`,
      };
    });

    return {
      x, y, stressY, histPath, medianPath, gridVals, actualPath, actualEnd, comparePaths,
      stressPath, fanPrices, volBars, maxTurn,
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
  }, [history, fan, last, actual, compare, stress, volume, viewStart, viewEndClamped, H, F, total, plotW, priceH, stressH, stressTop, volumeH, volumeBase]);

  const onPointerDown = (e: React.PointerEvent<SVGSVGElement>): void => {
    dragRef.current = { x: e.clientX, y: e.clientY, end: viewEndClamped, span: viewEndClamped - viewStart, moved: false };
    try { e.currentTarget.setPointerCapture?.(e.pointerId); } catch { /* jsdom */ }
  };
  const onPointerMove = (e: React.PointerEvent<SVGSVGElement>): void => {
    const d = dragRef.current;
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    if (!d) {
      // 未拖动时：鼠标划过 → 显示该交易日的日期/价格/压力/成交额
      const vx = ((e.clientX - rect.left) * W) / rect.width;
      if (vx < PAD.left || vx > W - PAD.right) {
        setHoverIdx(null);
        return;
      }
      const idx = viewStart + ((vx - PAD.left) / plotW) * (viewEndClamped - viewStart);
      setHoverIdx(Math.round(idx));
      return;
    }
    if (Math.abs(e.clientX - d.x) > 3 || Math.abs(e.clientY - d.y) > 3) d.moved = true;
    setHoverIdx(null);
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
    setHoverIdx(null);
  };

  const dates = useMemo(() => {
    const anchorDate = history[H - 1]?.date ?? '';
    const at = (i: number): string => allDates[Math.min(Math.max(Math.round(i), 0), allDates.length - 1)] ?? '';
    const estimated = forwardDates.length === 0;
    const isFuture = (i: number): boolean => i > H - 1;
    const mid = Math.round((viewStart + viewEndClamped) / 2);
    return {
      left: mmdd(at(viewStart)),
      mid: mmdd(at(mid)),
      right: `${isFuture(viewEndClamped) && estimated ? '约' : ''}${mmdd(at(viewEndClamped))}`,
      anchor: mmdd(anchorDate),
    };
  }, [allDates, forwardDates.length, viewStart, viewEndClamped, H]);

  const hover = useMemo(() => {
    if (hoverIdx === null || allDates.length === 0) return null;
    const i = Math.min(Math.max(Math.round(hoverIdx), 0), total - 1);
    const date = allDates[Math.min(i, allDates.length - 1)] ?? '';
    const histClose = i <= H - 1 ? history[i]?.close ?? null : null;
    const k = i - (H - 1);
    const f = k >= 0 && k < F ? fan[k] : null;
    const vol = volMap.get(date) ?? null;
    const stressV = stressMap.get(date) ?? null;
    return {
      x: PAD.left + ((i - viewStart) / Math.max(viewEndClamped - viewStart, 1e-9)) * plotW,
      i,
      date,
      histClose,
      f,
      stress: stressV,
      turnover: vol?.turnover ?? null,
      vw: vol?.vw ?? null,
    };
  }, [hoverIdx, allDates, total, H, F, history, fan, volMap, stressMap, viewStart, viewEndClamped, plotW]);

  const upWider = geom.lastPct.p90 - geom.lastPct.p50 > geom.lastPct.p50 - geom.lastPct.p10;
  const latestStress = stress.length ? stress[stress.length - 1].stress : null;

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
          <linearGradient id="stress-grad" gradientUnits="userSpaceOnUse" x1="0" y1={stressTop} x2="0" y2={stressTop + stressH}>
            <stop offset="0%" stopColor="#ef4444" />
            <stop offset="50%" stopColor="#94a3b8" />
            <stop offset="100%" stopColor="#10b981" />
          </linearGradient>
          <clipPath id="sentiment-plot-clip">
            <rect x={PAD.left} y={PAD.top} width={plotW} height={priceH} />
          </clipPath>
        </defs>

        {/* 未来区域淡底：与历史段一眼分开 */}
        {geom.anchorVisible && (
          <rect x={geom.anchorX} y={PAD.top} width={Math.max(0, W - PAD.right - geom.anchorX)} height={priceH} fill="#f8fafc" />
        )}

        {geom.gridVals.map((v) => (
          <g key={v}>
            <line x1={PAD.left} x2={W - PAD.right} y1={geom.y(v)} y2={geom.y(v)} stroke={GRID} strokeWidth={1} />
            <text x={PAD.left - 8} y={geom.y(v) + 3} textAnchor="end" fontSize={9} fill="#94a3b8" fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{fmtPrice(v)}</text>
          </g>
        ))}

        <g clipPath="url(#sentiment-plot-clip)">
          <path d={`${geom.histPath} L${geom.anchorX.toFixed(2)},${PAD.top + priceH} L${PAD.left},${PAD.top + priceH} Z`} fill="url(#sentiment-hist-fill)" stroke="none" />
          <path d={geom.histPath} fill="none" stroke={HISTORY} strokeWidth={1.7} strokeLinejoin="round" />
          <path d={geom.upperOuter} fill={UP} fillOpacity={0.07} />
          <path d={geom.lowerOuter} fill={DOWN} fillOpacity={0.07} />
          <path d={geom.upperInner} fill={UP} fillOpacity={0.16} />
          <path d={geom.lowerInner} fill={DOWN} fillOpacity={0.16} />
          {geom.comparePaths.map((c) => (
            <path key={c.name} d={c.d} fill="none" stroke={c.color} strokeWidth={1.2} strokeDasharray="2 3" opacity={0.8} />
          ))}
          {geom.actualPath && (
            <>
              <path d={geom.actualPath} fill="none" stroke="#0f172a" strokeWidth={2.2} strokeLinejoin="round" />
              {geom.actualEnd && <circle cx={geom.actualEnd.x} cy={geom.actualEnd.y} r={3.4} fill="#0f172a" stroke="#fff" strokeWidth={1.5} />}
            </>
          )}
          <path d={geom.medianPath} fill="none" stroke={geom.lastPct.p50 >= 0 ? UP : DOWN} strokeWidth={2.2} strokeDasharray="5 4" strokeLinejoin="round" />
          <circle cx={geom.lastX} cy={geom.y(geom.lastFan.p50)} r={3.6} fill="#fff" stroke={geom.lastPct.p50 >= 0 ? UP : DOWN} strokeWidth={2} />
          {geom.anchorVisible && (
            <line x1={geom.anchorX} x2={geom.anchorX} y1={PAD.top} y2={PAD.top + priceH} stroke="#94a3b8" strokeWidth={1} strokeDasharray="3 3" />
          )}
        </g>

        {geom.anchorVisible && (
          <text x={geom.anchorX} y={PAD.top - 4} textAnchor="middle" fontSize={9} fill="#64748b" fontWeight={700}>今日</text>
        )}
        {geom.fanEndVisible && (
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
            <text x={geom.lastX + 8} y={geom.y(geom.lastFan.p50) + 3} fontSize={11} fill={geom.lastPct.p50 >= 0 ? UP : DOWN} fontWeight={800}>典型 {fmtPct(geom.lastPct.p50)}</text>
            <text x={geom.lastX + 8} y={geom.y(geom.lastFan.p50) + 14} fontSize={8} fill="#94a3b8">80% {fmtPct(geom.lastPct.p10)}~{fmtPct(geom.lastPct.p90)}</text>
          </g>
        )}

        {/* 压力分细条：不再叠在价格上 */}
        {hasStress && (
          <g>
            <rect x={PAD.left} y={stressTop} width={plotW} height={stressH} fill="#faf5ff" opacity={0.65} rx={2} />
            {[20, 40, 60, 80].map((v) => (
              <line key={v} x1={PAD.left} x2={W - PAD.right} y1={geom.stressY(v)} y2={geom.stressY(v)} stroke="#a78bfa" strokeWidth={0.6} strokeDasharray="2 4" opacity={0.3} />
            ))}
            <path d={geom.stressPath} fill="none" stroke="url(#stress-grad)" strokeWidth={1.7} strokeLinejoin="round" />
            {latestStress != null && (
              <circle cx={geom.x(stress.length - 1)} cy={geom.stressY(latestStress)} r={2.6} fill="#fff" stroke="#7c3aed" strokeWidth={1.8} />
            )}
            <text x={PAD.left - 6} y={stressTop + 11} textAnchor="end" fontSize={8} fill="#7c3aed" fontWeight={700}>压力</text>
            {latestStress != null && (
              <text x={W - PAD.right + 4} y={stressTop + 11} fontSize={9} fill="#7c3aed" fontWeight={800} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{latestStress.toFixed(1)}</text>
            )}
          </g>
        )}

        {/* 成交量副图：与历史日期逐日对齐 */}
        {hasVolume && (
          <g>
            <line x1={PAD.left} x2={W - PAD.right} y1={volumeBase} y2={volumeBase} stroke="#e2e8f0" strokeWidth={1} />
            {geom.volBars.map((b) => (
              <rect key={b.key} x={b.x} y={b.y} width={b.w} height={b.h} fill={b.color} opacity={0.66} rx={0.5}>
                <title>{b.title}</title>
              </rect>
            ))}
            <text x={PAD.left - 6} y={volumeTop + 10} textAnchor="end" fontSize={8} fill="#94a3b8" fontWeight={700}>成交额</text>
            <text x={PAD.left - 6} y={volumeTop + 20} textAnchor="end" fontSize={7} fill="#cbd5e1">{fmtTurnover(geom.maxTurn)}</text>
          </g>
        )}

        {/* 鼠标划过：十字线 + 图表内读数（日期/价格/压力/成交额） */}
        {hover && (
          <g pointerEvents="none">
            <line x1={hover.x} x2={hover.x} y1={PAD.top} y2={volumeBase} stroke="#475569" strokeWidth={0.9} strokeDasharray="3 3" opacity={0.7} />
            <rect x={hover.x - 8} y={PAD.top + 4} width={16} height={16} rx={2} fill="#f8fafc" opacity={0.9} />
            <circle cx={hover.x} cy={PAD.top + 12} r={2.5} fill="#475569" />
            {(() => {
              const boxW = 166;
              const boxH = 46 + (hover.stress != null ? 11 : 0) + (hover.turnover != null ? 11 : 0);
              const boxX = hover.x > W / 2 ? PAD.left + 6 : W - PAD.right - boxW - 6;
              const boxY = PAD.top + 6;
              return (
                <g>
                  <rect x={boxX} y={boxY} width={boxW} height={boxH} rx={4} fill="#ffffff" opacity={0.93} stroke="#e2e8f0" />
                  <text x={boxX + 6} y={boxY + 13} fontSize={9} fill="#334155" fontWeight={800} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{hover.date}</text>
                  <text x={boxX + 6} y={boxY + 25} fontSize={9} fill="#475569" fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
                    {hover.histClose != null ? `收盘 ${fmtPrice(hover.histClose)}` : hover.f ? `典型 ${fmtPct(hover.f.p50)}` : '—'}
                  </text>
                  {hover.f && (
                    <text x={boxX + 6} y={boxY + 36} fontSize={8} fill="#94a3b8" fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
                      80% {fmtPct(hover.f.p10)} ~ {fmtPct(hover.f.p90)}
                    </text>
                  )}
                  {hover.stress != null && (
                    <text x={boxX + 6} y={boxY + (hover.f ? 47 : 36)} fontSize={8} fill="#7c3aed" fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">压力 {hover.stress.toFixed(1)}</text>
                  )}
                  {hover.turnover != null && (
                    <text x={boxX + 6} y={boxY + (hover.f ? 58 : 47)} fontSize={8} fill={hover.vw != null && hover.vw < 0 ? '#059669' : '#dc2626'} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
                      成交额 {fmtTurnover(hover.turnover)}{hover.vw != null ? ` · 系数 ${hover.vw > 0 ? '+' : ''}${hover.vw.toFixed(2)}` : ''}
                    </text>
                  )}
                </g>
              );
            })()}
          </g>
        )}

        {/* 日期轴：压力/成交量共用 */}
        <text x={PAD.left} y={height - 6} fontSize={9} fill="#94a3b8" fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{dates.left}</text>
        <text x={PAD.left + plotW / 2} y={height - 6} textAnchor="middle" fontSize={9} fill="#94a3b8" fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{dates.mid}</text>
        <text x={W - PAD.right} y={height - 6} textAnchor="end" fontSize={9} fill="#94a3b8" fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{dates.right}</text>
        {geom.anchorVisible && (
          <text x={geom.anchorX} y={height - 6} textAnchor="middle" fontSize={9} fill="#64748b" fontWeight={700} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">{dates.anchor}</text>
        )}
      </svg>

      <div className="flex items-center gap-3 px-1 -mt-1 text-[10px] text-slate-500 flex-wrap">
        <span className="text-slate-400">{onPickDate ? '拖动＝平移；单击历史某天＝回看当天预测（双击复位）' : '拖动＝回看更早（双击复位）'}</span>
        <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-2 rounded-sm" style={{ background: `${UP}29` }} />上涨空间</span>
        <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-2 rounded-sm" style={{ background: `${DOWN}29` }} />下跌空间</span>
        {hasStress && <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-0.5" style={{ background: 'linear-gradient(90deg,#ef4444,#94a3b8,#10b981)' }} />压力分（下条）</span>}
        {hasVolume && <span className="flex items-center gap-1.5"><span className="inline-block w-2 h-2 rounded-sm bg-red-500/70" />成交额（偏涨）<span className="inline-block w-2 h-2 rounded-sm bg-emerald-500/70 ml-1" />偏跌</span>}
        {actual.length > 0 && <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-0.5 bg-slate-900" />之后实际</span>}
        {compare.map((c) => <span key={c.name} className="flex items-center gap-1.5"><span className="inline-block w-3 h-0.5" style={{ background: c.color }} />{c.name}</span>)}
        <span className="ml-auto">{upWider ? '向上空间更宽' : '向下空间更宽'}</span>
      </div>
    </div>
  );
};
