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
  HkHeatmapDrill,
  HkIndexItem,
  HkIndexModelResponse,
  HkOpportunitySectors,
  HkOpportunityStocks,
  HkOpportunitySummary,
  HkRiskWatch,
  HkRotationItem,
  HkProfitLeaders,
  HkSectorHeatItem,
  HkSectorValuationItem,
  HkSouthFlow,
  HkSouthOverview,
  HkSouthSectorItem,
  HkValuationRanking,
  InstitutionalMovers,
  InstitutionalOverview,
  InstitutionalParticipants,
  InstitutionalStockDetail,
  InstitutionalSuggestItem,
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

/** 宽基指数模型排序分（缺省取默认/最新可用港股模型） */
export function getIndexModelScores(modelId?: string): Promise<HkIndexModelResponse> {
  const q = modelId ? `?model_id=${encodeURIComponent(modelId)}` : '';
  return getJson<HkIndexModelResponse>(`/indices/model-scores${q}`);
}

export function getBreadth(): Promise<HkBreadthData> {
  return getJson<HkBreadthData>('/breadth');
}

export function getHeatmap(limit = 40): Promise<HkSectorHeatItem[]> {
  return getJson<HkSectorHeatItem[]>(`/heatmap?limit=${limit}`);
}

export function getHeatmapDrill(sector: string): Promise<HkHeatmapDrill> {
  return getJson<HkHeatmapDrill>(`/heatmap/drill?sector=${encodeURIComponent(sector)}`);
}

// ---- 机会雷达 ----

export function getOpportunityStocks(
  limit = 30,
  sector?: string,
): Promise<HkOpportunityStocks> {
  const q = sector ? `&sector=${encodeURIComponent(sector)}` : '';
  return getJson<HkOpportunityStocks>(`/opportunity/stocks?limit=${limit}${q}`);
}

export function getOpportunitySectors(
  limit = 12,
  minStocks = 8,
): Promise<HkOpportunitySectors> {
  return getJson<HkOpportunitySectors>(
    `/opportunity/sectors?limit=${limit}&min_stocks=${minStocks}`,
  );
}

export function getRiskWatch(limit = 20): Promise<HkRiskWatch> {
  return getJson<HkRiskWatch>(`/opportunity/risk?limit=${limit}`);
}

export function getOpportunitySummary(): Promise<HkOpportunitySummary> {
  return getJson<HkOpportunitySummary>('/opportunity/summary');
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
// ---- 机构持仓分析 ----

export function getInstitutionalOverview(): Promise<InstitutionalOverview> {
  return getJson<InstitutionalOverview>('/institutional/overview');
}

export function getInstitutionalMovers(
  category: string,
  window: number,
  direction: 'increase' | 'decrease',
  limit = 20,
): Promise<InstitutionalMovers> {
  return getJson<InstitutionalMovers>(
    `/institutional/movers?category=${encodeURIComponent(category)}&window=${window}&direction=${direction}&limit=${limit}`,
  );
}

export function getInstitutionalStock(symbol: string): Promise<InstitutionalStockDetail> {
  return getJson<InstitutionalStockDetail>(
    `/institutional/stock?symbol=${encodeURIComponent(symbol)}`,
  );
}

export function getInstitutionalSuggest(q: string, limit = 10): Promise<InstitutionalSuggestItem[]> {
  return getJson<InstitutionalSuggestItem[]>(
    `/institutional/stocks/suggest?q=${encodeURIComponent(q)}&limit=${limit}`,
  );
}

export function getInstitutionalParticipants(
  category = 'all',
  q = '',
  limit = 50,
): Promise<InstitutionalParticipants> {
  return getJson<InstitutionalParticipants>(
    `/institutional/participants?category=${encodeURIComponent(category)}&q=${encodeURIComponent(q)}&limit=${limit}`,
  );
}

// ---- 聪明钱 · 席位行为统计与类型效力（细案 §6；契约见后端 hk_smart_money.py）----
//
// 契约要点：
// - 404（产物未构建）→ `notBuilt` 空态，不白屏；evidence 生命周期独立；
// - 缺失字段一律 null 原样透传（界面显示「—」，**绝不补 0**）；
// - 「零入柱（evidence_only）」是真裁定（数据结论），不是链路故障。

/** 携带状态码的取数错误；404（未构建）走 notBuilt 空态 */
export class HkSmartMoneyError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'HkSmartMoneyError';
    this.status = status;
  }

  get notBuilt(): boolean {
    return this.status === 404;
  }
}

