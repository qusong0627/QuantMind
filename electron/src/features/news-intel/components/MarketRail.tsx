/** 市场轨：A股 / 港股 / 美股 / 期货。 */

import type { MarketKey } from '../services/newsIntelService';

interface MarketRailProps {
  value: MarketKey;
  onChange: (market: MarketKey) => void;
}

export const MARKETS: Array<{ key: MarketKey; label: string; sub: string }> = [
  { key: 'CN', label: 'A股', sub: '沪深京' },
  { key: 'HK', label: '港股', sub: 'HKEX' },
  { key: 'US', label: '美股', sub: 'US' },
  { key: 'FUTURES', label: '期货', sub: '商品' },
];

export function MarketRail({ value, onChange }: MarketRailProps) {
  return (
    <div className="ni-market-rail" role="tablist" aria-label="市场">
      {MARKETS.map((m) => (
        <button
          type="button"
          key={m.key}
          role="tab"
          aria-selected={value === m.key}
          className={`ni-market-btn ni-market-${m.key.toLowerCase()} ${value === m.key ? 'active' : ''}`}
          onClick={() => onChange(m.key)}
        >
          <span>{m.label}</span>
          <em>{m.sub}</em>
        </button>
      ))}
    </div>
  );
}
