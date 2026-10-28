/** 大盘情绪 · 广度压力预警取数层（/api/v1/market-analysis/sentiment）
 *
 * 数据由 `backend/scripts/market_sentiment_snapshot.py` 预计算成 JSON，API 只读转发。
 * 契约要点：
 * - `stress.score` 0~100，越高表示广度压力越大（普跌/超卖），越低表示过热；
 * - `indices[*].fan` 是「当前状态所在区间」的历史路径分位（k=0..20），单位 %；
 * - 缺失一律为 null，绝不回落成 0（0 在情绪分里是「无压力」，语义不同）。
 */

import { SERVICE_ENDPOINTS } from '../../../config/services';

const MA_API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis`;

export type IndicatorZone = 'low' | 'normal' | 'high';

export interface SentimentIndicator {
  key: string;
  label: string;
  value: number | null;
  unit: string;
  pct: number | null;
  zone: IndicatorZone;
}

export interface StressInfo {
  score: number;
  zone: string;
  zone_label: string;
  zone_note: string;
  /** 白话阶段名，如「下跌后段 / 超卖区」 */
  phase?: string;
  /** 一句话解释这个阶段意味着什么 */
  phase_detail?: string;
  percentile: number | null;
}

/** 单周期模型预测（P1 量能模型 + P2 概率校准） */
export interface HorizonForecast {
  pred: number | null;
  prob_up: number | null;
  base_prob: number | null;
  p10: number | null;
  p50: number | null;
  p90: number | null;
  n_train?: number;
  n_oos?: number;
  /** 样本外复盘（只用 as_of 及以前的数据；命中按非重叠样本算） */
  accuracy?: ForecastAccuracy | null;
  /** 最近若干次非重叠预测 vs 实际 */
  review?: ForecastReviewPoint[];
}

export interface ForecastAccuracy {
  n_oos: number;
  n_eff: number;
  hit: number | null;
  mae: number;
  ic: number | null;
}

export interface ForecastReviewPoint {
  date: string;
  pred: number;
  actual: number;
  hit: boolean;
}

export interface FactorHistoryItem {
  key: string;
  label: string;
  /** 冷度 0~100（越高越冷）；null = 当日无数据 */
  cold: (number | null)[];
}

export interface FanPoint {
  k: number;
  p10: number;
  p25: number;
  p50: number;
  p75: number;
  p90: number;
}

export interface IndexStats {
  n_days: number;
  n_clusters: number;
  mean: number;
  median: number;
  win: number;
  p10: number;
  p90: number;
  p_drop5: number;
  p_drop10: number;
}

export interface IndexBaseline {
  mean: number;
  win: number;
  p_drop10: number;
}

/** 回看模式下之后**真实走出**的路径（相对 as-of 收盘，%）。最新一期为空。 */
export interface ActualPoint {
  k: number;
  pct: number;
}

/** 单指数使用的广度宇宙（PIT 口径，前端如实标注「近似」）。 */
export interface UniverseMeta {
  key: string;
  label: string;
  /** board=按板别全样本；rank=逐日流通市值排名代理 */
  mode: string;
  note: string;
  n_last: number | null;
}

/** 七档涨跌分布（当日计数，互斥）：涨停/涨>5%/涨0~5%/平/跌0~5%/跌>5%/跌停 */
export type BreadthDistribution = Record<string, number>;

export interface IndexSentiment {
  name: string;
  last: number;
  change_pct: number | null;
  history: { date: string; close: number }[];
  fan: FanPoint[];
  stats: IndexStats;
  baseline: IndexBaseline;
  actual: ActualPoint[];
  /** 回看模式下已走出的天数（不足 20 天时 actual 是部分路径） */
  actual_days?: number;
  actual_horizon?: number;
  forecast: Record<string, HorizonForecast>;
  /** as-of 时给出真实未来交易日；最新一期为空（前端按工作日估算） */
  future_dates: string[];
  /** 该指数**自己宇宙**的广度压力分（分市场口径）；旧快照没有此字段 */
  stress?: StressInfo;
  indicators?: SentimentIndicator[];
  stress_history?: StressHistoryPoint[];
  factor_history?: { dates: string[]; items: FactorHistoryItem[] };
  dispersion?: { value: number | null; pct: number | null };
  universe?: UniverseMeta;
  distribution?: BreadthDistribution;
}

export interface StressHistoryPoint {
  date: string;
  stress: number;
  breadth20: number;
}

export interface MarketSentiment {
  trade_date: string;
  generated_at: string;
  source: string;
  horizon: number;
  /** 横截面个股分化度（%）：越高＝个股走势越分化 */
  dispersion?: { value: number | null; pct: number | null };
  stress: StressInfo;
  indicators: SentimentIndicator[];
  stress_history: StressHistoryPoint[];
  factor_history: { dates: string[]; items: FactorHistoryItem[] };
  /** 全A 口径的七档涨跌分布；旧快照没有此字段 */
  distribution?: BreadthDistribution;
  indices: Record<string, IndexSentiment>;
  /** true = 历史回看；false = 最新一期 */
  as_of: boolean;
  available_from: string;
  available_to: string;
}

export class SentimentError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'SentimentError';
    this.status = status;
  }
}

export async function fetchMarketSentiment(date?: string): Promise<MarketSentiment> {
  const token = localStorage.getItem('access_token') || '';
  const qs = date ? `?date=${encodeURIComponent(date)}` : '';
  const res = await fetch(`${MA_API}/sentiment${qs}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    const msg =
      typeof detail === 'string'
        ? detail
        : typeof detail === 'object' && detail !== null && 'message' in detail
          ? String((detail as { message: unknown }).message)
          : `大盘情绪接口失败 ${res.status}`;
    throw new SentimentError(msg, res.status);
  }
  if (body === null || typeof body !== 'object' || !('stress' in body)) {
    throw new SentimentError('大盘情绪接口结构异常', res.status);
  }
  return body as MarketSentiment;
}