async function hkSmGet<T>(path: string): Promise<T> {
  const res = await fetch(`${HK_API}${path}`, { headers: authHeaders() });
  if (!res.ok) {
    let detail = '';
    try {
      const body = (await res.json()) as { detail?: unknown };
      detail = typeof body.detail === 'string' ? body.detail : '';
    } catch {
      detail = '';
    }
    throw new HkSmartMoneyError(
      detail ? `港股聪明钱接口失败 ${res.status}: ${detail}` : `港股聪明钱接口失败 ${res.status}`,
      res.status,
    );
  }
  return res.json() as Promise<T>;
}

/** 口径脚注集合（键随面不同：hit/shrink/suspect/episode/eligible/method/fwd/…） */
export type HkSmNotes = Record<string, string | null | undefined>;

export interface HkSmFreshness {
  ccass_date: string | null;
  south_date: string | null;
  kline_date: string | null;
  ccass_lag_days: number | null;
  partial: boolean | null;
}

/** 类型概览行（含该类型最强 h10 变体；rank_ic 为 None 的变体不进 best） */
export interface HkSmCategoryRow {
  category: string;
  label: string | null;
  category_label: string | null;
  holding_yi: number | null;
  holding_date: string | null;
  n_symbols: number | null;
  net5_yi: number | null;
  net5_days: number | null;
  net20_yi: number | null;
  net20_days: number | null;
  /** v2：类别方向技能（bp，买 +exc / 卖 −exc，事件层口径）；南向与 other 为 null */
  skill_bp: number | null;
  vw_skill_bp: number | null;
  n_lead_events: number | null;
  admitted: boolean | null;
  best: {
    h10_ic: number | null;
    h10_tail_ok: boolean | null;
    variant: string | null;
    variant_label: string | null;
    direction: number | null;
    admitted: boolean | null;
  } | null;
}

export interface HkSmCounts30d {
  days: number | null;
  events: number | null;
  transfer_suspect: number | null;
  first_seen: number | null;
  exit_boundary: number | null;
}

export interface HkSmOverviewResponse {
  as_of: string | null;
  generated_at: string | null;
  partial: boolean | null;
  version: string | null;
  evidence_only: boolean | null;
  admitted: string[];
  directions: Record<string, number | null>;
  freshness: HkSmFreshness | null;
  categories: HkSmCategoryRow[];
  counts_30d: HkSmCounts30d | null;
  summary: {
    scores_date: string | null;
    pool_size: number | null;
    covered_mean: number | null;
  } | null;
  trends: {
    window: number | null;
    categories: Record<
      string,
      Array<{ date: string; net_yi: number | null; n_symbols: number | null }>
    >;
  } | null;
  notes: HkSmNotes;
}

/** 单变体单horizon 段（24 信号 × 3 horizon） */
export interface HkSmSignalSeg {
  n_days: number | null;
  rank_ic: number | null;
  icir: number | null;
  ic_pos_rate: number | null;
  q1_bp: number | null;
  q2_bp: number | null;
  q3_bp: number | null;
  q4_bp: number | null;
  q5_bp: number | null;
  q5_minus_q1_bp: number | null;
  buy_hit: number | null;
  sell_hit: number | null;
  asym_bp: number | null;
  n_top_obs: number | null;
  n_bot_obs: number | null;
  tail_ok: boolean | null;
}

export interface HkSmSignal {
  name: string;
  category: string;
  category_label: string | null;
  variant: string;
  variant_label: string | null;
  coverage: {
    avg_names_per_day: number | null;
    days: number | null;
  } | null;
  direction: number | null;
  admitted: boolean | null;
  decision: string | null;
  results: Record<string, HkSmSignalSeg | undefined>;
}

