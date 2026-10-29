/**
 * minibt 单标的策略的「买入持有」基准。
 *
 * 背景：minibt 的 result.equity 不带 benchmark 列，后端也不产出指数序列，
 * 结果页的「超额」一直是 —。对单标的择时策略，最直接、且现有 API 就能算的
 * 对照是「同区间买入持有」：策略累计收益 − 标的买入持有收益。
 *
 * 数据走与 K 线页相同的 `/api/v1/market/kline`（前复权），按日期前向填充；
 * 只对 minibt 结果启用（截面型 STRATEGY_CONFIG 不适合单标的持有基准）。
 */
import { useEffect, useState } from 'react';
import axios from 'axios';
import type { StrategyLabEquityPoint } from '../../../features/strategy-lab/types';
import { authService } from '../../../features/auth/services/authService';
import { SERVICE_ENDPOINTS } from '../../../config/services';

export interface BuyHoldPoint {
  date: string;
  close: number;
}

interface KlineParams {
  symbol: string;
  market: 'A' | 'HK' | 'US';
}

const viteEnv = (import.meta as unknown as { env?: Record<string, string | undefined> }).env;
const GATEWAY =
  String(viteEnv?.VITE_USER_API_URL || '').replace(/\/+$/, '') || SERVICE_ENDPOINTS.USER_SERVICE;

/** 平台前缀式/裸码 → K 线 API 的后缀式；与 StrategyLabKlineView 的口径一致。 */
export function toKlineParams(rawSymbol: string, market: string): KlineParams | null {
  const symbol = String(rawSymbol || '').trim();
  if (!symbol) return null;
  const lower = symbol.toLowerCase();

  if (market === 'HK' || lower.endsWith('.hk') || /^\d{4,5}$/.test(symbol)) {
    return {
      symbol: lower.endsWith('.hk') ? symbol.toUpperCase() : `${symbol}.HK`,
      market: 'HK',
    };
  }
  if (market === 'US') return { symbol: symbol.toUpperCase(), market: 'US' };

  if (/^\d{6}$/.test(symbol)) {
    const suffix = /^[69]/.test(symbol) ? 'SH' : /^[48]/.test(symbol) ? 'BJ' : 'SZ';
    return { symbol: `${symbol}.${suffix}`, market: 'A' };
  }
  if (/^(SH|SZ|BJ)\d{6}$/i.test(symbol)) {
    return { symbol: `${symbol.slice(2)}.${symbol.slice(0, 2).toUpperCase()}`, market: 'A' };
  }
  if (/^\d{6}\.(SH|SZ|BJ)$/i.test(symbol)) {
    return { symbol: symbol.toUpperCase(), market: 'A' };
  }
  return { symbol: symbol.toUpperCase(), market: 'US' };
}

const cache = new Map<string, Promise<BuyHoldPoint[]>>();

async function fetchBuyHold(symbol: string, market: string, start: string, end: string): Promise<BuyHoldPoint[]> {
  const params = toKlineParams(symbol, market);
  if (!params) throw new Error('标的代码为空');
  const token = authService.getAccessToken();
  const response = await axios.get(`${GATEWAY}/market/kline`, {
    params: {
      symbol: params.symbol,
      market: params.market,
      period: 'daily',
      start,
      end,
      days: 750,
    },
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    timeout: 30_000,
  });
  const items = response?.data?.data?.items;
  if (!Array.isArray(items)) return [];
  return items
    .map((item: Record<string, unknown>) => ({
      date: String(item.date || '').slice(0, 10),
      close: Number(item.close),
    }))
    .filter((point: BuyHoldPoint) => point.date.length === 10 && Number.isFinite(point.close) && point.close > 0);
}

function fetchCached(symbol: string, market: string, start: string, end: string): Promise<BuyHoldPoint[]> {
  const key = `${market}:${symbol}:${start}:${end}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const promise = fetchBuyHold(symbol, market, start, end).catch((error) => {
    cache.delete(key);
    throw error;
  });
  cache.set(key, promise);
  return promise;
}

export interface BuyHoldState {
  data: BuyHoldPoint[] | null;
  loading: boolean;
  error: string | null;
}

export function useBuyHoldBenchmark(input: {
  enabled: boolean;
  symbol: string;
  market: string;
  equity: StrategyLabEquityPoint[];
}): BuyHoldState {
  const { enabled, symbol, market, equity } = input;
  const start = equity[0]?.date?.slice(0, 10) || '';
  const end = equity[equity.length - 1]?.date?.slice(0, 10) || '';
  const [state, setState] = useState<BuyHoldState>({ data: null, loading: false, error: null });

  useEffect(() => {
    if (!enabled || !symbol || !start || !end || equity.length < 2) {
      setState({ data: null, loading: false, error: null });
      return;
    }
    let cancelled = false;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    fetchCached(symbol, market, start, end)
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setState({ data: null, loading: false, error: message });
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, symbol, market, start, end, equity.length]);

  return state;
}
