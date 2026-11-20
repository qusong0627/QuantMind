/**
 * 管理后台 ↔ 应用市场桥接（单向跟随：后台 → 应用）。
 *
 * 管理后台切市场会同时写 `qm:current_market` 并派发 `qm:admin-market-change`；
 * 本 hook 监听后 dispatch redux setMarket，让模型训练/推理等页即时同步。
 * 反向（应用 → 后台）由 MarketSelector / DashboardPage 点击时直接广播，无需回路。
 */
import { useEffect } from 'react';
import { useAppDispatch } from '../store';
import {
  setMarket,
  type AppMarket,
} from '../store/slices/uiSlice';
import {
  SHARED_MARKET_EVENT,
  SHARED_MARKET_APP_KEY,
} from '../features/admin/hooks/useSharedAdminMarket';
import { isMarketEnabled } from '../config/marketFlags';

const VALID: AppMarket[] = ['CN', 'US', 'HK', 'CRYPTO', 'FUTURES', 'CUSTOM'];

export function useAdminMarketBridge() {
  const dispatch = useAppDispatch();

  useEffect(() => {
    const apply = (raw: unknown) => {
      if (typeof raw !== 'string' || !(VALID as string[]).includes(raw)) return;
      if (!isMarketEnabled(raw)) return;
      dispatch(setMarket(raw as AppMarket));
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key !== SHARED_MARKET_APP_KEY) return;
      apply(event.newValue);
    };
    const onCustom = (event: Event) => {
      apply((event as CustomEvent<AppMarket>).detail);
    };
    window.addEventListener('storage', onStorage);
    window.addEventListener(SHARED_MARKET_EVENT, onCustom as EventListener);
    return () => {
      window.removeEventListener('storage', onStorage);
      window.removeEventListener(SHARED_MARKET_EVENT, onCustom as EventListener);
    };
  }, [dispatch]);
}
