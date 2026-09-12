/** 美股市场分析 · API 服务层（/api/v1/market-analysis-us） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type {
  UsAnalystRatings,
  UsAnalystTargets,
  UsAnalystUpgrades,
  UsBreadthData,
  UsBreadthHighlights,
  UsBreadthHistory,
  UsDividendCalendar,
  UsDividendHistoryRow,
  UsEarningsCalendar,
  UsEarningsRevisions,
  UsEarningsSurprises,
  UsFeedStatus,
  UsIndexItem,
  UsIndexSpread,
  UsInsiderMovers,
  UsInstitutionalHolders,
  UsProfitLeaders,
  UsRecentSplits,
  UsRefreshResult,
  UsSectorHeatItem,
  UsSectorRotation,
  UsSectorValuationRow,
  UsSizeTiers,
  UsValuationOverview,
  UsValuationRankings,
} from '../types';

const US_API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis-us`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${US_API}${path}`, { headers: authHeaders() });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`美股市场分析接口失败 ${res.status}: ${detail.slice(0, 120)}`);
  }
  return res.json() as Promise<T>;
}

// ---- 诊断 ----

export function getStatus(): Promise<UsFeedStatus> {
  return getJson<UsFeedStatus>('/status');
}

// ---- Tab1 大盘脉搏 ----

export function getIndicesOverview(): Promise<UsIndexItem[]> {
  return getJson<UsIndexItem[]>('/indices/overview');
}

export function getIndexSpread(): Promise<UsIndexSpread> {
  return getJson<UsIndexSpread>('/indices/spread');
}

export function getBreadth(): Promise<UsBreadthData> {
  return getJson<UsBreadthData>('/breadth');
}

export function getHeatmap(limit = 40): Promise<UsSectorHeatItem[]> {
  return getJson<UsSectorHeatItem[]>(`/heatmap?limit=${limit}`);
}

export function getProfitLeaders(limit = 10): Promise<UsProfitLeaders> {
  return getJson<UsProfitLeaders>(`/profit-leaders?limit=${limit}`);
}

// ---- Tab2 市场宽度 ----

export function getBreadthHistory(days = 60): Promise<UsBreadthHistory> {
  return getJson<UsBreadthHistory>(`/breadth/history?days=${days}`);
}

export function getBreadthHighlights(limit = 30): Promise<UsBreadthHighlights> {
  return getJson<UsBreadthHighlights>(`/breadth/highlights?limit=${limit}`);
}

// ---- Tab3 板块轮动 ----

export function getSectorRotation(limit = 24): Promise<UsSectorRotation> {
  return getJson<UsSectorRotation>(`/sector-rotation?limit=${limit}`);
}

export function getSectorValuation(limit = 24): Promise<UsSectorValuationRow[]> {
  return getJson<UsSectorValuationRow[]>(`/sector-valuation?limit=${limit}`);
}

// ---- Tab4 财报季 ----

export function getEarningsCalendar(days = 30, limit = 50): Promise<UsEarningsCalendar> {
  return getJson<UsEarningsCalendar>(`/earnings/calendar?days=${days}&limit=${limit}`);
}

export function getEarningsSurprises(limit = 30, lookbackDays = 120): Promise<UsEarningsSurprises> {
  return getJson<UsEarningsSurprises>(
    `/earnings/surprises?limit=${limit}&lookback_days=${lookbackDays}`,
  );
}

export function getEarningsRevisions(limit = 30): Promise<UsEarningsRevisions> {
  return getJson<UsEarningsRevisions>(`/earnings/revisions?limit=${limit}`);
}

// ---- Tab5 分析师 ----

export function getAnalystUpgrades(days = 30, limit = 40): Promise<UsAnalystUpgrades> {
  return getJson<UsAnalystUpgrades>(`/analysts/upgrades?days=${days}&limit=${limit}`);
}

export function getAnalystTargets(limit = 30): Promise<UsAnalystTargets> {
  return getJson<UsAnalystTargets>(`/analysts/targets?limit=${limit}`);
}

export function getAnalystRatings(): Promise<UsAnalystRatings> {
  return getJson<UsAnalystRatings>('/analysts/ratings');
}

// ---- Tab6 资金与筹码 ----

export function getInsiderMovers(days = 90, limit = 20): Promise<UsInsiderMovers> {
  return getJson<UsInsiderMovers>(`/insiders/movers?days=${days}&limit=${limit}`);
}

export function getInstitutionalHolders(limit = 30): Promise<UsInstitutionalHolders> {
  return getJson<UsInstitutionalHolders>(`/holdings/institutional?limit=${limit}`);
}

export function getDividendCalendar(days = 60, limit = 40): Promise<UsDividendCalendar> {
  return getJson<UsDividendCalendar>(`/corporate-actions/dividends?days=${days}&limit=${limit}`);
}

export function getRecentSplits(days = 365, limit = 30): Promise<UsRecentSplits> {
  return getJson<UsRecentSplits>(`/corporate-actions/splits?days=${days}&limit=${limit}`);
}

export function getDividendHistory(limit = 30): Promise<UsDividendHistoryRow[]> {
  return getJson<UsDividendHistoryRow[]>(
    `/corporate-actions/dividend-history?limit=${limit}`,
  );
}

// ---- Tab7 估值 ----

export function getValuationRankings(
  kind: 'dividend' | 'pe' | 'pb',
  limit = 20,
): Promise<UsValuationRankings> {
  return getJson<UsValuationRankings>(`/valuation/rankings?kind=${kind}&limit=${limit}`);
}

export function getSizeTiers(): Promise<UsSizeTiers> {
  return getJson<UsSizeTiers>('/valuation/size-tiers');
}

export function getValuationOverview(): Promise<UsValuationOverview> {
  return getJson<UsValuationOverview>('/valuation/overview');
}

// ---- 刷新 ----

export function refreshMarket(): Promise<UsRefreshResult> {
  return fetch(`${US_API}/refresh`, { method: 'POST', headers: authHeaders() }).then((res) =>
    res.json(),
  );
}
