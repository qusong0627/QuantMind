/** 管理后台全局市场切换（数据管理 / 数据发布 / 特征字典 / 模型管理互相同步）。
 *
 * 各页此前均为本地 state 且口径不一（a_share/CN、quantfutures/FUTURES……），此处统一为
 * 数据发布口径 canonical code：CN / HK / US / CRYPTO / FUTURES / CUSTOM，
 * 各页按自身词汇做映射跟随。localStorage 持久化 + 自定义事件同文档同步 +
 * storage 事件跨浏览器页签同步。
 */
import { useCallback, useEffect, useState } from 'react';

export type SharedAdminMarket = 'CN' | 'HK' | 'US' | 'CRYPTO' | 'FUTURES' | 'CUSTOM';

export const SHARED_MARKET_STORAGE_KEY = 'qm:admin_market';
export const SHARED_MARKET_APP_KEY = 'qm:current_market';
export const SHARED_MARKET_EVENT = 'qm:admin-market-change';

export const SHARED_ADMIN_MARKETS: SharedAdminMarket[] = [
  'CN', 'HK', 'US', 'CRYPTO', 'FUTURES', 'CUSTOM',
];

const DEFAULT_MARKET: SharedAdminMarket = 'CN';

function isSharedMarket(value: unknown): value is SharedAdminMarket {
  return typeof value === 'string' && (SHARED_ADMIN_MARKETS as string[]).includes(value);
}

export function readSharedAdminMarket(): SharedAdminMarket {
  try {
    const raw = window.localStorage.getItem(SHARED_MARKET_STORAGE_KEY);
    if (isSharedMarket(raw)) return raw;
  } catch {
    /* ignore */
  }
  return DEFAULT_MARKET;
}

/** 广播全局市场：写管理后台键 + 应用键，并派发同文档事件（供两边监听）。 */
export function notifySharedAdminMarket(next: SharedAdminMarket): void {
  if (!isSharedMarket(next)) return;
  try {
    window.localStorage.setItem(SHARED_MARKET_STORAGE_KEY, next);
    window.localStorage.setItem(SHARED_MARKET_APP_KEY, next);
  } catch {
    /* ignore */
  }
  window.dispatchEvent(new CustomEvent<SharedAdminMarket>(SHARED_MARKET_EVENT, { detail: next }));
}

export function useSharedAdminMarket(): [SharedAdminMarket, (next: SharedAdminMarket) => void] {
  const [market, setMarketState] = useState<SharedAdminMarket>(readSharedAdminMarket);

  const setMarket = useCallback((next: SharedAdminMarket) => {
    if (!isSharedMarket(next)) return;
    setMarketState((prev) => {
      if (prev === next) return prev;
      notifySharedAdminMarket(next);
      return next;
    });
  }, []);

  useEffect(() => {
    const sync = () => {
      const next = readSharedAdminMarket();
      setMarketState((prev) => (prev === next ? prev : next));
    };
    const onCustom = (event: Event) => {
      const next = (event as CustomEvent<SharedAdminMarket>).detail;
      if (isSharedMarket(next)) {
        setMarketState((prev) => (prev === next ? prev : next));
      }
    };
    window.addEventListener('storage', sync);
    window.addEventListener(SHARED_MARKET_EVENT, onCustom as EventListener);
    sync();
    return () => {
      window.removeEventListener('storage', sync);
      window.removeEventListener(SHARED_MARKET_EVENT, onCustom as EventListener);
    };
  }, []);

  return [market, setMarket];
}
