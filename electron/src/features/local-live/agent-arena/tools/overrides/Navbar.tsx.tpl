import { MarketId } from '../api/client';
import './Navbar.css';

/**
 * arena 原 Navbar 的本地替身（由 tools/port-from-arena.mjs 覆盖写入，勿手改）。
 *
 * 原文件是**整站导航条**：`<Link to="/live">`、`<NavLink to="/control">` 等指向 arena
 * 自己的路由 —— 移植进 QuantMind 后导航由交易台侧栏承担，那一整块不要了。
 * 但 Live.tsx 还在 import 它的 `MarketSwitcher`（cn/hk/us 三个 pill，带自己的
 * 市场参数），所以这里只保留那一个导出，样式仍用原 Navbar.css。
 */

const MARKET_LABELS: Record<MarketId, string> = {
  us: '🇺🇸 美股',
  cn: '🇨🇳 A股',
  hk: '🇭🇰 港股',
};

export function MarketSwitcher({
  market,
  onChange,
}: {
  market: MarketId;
  onChange: (m: MarketId) => void;
}) {
  return (
    <div className="market-switcher" style={{ display: 'flex', gap: 0 }}>
      {(['cn', 'hk', 'us'] as MarketId[]).map((m) => (
        <button
          key={m}
          className={`chip ${m} ${market === m ? 'active' : ''}`}
          style={{ borderRadius: 0 }}
          onClick={() => onChange(m)}
        >
          {MARKET_LABELS[m]}
        </button>
      ))}
    </div>
  );
}