export interface HkSmEvalWindow {
  eval_start: string | null;
  eval_end: string | null;
  warmup_days: number | null;
  event_days: number | null;
  event_rows: number | null;
}

export interface HkSmCalibrationGridRow {
  min_value_hkd: number | null;
  min_pct: number | null;
  mean_daily: number | null;
  median_daily: number | null;
  covered_symbols_mean: number | null;
  value_median: number | null;
  value_p90: number | null;
  suspect_total: number | null;
  in_target: boolean | null;
}

export interface HkSmCalibration {
  sample_days: number | null;
  sample_start: string | null;
  sample_end: string | null;
  target_daily_events: [number, number] | null;
  grid: HkSmCalibrationGridRow[];
  selected: { min_value_hkd: number | null; min_pct: number | null } | null;
  note: string | null;
}

export interface HkSmTypesResponse {
  validated_at: string | null;
  as_of: string | null;
  admit_horizon: number | null;
  horizon: string | null;
  window: HkSmEvalWindow | null;
  admitted: string[];
  calibration: HkSmCalibration | null;
  signals: HkSmSignal[];
  notes: HkSmNotes;
}

export interface HkSmSeatItem {
  participant_id: string;
  participant_name: string | null;
  category: string;
  category_label: string | null;
  kind: string | null;
  /** 窗口（/seats items 依查询窗；/seat/{id} windows 各键对应行同带） */
  window?: string | null;
  n_events: number | null;
  n_buy: number | null;
  n_sell: number | null;
  n_exc10: number | null;
  avg_exc_bp: number | null;
  hit: number | null;
  buy_hit: number | null;
  sell_hit: number | null;
  t: number | null;
  shrunk_exc_bp: number | null;
  shrunk_hit: number | null;
  stability: boolean | null;
  eligible: boolean | null;
  first_event_date: string | null;
  last_event_date: string | null;
  /** v2：方向技能（bp，买 +exc / 卖 −exc） */
  skill_bp: number | null;
  /** v2：技能 t 值（skill / 标准误） */
  skill_t: number | null;
  /** v2：90% 单侧下界（skill − 1.28×SE）；仅 n_exc10 ≥ 5 给出，更小样本「—」沉底 */
  lb_skill_bp: number | null;
  /** v2：|Δvalue| 加权技能（大钱口径） */
  vw_skill_bp: number | null;
  /** v2：fwd_exc_5 / fwd_exc_20 裸超额均值（稳健性横看） */
  exc5_bp: number | null;
  exc20_bp: number | null;
  /** v2：买/卖单侧事件的事后超额均值（原始符号，不翻转） */
  buy_exc_bp: number | null;
  sell_exc_bp: number | null;
  /** v2：该席事件去重标的数（专精 vs 分散） */
  n_symbols: number | null;
  /** v2：单笔分量级 = mean(|Δvalue|) / 1e6（百万 HKD） */
  avg_value_m: number | null;
}

export interface HkSmSeatsResponse {
  as_of: string | null;
  generated_at: string | null;
  window: string | null;
  category: string | null;
  sort: string | null;
  min_events: number | null;
  horizon: number | null;
  total: number | null;
  items: HkSmSeatItem[];
  notes: HkSmNotes;
}

export interface HkSmConsensusItem {
  symbol: string;
  name: string | null;
  score: number | null;
  covered: number | null;
  n_pillars: number | null;
  pct: Record<string, number | null>;
  val: Record<string, number | null>;
}

export interface HkSmConsensusResponse {
  as_of: string | null;
  generated_at: string | null;
  adopted: string[];
  directions: Record<string, number | null>;
  evidence_only: boolean | null;
  total: number | null;
  items: HkSmConsensusItem[];
  notes: HkSmNotes;
}

