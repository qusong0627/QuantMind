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
  /** 该成交额对应的指数/宇宙，如「沪市」「沪深300」 */
  scope?: string;
  dates: string[];
  turnover_yi: (number | null)[];
  vw_breadth: (number | null)[];
  /** 主力净额柱（亿元）；barMode=main 时使用 */
  main_net_yi?: (number | null)[];
  retail_net_yi?: (number | null)[];
  barMode?: 'market' | 'main' | 'retail';
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
  /** 主力派发分（0~100，越高=主力越偏卖） */
  mainPressure?: { date: string; value: number | null }[];
  /** 散户追涨分（0~100，越高=越追涨/反向风险） */
  retailScore?: { date: string; value: number | null }[];
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
  height = 430,
  actual = [],
  forwardDates = [],
  compare = [],
  stress = [],
  volume,
  mainPressure = [],
  retailScore = [],
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
  const volumeH = hasVolume ? 64 : 0;
  const gap = 8;
  const priceH = height - PAD.top - PAD.bottom - volumeH - (hasVolume ? gap : 0);
  const priceBottom = PAD.top + priceH;
  const stressTop = PAD.top;
  const volumeTop = priceBottom + (hasVolume ? gap : 0);
  // 压力分画在主图中下部（0 上移，方便读数），带内用色块区分冷热
  const stressBandTop = PAD.top + priceH * 0.36;
  const stressBandH = priceH * 0.46;
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
    const map = new Map<string, { turnover: number | null; vw: number | null; mainNet: number | null; retailNet: number | null }>();
    if (volume) {
      volume.dates.forEach((d, i) =>
        map.set(d, {
          turnover: volume.turnover_yi[i] ?? null,
          vw: volume.vw_breadth[i] ?? null,
          mainNet: volume.main_net_yi?.[i] ?? null,
          retailNet: volume.retail_net_yi?.[i] ?? null,
        }),
      );
    }
    return map;
  }, [volume]);

  const stressMap = useMemo(() => new Map(stress.map((p) => [p.date, p.stress])), [stress]);
  const mainScoreMap = useMemo(
    () => new Map(mainPressure.map((p) => [p.date, p.value])),
    [mainPressure],
  );
  const retailScoreMap = useMemo(
    () => new Map(retailScore.map((p) => [p.date, p.value])),
    [retailScore],
  );

  const geom = useMemo(() => {
    const x = (i: number): number =>
      PAD.left + ((i - viewStart) / Math.max(viewEndClamped - viewStart, 1e-9)) * plotW;
    // 压力分叠加在主图下 38% 带：0=过热在带顶、100=超卖在带底（与价格同向）
    const stressY = (v: number): number => stressBandTop + (v / 100) * stressBandH;
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
    // 纵向 padding 收窄，放大主图波动幅度；p10/p90 仍在视窗内
    const pad = (rawMax - rawMin) * 0.05 || rawMax * 0.015 || 1;
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
    const seriesPath = (map: Map<string, number | null>): string => {
      let out = '';
      let open = false;
      history.forEach((h, i) => {
        const v = map.get(h.date);
        if (typeof v !== 'number' || !Number.isFinite(v)) { open = false; return; }
        out += `${open ? 'L' : 'M'}${x(i).toFixed(2)},${stressY(v).toFixed(2)} `;
        open = true;
      });
      return out.trim();
    };
    const mainScorePath = seriesPath(mainScoreMap);
    const retailScorePath = seriesPath(retailScoreMap);

    // 成交量柱：只画历史段，日期逐日映射到价格 x 轴（未来段留空）
    const volByDate = new Map<string, { turnover: number | null; vw: number | null; mainNet: number | null; retailNet: number | null }>();
    if (volume) {
      volume.dates.forEach((d, i) => volByDate.set(d, {
        turnover: volume.turnover_yi[i] ?? null,
        vw: volume.vw_breadth[i] ?? null,
        mainNet: volume.main_net_yi?.[i] ?? null,
        retailNet: volume.retail_net_yi?.[i] ?? null,
      }));
    }
    const visibleVol = history
      .map((h, i) => ({ i, ...(volByDate.get(h.date) ?? { turnover: null, vw: null, mainNet: null, retailNet: null }) }))
      .filter((v) => v.i >= Math.max(0, Math.floor(viewStart)) && v.i <= Math.min(H - 1, Math.ceil(viewEndClamped)));
    const isMain = volume?.barMode === 'main';
    const isRetail = volume?.barMode === 'retail';
    const barValue = (v: { turnover: number | null; mainNet: number | null; retailNet: number | null }) =>
      isMain ? v.mainNet : isRetail ? v.retailNet : v.turnover;
    const maxTurn = Math.max(
      ...visibleVol.map((v) => (isMain || isRetail ? Math.abs(barValue(v) ?? 0) : v.turnover ?? 0)),
      1,
    );
    const barW = Math.max(1, (plotW / Math.max(viewEndClamped - viewStart, 1)) * 0.72);
    const volZero = volumeTop + volumeH / 2;
    const volBars = visibleVol.map((v) => {
      if (isMain || isRetail) {
        const val = barValue(v) ?? 0;
        const hgt = Math.max(1, (Math.abs(val) / maxTurn) * (volumeH / 2 - 2));
        return {
          key: history[v.i].date,
          x: x(v.i) - barW / 2,
          y: val >= 0 ? volZero - hgt : volZero,
          h: hgt,
          w: barW,
          color: val >= 0 ? UP : DOWN,
          bgColor: val >= 0 ? 'rgba(220,38,38,0.10)' : 'rgba(5,150,105,0.10)',
          title: `${history[v.i].date} · ${isRetail ? '散户' : '主力'}净额 ${val >= 0 ? '+' : ''}${val.toFixed(2)} 亿（全市场）`,
        };
      }
      const hgt = v.turnover == null ? 0 : Math.max(1, (v.turnover / maxTurn) * (volumeH - 4));
      return {
        key: history[v.i].date,
        x: x(v.i) - barW / 2,
        y: volumeBase - hgt,
        h: hgt,
        w: barW,
        color: v.vw == null ? '#cbd5e1' : v.vw >= 0 ? UP : DOWN,
        bgColor: v.vw == null ? 'rgba(148,163,184,0.08)' : v.vw >= 0 ? 'rgba(220,38,38,0.10)' : 'rgba(5,150,105,0.10)',
        title: `${history[v.i].date} · 成交额 ${v.turnover == null ? '—' : fmtTurnover(v.turnover)} · 成交额加权系数 ${v.vw == null ? '—' : (v.vw > 0 ? '+' : '') + v.vw.toFixed(3)}`,
      };
    });
    const vwPath = visibleVol
      .filter((v) => v.vw != null && Number.isFinite(v.vw))
      .map((v, idx) => {
        const val = Math.max(-1, Math.min(1, v.vw as number));
        const yy = volumeTop + 2 + ((1 - val) / 2) * Math.max(1, volumeH - 4);
        return `${idx === 0 ? 'M' : 'L'}${x(v.i).toFixed(2)},${yy.toFixed(2)}`;
      })
      .join(' ');

    return {
      x, y, stressY, histPath, medianPath, gridVals, actualPath, actualEnd, comparePaths,
      stressPath, mainScorePath, retailScorePath, fanPrices, volBars, maxTurn, vwPath, isMain, isRetail, volZero,
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
  }, [history, fan, last, actual, compare, stress, volume, mainScoreMap, retailScoreMap, viewStart, viewEndClamped, H, F, total, plotW, priceH, volumeH, volumeBase, stressBandTop, stressBandH]);

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
    const mainScoreV = mainScoreMap.get(date) ?? null;
    const retailScoreV = retailScoreMap.get(date) ?? null;
    return {
      x: PAD.left + ((i - viewStart) / Math.max(viewEndClamped - viewStart, 1e-9)) * plotW,
      i,
      date,
      histClose,
      f,
      stress: stressV,
      mainScore: mainScoreV,
      retailScore: retailScoreV,
      turnover: vol?.turnover ?? null,
      vw: vol?.vw ?? null,
      mainNet: vol?.mainNet ?? null,
      retailNet: vol?.retailNet ?? null,
    };
  }, [hoverIdx, allDates, total, H, F, history, fan, volMap, stressMap, mainScoreMap, retailScoreMap, viewStart, viewEndClamped, plotW]);

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
          <linearGradient id="stress-grad" gradientUnits="userSpaceOnUse" x1="0" y1={stressBandTop} x2="0" y2={stressBandTop + stressBandH}>
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
          <path d={geom.histPath} fill="none" stroke={HISTORY} strokeWidth={1.9} strokeLinejoin="round" />
          <path d={geom.upperOuter} fill={UP} fillOpacity={0.07} />
          <path d={geom.lowerOuter} fill={DOWN} fillOpacity={0.07} />
          <path d={geom.upperInner} fill={UP} fillOpacity={0.16} />
          <path d={geom.lowerInner} fill={DOWN} fillOpacity={0.16} />
          {hasStress && (
            <>
              {[
                { from: 0, to: 20, color: 'rgba(220,38,38,0.07)' },
                { from: 20, to: 40, color: 'rgba(245,158,11,0.06)' },
                { from: 40, to: 60, color: 'rgba(148,163,184,0.05)' },
                { from: 60, to: 80, color: 'rgba(20,184,166,0.06)' },
                { from: 80, to: 100, color: 'rgba(16,185,129,0.07)' },
              ].map((z) => (
                <rect
                  key={`zone-${z.from}`}
                  x={PAD.left}
                  y={geom.stressY(z.from)}
                  width={plotW}
                  height={Math.max(1, geom.stressY(z.to) - geom.stressY(z.from))}
                  fill={z.color}
                />
              ))}
              <path d={geom.stressPath} fill="none" stroke="#7c3aed" strokeWidth={1.25} strokeDasharray="4 3" strokeLinejoin="round" opacity={0.75} />
              {geom.mainScorePath && (
                <path d={geom.mainScorePath} fill="none" stroke="#2563eb" strokeWidth={1.15} strokeDasharray="2 3" strokeLinejoin="round" opacity={0.8} />
              )}
              {geom.retailScorePath && (
                <path d={geom.retailScorePath} fill="none" stroke="#f59e0b" strokeWidth={1.15} strokeDasharray="1.5 3" strokeLinejoin="round" opacity={0.85} />
              )}
            </>
          )}
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
        {hasStress && (
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
            {[40, 60].map((v) => (
              <g key={`ref-${v}`}>
                <line x1={PAD.left} x2={W - PAD.right} y1={geom.stressY(v)} y2={geom.stressY(v)}
                      stroke="#7c3aed" strokeWidth={0.9} strokeDasharray="4 4" opacity={0.42} />
                <text x={W - PAD.right - 4} y={geom.stressY(v) - 2} textAnchor="end" fontSize={7} fill="#7c3aed"
                      fontWeight={700} paintOrder="stroke" stroke="#ffffff" strokeWidth={2}>
                  {v === 40 ? '偏热 40' : '偏冷 60'}
                </text>
              </g>
            ))}
          </g>
        )}
        {geom.fanEndVisible && (
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
            {[
              { y: geom.y(geom.lastFan.p90), pct: geom.lastPct.p90, label: '最高', color: UP, size: 9 },
              { y: geom.y(geom.lastFan.p50), pct: geom.lastPct.p50, label: '典型', color: geom.lastPct.p50 >= 0 ? UP : DOWN, size: 11 },
              { y: geom.y(geom.lastFan.p10), pct: geom.lastPct.p10, label: '最低', color: DOWN, size: 9 },
            ].map((it) => (
              <g key={it.label}>
                <line x1={geom.lastX + 5} x2={geom.lastX + 13} y1={it.y} y2={it.y} stroke={it.color} strokeWidth={1} opacity={0.6} />
                <text x={geom.lastX + 16} y={it.y + 3} fontSize={it.size} fill={it.color} fontWeight={800} paintOrder="stroke" stroke="#ffffff" strokeWidth={2.5}>
                  {it.label} {fmtPct(it.pct)}
                </text>
              </g>
            ))}
          </g>
        )}
        {/* 压力分右轴：只标 0/50/100，避免压住价格 */}
        {hasStress && (
          <g fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
            {[0, 50, 100].map((v) => (
              <text key={v} x={W - PAD.right + 4} y={geom.stressY(v) + 3} fontSize={7} fill="#7c3aed" fontWeight={700} opacity={0.75}>
                {v}
              </text>
            ))}
          </g>
        )}

        {/* 成交量副图：与历史日期逐日对齐 */}
        {hasVolume && (
          <g>
            <line x1={PAD.left} x2={W - PAD.right} y1={volumeBase} y2={volumeBase} stroke="#e2e8f0" strokeWidth={1} />
            {geom.volBars.map((b) => (
              <rect key={`bg-${b.key}`} x={b.x} y={volumeTop} width={b.w} height={volumeH} fill={b.bgColor} />
            ))}
            {geom.volBars.map((b) => (
              <rect key={b.key} x={b.x} y={b.y} width={b.w} height={b.h} fill={b.color} opacity={0.9} rx={0.5} stroke="#ffffff" strokeWidth={0.4}>
                <title>{b.title}</title>
              </rect>
            ))}
            {!geom.isMain && !geom.isRetail && <path d={geom.vwPath} fill="none" stroke="#f59e0b" strokeWidth={1.4} strokeLinejoin="round" opacity={0.95} />}
            {(geom.isMain || geom.isRetail) && <line x1={PAD.left} x2={W - PAD.right} y1={geom.volZero} y2={geom.volZero} stroke="#cbd5e1" strokeWidth={0.8} />}
            <text x={PAD.left + 3} y={volumeTop + 10} textAnchor="start" fontSize={8} fill="#64748b" fontWeight={700}
                  paintOrder="stroke" stroke="#ffffff" strokeWidth={2}>{geom.isMain ? '主力净额（全市场）' : geom.isRetail ? '散户净额（全市场）' : volume.scope ? `成交额·${volume.scope}` : '成交额'}</text>
            <text x={PAD.left + 3} y={volumeTop + 21} textAnchor="start" fontSize={7} fill="#94a3b8"
                  paintOrder="stroke" stroke="#ffffff" strokeWidth={2}>{geom.isMain || geom.isRetail ? '红=净流入 / 绿=净流出' : `峰值 ${fmtTurnover(geom.maxTurn)}`}</text>
            <text x={W - PAD.right + 4} y={volumeTop + 10} fontSize={7} fill="#f59e0b" fontWeight={700}>{geom.isMain || geom.isRetail ? '' : '系数'}</text>
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
              const hasScoreLine = hover.stress != null || hover.mainScore != null || hover.retailScore != null;
              const boxH = 46 + (hasScoreLine ? 11 : 0) + (hover.turnover != null ? 11 : 0);
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
                  {hasScoreLine && (
                    <text x={boxX + 6} y={boxY + (hover.f ? 47 : 36)} fontSize={7.5} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
                      <tspan fill="#7c3aed">广度 {hover.stress == null ? '—' : hover.stress.toFixed(0)}</tspan>
                      <tspan fill="#2563eb"> · 主力派发 {hover.mainScore == null ? '—' : hover.mainScore.toFixed(0)}</tspan>
                      <tspan fill="#f59e0b"> · 散户 {hover.retailScore == null ? '—' : hover.retailScore.toFixed(0)}</tspan>
                    </text>
                  )}
                  {(volume?.barMode === 'main' || volume?.barMode === 'retail') && (volume?.barMode === 'main' ? hover.mainNet : hover.retailNet) != null ? (
                    <text x={boxX + 6} y={boxY + (hasScoreLine ? (hover.f ? 58 : 47) : (hover.f ? 47 : 36))} fontSize={8} fill={((volume?.barMode === 'main' ? hover.mainNet : hover.retailNet) ?? 0) < 0 ? '#059669' : '#dc2626'} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
                      {volume?.barMode === 'main' ? '主力净额' : '散户净额'} {((volume?.barMode === 'main' ? hover.mainNet : hover.retailNet) ?? 0) > 0 ? '+' : ''}{((volume?.barMode === 'main' ? hover.mainNet : hover.retailNet) ?? 0).toFixed(2)} 亿
                    </text>
                  ) : hover.turnover != null ? (
                    <text x={boxX + 6} y={boxY + (hasScoreLine ? (hover.f ? 58 : 47) : (hover.f ? 47 : 36))} fontSize={8} fill={hover.vw != null && hover.vw < 0 ? '#059669' : '#dc2626'} fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace">
                      成交额 {fmtTurnover(hover.turnover)}{hover.vw != null ? ` · 系数 ${hover.vw > 0 ? '+' : ''}${hover.vw.toFixed(2)}` : ''}
                    </text>
                  ) : null}
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
        {hasStress && <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-0.5 bg-violet-600" />广度压力</span>}
        {mainPressure.length > 0 && <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-0.5 bg-blue-600" />主力派发（描述，非概率）</span>}
        {retailScore.length > 0 && <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-0.5 bg-amber-500" />散户追涨（10日均，反向）</span>}
        {hasVolume && <span className="flex items-center gap-1.5"><span className="inline-block w-2 h-2 rounded-sm bg-red-500/70" />成交额（偏涨）<span className="inline-block w-2 h-2 rounded-sm bg-emerald-500/70 ml-1" />偏跌<span className="inline-block w-3 h-0.5 bg-amber-500 ml-1" />加权系数</span>}
        {actual.length > 0 && <span className="flex items-center gap-1.5"><span className="inline-block w-3 h-0.5 bg-slate-900" />之后实际</span>}
        {compare.map((c) => <span key={c.name} className="flex items-center gap-1.5"><span className="inline-block w-3 h-0.5" style={{ background: c.color }} />{c.name}</span>)}
        <span className="ml-auto">{upWider ? '向上空间更宽' : '向下空间更宽'}</span>
      </div>
    </div>
  );
};
