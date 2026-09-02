/** 港股市场分析 · API 服务层（/api/v1/market-analysis-hk） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type {
  HkAhPairItem,
  HkAhPremium,
  HkBreadthData,
  HkCcassHolding,
  HkCcassMovers,
  HkCcassRankings,
  HkDividendCalendar,
  HkFeedStatus,
  HkIndexItem,
  HkRotationItem,
  HkProfitLeaders,
  HkSectorHeatItem,
  HkSectorValuationItem,
  HkSouthFlow,
  HkSouthOverview,
  HkSouthSectorItem,
  HkValuationRanking,
} from '../types';

const HK_API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis-hk`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${HK_API}${path}`, { headers: authHeaders() });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`港股市场分析接口失败 ${res.status}: ${detail.slice(0, 120)}`);
  }
  return res.json() as Promise<T>;
}

export function getStatus(): Promise<HkFeedStatus> {
  return getJson<HkFeedStatus>('/status');
}

export function getIndicesOverview(): Promise<HkIndexItem[]> {
  return getJson<HkIndexItem[]>('/indices/overview');
}

export function getBreadth(): Promise<HkBreadthData> {
  return getJson<HkBreadthData>('/breadth');
}

export function getHeatmap(limit = 40): Promise<HkSectorHeatItem[]> {
  return getJson<HkSectorHeatItem[]>(`/heatmap?limit=${limit}`);
}

export function getSouthOverview(): Promise<HkSouthOverview> {
  return getJson<HkSouthOverview>('/south/overview');
}

export function getSouthFlow(period = 5, limit = 20): Promise<HkSouthFlow> {
  return getJson<HkSouthFlow>(`/south/flow?period=${period}&limit=${limit}`);
}

export function getSouthSectors(limit = 20): Promise<HkSouthSectorItem[]> {
  return getJson<HkSouthSectorItem[]>(`/south/sectors?limit=${limit}`);
}

export function getCcassRankings(limit = 30): Promise<HkCcassRankings> {
  return getJson<HkCcassRankings>(`/ccass/rankings?limit=${limit}`);
}

export function getCcassHolding(symbol: string, limit = 30): Promise<HkCcassHolding> {
  return getJson<HkCcassHolding>(
    `/ccass/holding?symbol=${encodeURIComponent(symbol)}&limit=${limit}`,
  );
}

export function getCcassMovers(limit = 20): Promise<HkCcassMovers> {
  return getJson<HkCcassMovers>(`/ccass/movers?limit=${limit}`);
}

export function getAhPairs(limit = 50): Promise<{ trade_date: string; items: HkAhPairItem[] }> {
  return getJson<{ trade_date: string; items: HkAhPairItem[] }>(`/ah/pairs?limit=${limit}`);
}

export function getValuationRankings(
  kind: 'dividend' | 'pe' | 'pb' | 'ps' | 'pcf',
  limit = 20,
): Promise<HkValuationRanking> {
  return getJson<HkValuationRanking>(`/valuation/rankings?kind=${kind}&limit=${limit}`);
}

export function getAhPremium(limit = 20): Promise<HkAhPremium> {
  return getJson<HkAhPremium>(`/ah-premium?limit=${limit}`);
}

export function getDividendCalendar(days = 60, limit = 40): Promise<HkDividendCalendar> {
  return getJson<HkDividendCalendar>(`/dividend-calendar?days=${days}&limit=${limit}`);
}

export function getSectorRotation(limit = 24): Promise<{ trade_date: string; items: HkRotationItem[] }> {
  return getJson<{ trade_date: string; items: HkRotationItem[] }>(
    `/sector-rotation?limit=${limit}`,
  );
}

export function getSectorValuation(limit = 24): Promise<HkSectorValuationItem[]> {
  return getJson<HkSectorValuationItem[]>(`/sector-valuation?limit=${limit}`);
}

export function getProfitLeaders(limit = 10): Promise<HkProfitLeaders> {
  return getJson<HkProfitLeaders>(`/profit-leaders?limit=${limit}`);
}

export function refreshMarket(): Promise<{ status: string; trade_date: string; message: string }> {
  return fetch(`${HK_API}/refresh`, { method: 'POST', headers: authHeaders() }).then((res) =>
    res.json(),
  );
}