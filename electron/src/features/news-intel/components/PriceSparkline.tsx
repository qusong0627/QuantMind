/** 价格 sparkline：近 N 日收盘线 + 最新涨跌，红涨绿跌。 */

import { useEffect, useMemo, useState } from 'react';
import { inferenceCenterService, type KlineItem } from '../../../services/inferenceCenterService';
import type { MarketKey } from '../services/newsIntelService';
import { toMarketSymbol } from '../marketSymbol';

interface PriceSparklineProps {
  market: MarketKey;
  code?: string | null;
  days?: number;
  height?: number;
  showText?: boolean;
}

const API_MARKET: Record<MarketKey, string> = {
  CN: 'A',
  HK: 'HK',
  US: 'US',
  FUTURES: 'FUTURES',
};

export function PriceSparkline({
  market,
  code,
  days = 60,
  height = 42,
  showText = true,
}: PriceSparklineProps) {
  const [items, setItems] = useState<KlineItem[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!code) {
      setItems([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    inferenceCenterService
      .getMarketKline(API_MARKET[market], toMarketSymbol(market, code), days)
      .then((rows) => {
        if (!cancelled) setItems((rows || []).slice(-days));
      })
      .catch(() => {
        if (!cancelled) setItems([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [market, code, days]);

  const view = useMemo(() => {
    if (items.length < 2) return null;
    const closes = items.map((x) => Number(x.close || 0));
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const span = max - min || 1;
    const W = 160;
    const H = height;
    const pts = closes
      .map((v, i) => `${i === 0 ? 'M' : 'L'} ${(i / (closes.length - 1)) * W} ${H - ((v - min) / span) * H}`)
      .join(' ');
    const last = closes[closes.length - 1];
    const prev = closes[closes.length - 2];
    return { pts, last, change: prev ? last / prev - 1 : 0, up: last >= prev };
  }, [items, height]);

  if (loading || !view) {
    return <div className="ni-spark ni-spark-empty" style={{ height }} />;
  }

  return (
    <div className="ni-spark">
      <svg viewBox={`0 0 160 ${height}`} preserveAspectRatio="none" className="ni-spark-svg">
        <path d={view.pts} className={view.up ? 'is-up' : 'is-down'} />
      </svg>
      {showText && (
        <div className={`ni-spark-text ${view.up ? 'is-up' : 'is-down'}`}>
          {view.last.toFixed(2)}
          <em>{view.change >= 0 ? '+' : ''}{(view.change * 100).toFixed(2)}%</em>
        </div>
      )}
    </div>
  );
}
