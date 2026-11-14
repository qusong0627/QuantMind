/** 市场阶段（长期/中期/短期/当日）数据服务。 */

import { SERVICE_ENDPOINTS } from '../../../config/services';

export type RegimeColor = 'green' | 'yellow' | 'orange' | 'red' | 'neutral';

export interface RegimeMetric {
  label: string;
  value: string | number | null;
  unit: string;
}

export interface RegimeLayer {
  key: string;
  label: string;
  stage: string;
  score: number;
  color: RegimeColor;
  metrics: RegimeMetric[];
  action: string;
}

export interface RegimeCharts {
  dates: string[];
  close: (number | null)[];
  ma250: (number | null)[];
  above60: (number | null)[];
  adl20: (number | null)[];
  up_ratio: (number | null)[];
  pb_cheap: (number | null)[];
  breadth_5d: (number | null)[];
}

export interface RegimeQuadrant {
  x: number[];
  y: number[];
  ret: (number | null)[];
  current: { x: number; y: number };
}

export interface RegimeIntraday {
  available: boolean;
  source?: string;
  times: string[];
  breadth: (number | null)[];
  mcl_chg5: (number | null)[];
  note: string;
}

export interface RegimeBlock {
  key: string;
  label: string;
  score: number;
  value: number | string;
  unit: string;
  read: string;
}

export interface RegimeSectorItem {
  name: string;
  n: number;
  ret: number;
  up_share: number;
  median: number;
  amount_yi: number;
}

export interface RegimeSector {
  up_share: number;
  dispersion: number;
  list: RegimeSectorItem[];
}

export interface RegimeSummary {
  score: number;
  state: string;
  one_liner: string;
}

export interface SectorStock {
  symbol: string;
  name: string;
  ret: number;
  amount_yi: number;
  rs_1010: number | null;
}

export interface BlockHistoryPoint {
  date: string;
  scores: number[];
}

export interface SectorStocksResponse {
  date: string;
  sector: string;
  universe: string;
  universe_label: string;
  stocks: SectorStock[];
}

export interface CompareFwd {
  mean: number | null;
  win: number | null;
  n: number;
}

export interface CompareItem {
  code: string;
  label: string;
  date: string | null;
  chg: number | null;
  score: number;
  pressure: number;
  pctile: number | null;
  state: string;
  fwd: Record<'5' | '10' | '20', CompareFwd>;
  n_days: number;
}

export interface CompareResponse {
  date: string | null;
  items: CompareItem[];
}

export interface RegimeUniverse {
  code: string;
  label: string;
}

export interface RegimeEventFlags {
  trin_vol: number | null;
  trin_surrender: boolean;
  amount_gap_pp: number | null;
  amount_gap_risk: boolean;
  adl_divergence: boolean;
  breadth_1010: number | null;
  mcl_chg5: number | null;
}

export interface StockBreadthQuintile {
  q: string;
  n: number;
  ret_1010: number;
  ret_after: number;
  up_share_after: number;
}

export interface RegimeStockBreadth {
  available: boolean;
  date?: string;
  n?: number;
  sample?: 'full' | 'hot_set';
  quintiles?: StockBreadthQuintile[];
  q1_minus_q5_after?: number | null;
  note?: string;
}

export interface RegimeResponse {
  date: string;
  layers: RegimeLayer[];
  exposure: { ceiling: number; actual: number; label: string };
  top_risk: { flag: boolean; text: string };
  bottom: { flag: boolean; text: string };
  events: RegimeEventFlags;
  stock_breadth: RegimeStockBreadth;
  charts: RegimeCharts;
  quadrant: RegimeQuadrant;
  intraday: RegimeIntraday;
  sector: RegimeSector;
  blocks: RegimeBlock[];
  block_history: BlockHistoryPoint[];
  summary: RegimeSummary;
  universe: string;
  universe_label: string;
  universes: RegimeUniverse[];
  valuation_scope: string;
  universe_note: string;
  dates: string[];
}

const API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis/regime`;

export async function fetchMarketRegime(
  date?: string,
  universe?: string,
): Promise<RegimeResponse> {
  const token = localStorage.getItem('access_token') || '';
  const qs = new URLSearchParams();
  if (date) qs.set('date', date);
  if (universe) qs.set('universe', universe);
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  const res = await fetch(`${API}${suffix}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore non-JSON error body
    }
    throw new Error(detail);
  }
  return (await res.json()) as RegimeResponse;
}

export async function fetchSectorStocks(
  sector: string,
  date?: string,
  universe?: string,
): Promise<SectorStocksResponse> {
  const token = localStorage.getItem('access_token') || '';
  const qs = new URLSearchParams({ sector });
  if (date) qs.set('date', date);
  if (universe) qs.set('universe', universe);
  const res = await fetch(`${API}/sector-stocks?${qs.toString()}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore non-JSON error body
    }
    throw new Error(detail);
  }
  return (await res.json()) as SectorStocksResponse;
}

export async function fetchRegimeCompare(
  date?: string,
  universes?: string[],
): Promise<CompareResponse> {
  const token = localStorage.getItem('access_token') || '';
  const qs = new URLSearchParams();
  if (date) qs.set('date', date);
  if (universes?.length) qs.set('universes', universes.join(','));
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  const res = await fetch(`${API}/compare${suffix}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore non-JSON error body
    }
    throw new Error(detail);
  }
  return (await res.json()) as CompareResponse;
}