export interface HkSmTimelineRow {
  date: string;
  participant_id: string;
  participant_name: string | null;
  category: string;
  category_label: string | null;
  kind: string | null;
  direction: number | null;
  delta_qty: number | null;
  delta_value: number | null;
  value_yi: number | null;
  delta_pct: number | null;
  price: number | null;
  pct_of_amount: number | null;
  transfer_suspect: boolean | null;
  first_of_episode: boolean | null;
  fwd_exc_5_bp: number | null;
  fwd_exc_10_bp: number | null;
  fwd_exc_20_bp: number | null;
}

export interface HkSmStockHolding {
  category: string;
  category_label: string | null;
  qty_sum: number | null;
  n_seats: number | null;
  date: string | null;
}

export interface HkSmStockStats {
  days: number | null;
  events: number | null;
  n_buy: number | null;
  n_sell: number | null;
  net_value_yi: number | null;
  avg_exc_10_bp: number | null;
  buy_hit: number | null;
  sell_hit: number | null;
  by_category: Array<{
    category: string;
    category_label: string | null;
    n_buy: number | null;
    n_sell: number | null;
    net_value_yi: number | null;
    avg_exc_10_bp_buy: number | null;
    avg_exc_10_bp_sell: number | null;
  }>;
  notes: HkSmNotes;
}

export interface HkSmStockResponse {
  symbol: string;
  name: string | null;
  as_of: string | null;
  generated_at: string | null;
  window: { days: number | null; start: string | null } | null;
  timeline: HkSmTimelineRow[];
  holdings: HkSmStockHolding[];
  stats: HkSmStockStats | null;
  notes: HkSmNotes;
}

export interface HkSmEvidenceSignal extends HkSmSignal {
  by_year?: unknown;
}

export interface HkSmEvidenceResponse {
  validated_at: string | null;
  version: string | null;
  window: HkSmEvalWindow | null;
  method: Record<string, string>;
  admit_horizon: number | null;
  signals: HkSmEvidenceSignal[];
  correlation: {
    pairs?: unknown[];
    collapsed?: unknown[];
  } | null;
  calibration: HkSmCalibration | null;
  exclusions: {
    days: number | null;
    south_days: number | null;
    events: number | null;
    transfer_suspect: number | null;
    first_seen: number | null;
    exit_boundary: number | null;
    raw_moves: number | null;
    transfer_suspect_share: number | null;
  } | null;
  reconciliation: {
    as_of: string | null;
    a_seat_qty_sum: number | null;
    south_qty_sum: number | null;
    ratio: number | null;
  } | null;
  composite: unknown;
  admitted: string[];
  evidence_only: boolean | null;
  warnings: string[];
}

export function getHkSmOverview(): Promise<HkSmOverviewResponse> {
  return hkSmGet<HkSmOverviewResponse>('/smart-money/overview');
}

export function getHkSmTypes(horizon: '5' | '10' | '20' = '10'): Promise<HkSmTypesResponse> {
  return hkSmGet<HkSmTypesResponse>(`/smart-money/types?horizon=${horizon}`);
}

export interface HkSmSeatsQuery {
  /** 事件窗口：'20' | '60' | 'all'（后端 "0"/缺省 → 全史） */
  window?: string;
  category?: string | null;
  /** 排序：lb_skill（默认，90% 下界）| skill | vw_skill | avg_exc | shrunk_hit | n */
  sort?: string;
  minEvents?: number;
  limit?: number;
}

export function getHkSmSeats(q: HkSmSeatsQuery = {}): Promise<HkSmSeatsResponse> {
  const params = new URLSearchParams();
  params.set('window', q.window ?? '60');
  if (q.category) params.set('category', q.category);
  params.set('sort', q.sort ?? 'lb_skill');
  params.set('min_events', String(Math.max(Math.trunc(q.minEvents ?? 5), 1)));
  params.set('limit', String(Math.min(Math.max(Math.trunc(q.limit ?? 30), 1), 100)));
  return hkSmGet<HkSmSeatsResponse>(`/smart-money/seats?${params.toString()}`);
}

// ---- 席位档案与持续性审计（v2：解答「哪些机构更有优势」的钻取面）----

