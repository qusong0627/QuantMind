/**
 * 大盘情绪的指数 K 线（2026-10-28）。
 *
 * 口径差异必须显式可见：
 * - 官方指数（上证/深证成指/创业板指/沪深300/中证1000）：真 OHLC → 蜡烛图；
 * - 全A 指数：本地没有官方全A行情，用**月度再平衡的等权合成 K 线**。开/高/低
 *   是成分股加权值，不是指数日内极值（各票高点不在同一时刻），所以**只画折线**，
 *   不画蜡烛 —— 画了就是给假精度。
 *
 * 成交量：统一画成分股成交额（亿元）柱，红涨绿跌。成交额可加、口径干净；
 * 换手率（Σ成交额 / Σ昨流通市值）在标题右侧给出，避免再开一条会误导的轴。
 */
import React, { useEffect, useMemo, useRef } from 'react';
import {
  AreaSeries,
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts';
import type { IndexKline, UniverseMeta } from '../services/sentimentService';

const UP = '#ef4444';
const DOWN = '#10b981';
const GRID = '#f1f5f9';

/** 七档顺序与配色（A 股红涨绿跌）。 */
const DIST_ROWS: { key: string; label: string; color: string }[] = [
  { key: 'dist_limit_up', label: '涨停', color: '#dc2626' },
  { key: 'dist_up_big', label: '涨>5%', color: '#f87171' },
  { key: 'dist_up_small', label: '涨0~5%', color: '#fca5a5' },
  { key: 'dist_flat', label: '平', color: '#cbd5e1' },
  { key: 'dist_down_small', label: '跌0~5%', color: '#6ee7b7' },
  { key: 'dist_down_big', label: '跌>5%', color: '#34d399' },
  { key: 'dist_limit_down', label: '跌停', color: '#059669' },
];

interface SentimentKlineChartProps {
  name: string;
  kline: IndexKline;
  universe?: UniverseMeta;
  height?: number;
}

interface Row {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  amount: number | null;
  turnover: number | null;
}

const isNum = (v: number | null | undefined): v is number =>
  typeof v === 'number' && Number.isFinite(v);

const fmtAmount = (v: number | null): string =>
  isNum(v) ? (v >= 10000 ? `${(v / 10000).toFixed(2)} 万亿` : `${v.toFixed(0)} 亿`) : '—';

export const SentimentKlineChart: React.FC<SentimentKlineChartProps> = ({
  name,
  kline,
  universe,
  height = 260,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const priceRef = useRef<ISeriesApi<'Candlestick'> | ISeriesApi<'Area'> | null>(null);
  const amountRef = useRef<ISeriesApi<'Histogram'> | null>(null);

  const rows = useMemo<Row[]>(
    () =>
      kline.dates.map((date, i) => ({
        date,
        open: kline.open[i] ?? null,
        high: kline.high[i] ?? null,
        low: kline.low[i] ?? null,
        close: kline.close[i] ?? null,
        amount: kline.amount[i] ?? null,
        turnover: kline.turnover[i] ?? null,
      })),
    [kline],
  );

  const last = rows.length ? rows[rows.length - 1] : null;
  const prevClose = useMemo(() => {
    for (let i = rows.length - 2; i >= 0; i -= 1) {
      if (isNum(rows[i].close)) return rows[i].close as number;
    }
    return null;
  }, [rows]);

  const dist = useMemo(() => {
    if (!kline.dist) return [];
    return DIST_ROWS.map((r) => {
      const series = kline.dist?.[r.key];
      const value = series && series.length ? series[series.length - 1] : null;
      return { ...r, value: typeof value === 'number' ? value : null };
    });
  }, [kline.dist]);
  const distTotal = dist.reduce((acc, d) => acc + (d.value ?? 0), 0);

  /* ---- 建图（按品种选择蜡烛/折线） ---- */
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: {
        background: { color: '#ffffff' },
        textColor: '#64748b',
        attributionLogo: false,
      },
      grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#e2e8f0' },
      timeScale: { borderColor: '#e2e8f0', timeVisible: false },
    });
    chartRef.current = chart;

    if (kline.kind === 'official') {
      priceRef.current = chart.addSeries(CandlestickSeries, {
        upColor: UP,
        downColor: DOWN,
        borderUpColor: UP,
        borderDownColor: DOWN,
        wickUpColor: UP,
        wickDownColor: DOWN,
      });
    } else {
      // 合成口径：只画收盘折线（高/低不是真实日内极值）
      priceRef.current = chart.addSeries(AreaSeries, {
        lineColor: '#6366f1',
        topColor: 'rgba(99,102,241,0.18)',
        bottomColor: 'rgba(99,102,241,0.01)',
        lineWidth: 2,
      });
    }

    amountRef.current = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    });
    chart.priceScale('').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

    const onResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      chart.remove();
      chartRef.current = null;
      priceRef.current = null;
      amountRef.current = null;
    };
  }, [height, kline.kind]);

  /* ---- 灌数据 ---- */
  useEffect(() => {
    const chart = chartRef.current;
    const price = priceRef.current;
    if (!chart || !price) return;

    if (kline.kind === 'official') {
      const candles = rows
        .filter(
          (r): r is Row & { open: number; high: number; low: number; close: number } =>
            isNum(r.open) && isNum(r.high) && isNum(r.low) && isNum(r.close),
        )
        .map((r) => ({
          time: r.date as Time,
          open: r.open,
          high: r.high,
          low: r.low,
          close: r.close,
        }));
      (price as ISeriesApi<'Candlestick'>).setData(candles);
    } else {
      const line = rows
        .filter((r): r is Row & { close: number } => isNum(r.close))
        .map((r) => ({ time: r.date as Time, value: r.close }));
      (price as ISeriesApi<'Area'>).setData(line);
    }

    const amounts = rows
      .filter((r): r is Row & { amount: number } => isNum(r.amount))
      .map((r) => {
        const up = isNum(r.open) ? (r.close ?? 0) >= r.open : (r.close ?? 0) >= (prevClose ?? 0);
        return {
          time: r.date as Time,
          value: r.amount,
          color: up ? 'rgba(239,68,68,0.45)' : 'rgba(16,185,129,0.45)',
        };
      });
    amountRef.current?.setData(amounts);
    chart.timeScale().fitContent();
  }, [rows, kline.kind, prevClose]);

  if (!rows.length) {
    return (
      <div className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-6 text-center text-[11px] text-slate-400">
        暂无 K 线数据
      </div>
    );
  }

  const synthetic = kline.kind === 'synthetic';
  const modeHint = synthetic
    ? '等权、月度再平衡；开/高/低为成分股加权，不是指数日内极值，故只画折线'
    : universe?.mode === 'rank'
      ? '成分股按逐日流通市值排名近似（用前一交易日市值，无前视）'
      : '成分股按板别取当日全部 A 股（无前视）';

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap text-[11px]">
          <span className="font-extrabold text-slate-700">{name} · K 线</span>
          <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 font-bold">
            {synthetic ? '等权合成 · 折线' : '官方指数 · 蜡烛'}
          </span>
          {last && (
            <span className="font-mono text-slate-500">
              成交额 <b className="text-slate-700">{fmtAmount(last.amount)}</b>
              {isNum(last.turnover) && (
                <>
                  {' · '}换手 <b className="text-slate-700">{last.turnover.toFixed(2)}%</b>
                </>
              )}
            </span>
          )}
        </div>
        <span className="text-[10px] text-slate-400">{modeHint}</span>
      </div>

      <div ref={containerRef} className="w-full" />

      {dist.length > 0 && distTotal > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
            {dist.map((d) =>
              d.value && d.value > 0 ? (
                <div
                  key={d.key}
                  title={`${d.label} ${d.value} 只`}
                  style={{ width: `${(d.value / distTotal) * 100}%`, background: d.color }}
                />
              ) : null,
            )}
          </div>
          <div className="flex items-center gap-3 flex-wrap text-[10px] text-slate-500">
            {dist.map((d) => (
              <span key={d.key} className="inline-flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: d.color }} />
                {d.label}
                <b className="font-mono text-slate-700">{d.value ?? '—'}</b>
              </span>
            ))}
            <span className="ml-auto text-slate-400">
              {last?.date} · 合计 {distTotal} 只
            </span>
          </div>
        </div>
      )}
    </div>
  );
};
