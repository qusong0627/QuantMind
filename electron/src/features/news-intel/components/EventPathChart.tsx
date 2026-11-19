/** 新闻前后 K 线与成交量：T-5..T-1（pre5）+ T0..T+5，蜡烛 + 量柱 + 关键节点表。 */

import { useEffect, useMemo, useState } from 'react';
import dayjs from 'dayjs';
import { Spin } from 'antd';
import { inferenceCenterService, type KlineItem } from '../../../services/inferenceCenterService';
import type { MarketKey } from '../services/newsIntelService';
import { toMarketSymbol } from '../marketSymbol';

interface EventPathChartProps {
  market: MarketKey;
  code?: string | null;
  date?: string | null;
  direction?: string | null;
}

const API_MARKET: Record<MarketKey, string> = {
  CN: 'A',
  HK: 'HK',
  US: 'US',
  FUTURES: 'FUTURES',
};

interface Point {
  label: string;
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  dayRet: number;
  cumRet: number;
  offset: number;
}

const LABEL_OFFSETS = new Set([-5, -1, 0, 1, 5]);

function fmtVolume(v: number): string {
  if (!v) return '—';
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(1)}万`;
  return String(Math.round(v));
}

export function EventPathChart({ market, code, date, direction }: EventPathChartProps) {
  const [items, setItems] = useState<KlineItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!code || !date) {
      setItems([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const d = dayjs(date);
    const start = d.subtract(35, 'day').format('YYYY-MM-DD');
    const end = d.add(18, 'day').format('YYYY-MM-DD');
    inferenceCenterService
      .getMarketKline(API_MARKET[market], toMarketSymbol(market, code), 60, end, start)
      .then((rows) => {
        if (!cancelled) setItems(rows || []);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'K 线加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [market, code, date]);

  const points = useMemo<Point[]>(() => {
    if (!items.length || !date) return [];
    const rows = [...items].sort((a, b) => a.date.localeCompare(b.date));
    const day = dayjs(date).format('YYYY-MM-DD');
    let eventIdx = rows.findIndex((r) => r.date >= day);
    if (eventIdx < 0) eventIdx = rows.length - 1;
    const baseIdx = Math.max(0, eventIdx - 6); // T-6 收盘 = pre5 的基准
    const base = Number(rows[baseIdx]?.close || 0);
    if (!base) return [];
    const out: Point[] = [];
    const first = Math.max(0, eventIdx - 5);
    for (let i = first; i < rows.length && i <= eventIdx + 5; i += 1) {
      const prev = i > 0 ? Number(rows[i - 1].close || 0) : 0;
      const close = Number(rows[i].close || 0);
      const open = Number(rows[i].open || close);
      const high = Number(rows[i].high || Math.max(open, close));
      const low = Number(rows[i].low || Math.min(open, close));
      const offset = i - eventIdx;
      out.push({
        label: offset === 0 ? 'T0' : offset > 0 ? `T+${offset}` : `T${offset}`,
        date: rows[i].date,
        open,
        high,
        low,
        close,
        volume: Number(rows[i].volume || 0),
        dayRet: prev ? close / prev - 1 : 0,
        cumRet: close / base - 1,
        offset,
      });
    }
    return out;
  }, [items, date]);

  const summary = useMemo(() => {
    const at = (label: string) => points.find((x) => x.label === label)?.cumRet ?? null;
    const pre = points.filter((p) => p.offset < 0 && p.volume > 0);
    const post = points.filter((p) => p.offset > 0 && p.volume > 0);
    const preAvg = pre.length
      ? pre.reduce((a, p) => a + p.volume, 0) / pre.length
      : 0;
    const t0 = points.find((p) => p.offset === 0);
    const postAvg = post.length
      ? post.reduce((a, p) => a + p.volume, 0) / post.length
      : 0;
    return {
      pre5: at('T-1'),
      t1: at('T+1'),
      t3: at('T+3'),
      t5: at('T+5'),
      preAvg,
      eventVolRatio: preAvg && t0?.volume ? t0.volume / preAvg : null,
      afterVolRatio: preAvg && postAvg ? postAvg / preAvg : null,
    };
  }, [points]);

  const preRead = useMemo(() => {
    const p = summary.pre5;
    if (p == null) return null;
    const pct = `${(p * 100).toFixed(2)}%`;
    if (direction === '利好' && p > 0.05) {
      return `利好 + pre5 ${pct}：研究里后 5 日 -103bp，典型的「买预期卖事实」，不追。`;
    }
    if (direction === '利空' && p > 0.05) {
      return `利空 + pre5 ${pct}：研究里的 P0 回避格（T+1 回避 +186.8bp/事件），不抄底。`;
    }
    if (direction === '利空' && p < -0.02) {
      return `利空 + pre5 ${pct}：已跌透，样本里更偏反弹，不追空。`;
    }
    if (direction === '利好' && p < -0.02) {
      return `利好 + pre5 ${pct}：下跌趋势里的利好，样本里不抄底。`;
    }
    return `新闻前 5 日 ${pct}：未出现大幅抢跑。`;
  }, [summary.pre5, direction]);

  if (!code || !date) return null;
  if (loading) {
    return (
      <div className="ni-path-loading">
        <Spin size="small" />
      </div>
    );
  }
  if (error || !points.length) {
    return <div className="ni-path-empty">{error || '该窗口没有可用日线（停牌/新股/数据缺失）'}</div>;
  }

  const W = 420;
  const priceH = 112;
  const volH = 42;
  const padL = 8;
  const padR = 8;
  const top = 8;
  const gap = 8;
  const priceBottom = top + priceH;
  const volTop = priceBottom + gap;
  const volBottom = volTop + volH;
  const H = volBottom + 18;
  const slot = (W - padL - padR) / Math.max(1, points.length);
  const minLow = Math.min(...points.map((p) => p.low || p.close));
  const maxHigh = Math.max(...points.map((p) => p.high || p.close));
  const priceSpan = maxHigh - minLow || 1;
  const maxVol = Math.max(1, ...points.map((p) => p.volume));
  const yPrice = (v: number) => priceBottom - ((v - minLow) / priceSpan) * (priceH - 8);
  const yVol = (v: number) => volBottom - (v / maxVol) * (volH - 4);
  const t0Index = points.findIndex((p) => p.label === 'T0');
  const t0X = t0Index >= 0 ? padL + slot * (t0Index + 0.5) : padL;

  const fmt = (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(2)}%`);
  const cls = (v: number) => (v > 0 ? 'is-up' : v < 0 ? 'is-down' : 'is-flat');

  return (
    <div className="ni-path">
      <div className="ni-path-head">
        <span className="ni-kicker">新闻前后 K 线与成交量</span>
        <span className="ni-path-legend">蜡烛=OHLC · 下=成交量 · 虚线=T0</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="ni-path-svg" role="img" aria-label="新闻前后 K 线与成交量">
        <rect x={padL} y={top} width={Math.max(0, t0X - padL)} height={volBottom - top} className="ni-path-preband" />
        {[0.25, 0.5, 0.75].map((r) => (
          <line
            key={r}
            x1={padL}
            x2={W - padR}
            y1={top + priceH * r}
            y2={top + priceH * r}
            className="ni-path-grid"
          />
        ))}
        {points.map((p, i) => {
          const x = padL + slot * (i + 0.5);
          const up = p.close >= p.open;
          const bodyW = Math.max(3, slot * 0.5);
          const yO = yPrice(p.open);
          const yC = yPrice(p.close);
          const bodyY = Math.min(yO, yC);
          const bodyH = Math.max(1, Math.abs(yC - yO));
          const vy = yVol(p.volume);
          const label = LABEL_OFFSETS.has(p.offset) ? p.label : '';
          return (
            <g key={p.date} className={p.offset < 0 ? 'is-pre' : ''}>
              {p.label === 'T0' && (
                <line x1={x} x2={x} y1={top} y2={volBottom} className="ni-path-t0" />
              )}
              <line x1={x} x2={x} y1={yPrice(p.high)} y2={yPrice(p.low)} className={`ni-path-wick ${up ? 'is-up' : 'is-down'}`} />
              <rect
                x={x - bodyW / 2}
                y={bodyY}
                width={bodyW}
                height={bodyH}
                className={`ni-path-candle ${up ? 'is-up' : 'is-down'}`}
                rx={0.5}
              />
              <rect
                x={x - bodyW / 2}
                y={vy}
                width={bodyW}
                height={Math.max(1, volBottom - vy)}
                className={`ni-path-vol ${up ? 'is-up' : 'is-down'}`}
                rx={0.5}
              />
              {label && (
                <text x={x} y={H - 4} textAnchor="middle" className="ni-path-label">
                  {label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="ni-path-summary">
        <span>
          pre5 <b className={cls(summary.pre5 ?? 0)}>{fmt(summary.pre5)}</b>
        </span>
        <span>
          T+1 <b className={cls(summary.t1 ?? 0)}>{fmt(summary.t1)}</b>
        </span>
        <span>
          T+3 <b className={cls(summary.t3 ?? 0)}>{fmt(summary.t3)}</b>
        </span>
        <span>
          T+5 <b className={cls(summary.t5 ?? 0)}>{fmt(summary.t5)}</b>
        </span>
      </div>
      <div className="ni-vol-chips">
        <span>
          新闻日量比{' '}
          <b className={(summary.eventVolRatio ?? 0) >= 1.5 ? 'is-high' : ''}>
            {summary.eventVolRatio == null ? '—' : `${summary.eventVolRatio.toFixed(2)}×`}
          </b>
        </span>
        <span>
          后 5 日均量比{' '}
          <b className={(summary.afterVolRatio ?? 0) >= 1.5 ? 'is-high' : ''}>
            {summary.afterVolRatio == null ? '—' : `${summary.afterVolRatio.toFixed(2)}×`}
          </b>
        </span>
      </div>
      <div className="ni-path-table">
        <div className="ni-path-tr is-head">
          <span>节点</span>
          <span>涨跌</span>
          <span>收盘</span>
          <span>成交量</span>
          <span>量比</span>
        </div>
        {points
          .filter((p) => LABEL_OFFSETS.has(p.offset))
          .map((p) => (
            <div className={`ni-path-tr ${p.offset === 0 ? 'is-t0' : ''}`} key={p.date}>
              <span>{p.label}</span>
              <span className={cls(p.dayRet)}>{fmt(p.dayRet)}</span>
              <span>{p.close.toFixed(2)}</span>
              <span>{fmtVolume(p.volume)}</span>
              <span>
                {summary.preAvg && p.volume
                  ? `${(p.volume / summary.preAvg).toFixed(2)}×`
                  : '—'}
              </span>
            </div>
          ))}
      </div>
      {preRead && <div className="ni-path-read">{preRead}</div>}
      <div className="ni-path-note">
        量比 = 当日成交量 / 新闻前 5 日均量。证据展示，不是回测：未扣费、未排涨跌停/停牌；
        A 股口径不自动外推到港股/美股。
      </div>
    </div>
  );
}