export interface HkSmSeatMonthlyRow {
  month: string;
  n: number | null;
  skill_bp: number | null;
  vw_skill_bp: number | null;
  hit: number | null;
}

export interface HkSmSeatSymbolRow {
  symbol: string;
  name: string | null;
  n: number | null;
  skill_bp: number | null;
  vw_skill_bp: number | null;
  last_event_date: string | null;
}

export interface HkSmSeatEventRow {
  date: string | null;
  symbol: string;
  name: string | null;
  kind: string | null;
  direction: number | null;
  delta_value: number | null;
  value_yi: number | null;
  delta_pct: number | null;
  price: number | null;
  transfer_suspect: boolean | null;
  first_of_episode: boolean | null;
  fwd_exc_5_bp: number | null;
  fwd_exc_10_bp: number | null;
  fwd_exc_20_bp: number | null;
}

export interface HkSmSeatProfileResponse {
  participant: {
    participant_id: string;
    participant_name: string | null;
    category: string;
    category_label: string | null;
    kind: string | null;
    first_event_date: string | null;
    last_event_date: string | null;
  };
  as_of: string | null;
  generated_at: string | null;
  /** 三窗统计行（键 '20' | '60' | 'all'） */
  windows: Record<string, HkSmSeatItem | undefined>;
  monthly: HkSmSeatMonthlyRow[];
  top_symbols: HkSmSeatSymbolRow[];
  recent_events: HkSmSeatEventRow[];
  notes: HkSmNotes;
}

export interface HkSmPersistenceScatterPoint {
  participant_id: string;
  name: string | null;
  category: string | null;
  n1: number | null;
  n2: number | null;
  skill1_bp: number | null;
  skill2_bp: number | null;
}

export interface HkSmPersistenceQuartile {
  q: number;
  n_seats: number | null;
  n_events: number | null;
  forward_skill_bp: number | null;
  forward_hit: number | null;
}

export interface HkSmPersistenceCategoryHalf {
  category: string;
  n: number | null;
  first_half_vw_skill_bp: number | null;
  second_half_vw_skill_bp: number | null;
}

export interface HkSmPersistenceResponse {
  generated_at: string | null;
  as_of: string | null;
  split_date: string | null;
  scatter: HkSmPersistenceScatterPoint[];
  spearman: { h10: number | null; h20: number | null; hit: number | null };
  quartiles: HkSmPersistenceQuartile[];
  category_halves: HkSmPersistenceCategoryHalf[];
  method: Record<string, string>;
}

/** 席位档案：三窗统计 + 月度战绩 + 活跃标的 + 最近事件（查无 → 404 seat_not_found） */
export function getHkSmSeatProfile(participantId: string): Promise<HkSmSeatProfileResponse> {
  return hkSmGet<HkSmSeatProfileResponse>(
    `/smart-money/seat/${encodeURIComponent(participantId.trim().toUpperCase())}`,
  );
}

/** 持续性审计：散点/秩相关/四分位 OOS/类别前后半（构建期实算，原样透传） */
export function getHkSmPersistence(): Promise<HkSmPersistenceResponse> {
  return hkSmGet<HkSmPersistenceResponse>('/smart-money/persistence');
}

export function getHkSmConsensus(limit = 50): Promise<HkSmConsensusResponse> {
  const clamped = Math.min(Math.max(Math.trunc(limit), 5), 200);
  return hkSmGet<HkSmConsensusResponse>(`/smart-money/consensus?limit=${clamped}`);
}

export function getHkSmStock(symbol: string, days = 60): Promise<HkSmStockResponse> {
  const clamped = Math.min(Math.max(Math.trunc(days), 20), 120);
  return hkSmGet<HkSmStockResponse>(
    `/smart-money/stock/${encodeURIComponent(symbol)}?days=${clamped}`,
  );
}

export function getHkSmEvidence(): Promise<HkSmEvidenceResponse> {
  return hkSmGet<HkSmEvidenceResponse>('/smart-money/evidence');
}
