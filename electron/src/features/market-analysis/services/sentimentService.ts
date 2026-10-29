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


/** 成交量 × 主力加权系数（全市场口径；120 日曲线）。 */
export interface SentimentVolume {
  dates: string[];
  /** 全市场成交额（亿元） */
  turnover_yi: (number | null)[];
  /** (上涨股成交额 − 下跌股成交额) / 全市场成交额，−1~1 */
  vw_breadth: (number | null)[];
  /** 上涨股成交额 / 全市场成交额，0~1 */
  vw_breadth_pct: (number | null)[];
  /** (超大单+大单)净额 / 全市场成交额 */
  main_net_ratio: (number | null)[];
  main_net_yi: (number | null)[];
  /** 主力净流入为正的个股占比（等权） */
  main_breadth: (number | null)[];
  /** 主力净流入为正的个股成交额占比（成交额加权） */
  main_breadth_amt: (number | null)[];
  /** 散户（中单+小单）净额（亿元）；散户柱用 */
  retail_net_yi?: (number | null)[];
  latest: {
    date?: string | null;
    turnover_yi?: number | null;
    vw_breadth?: number | null;
    main_net_yi?: number | null;
    main_net_ratio?: number | null;
    main_net_z20?: number | null;
    main_breadth?: number | null;
    main_breadth_amt?: number | null;
  };
  conventions?: Record<string, string>;
  disclosure?: string;
}

/** 主力视角冷热分项（复用广度分项 UI；分位为扩张窗口）。 */
export interface MainForceBlock {
  latest: SentimentVolume['latest'];
  available?: boolean;
  available_from?: string | null;
  /** 0~100，越高=主力越偏买 */
  score?: number | null;
  /** 100−score，越高=主力越偏派发 */
  pressure?: number | null;
  history?: { dates: string[]; score: (number | null)[]; pressure: (number | null)[] };
  indicators: SentimentIndicator[];
  disclosure?: string;
}

/** 散户视角：唯一经历史验证的反向指标是「散户追涨相关」。 */
export interface RetailBlock {
  latest: SentimentVolume['latest'] & { retail_score?: number | null };
  available?: boolean;
  available_from?: string | null;
  verdict: { label: string; tone: 'warn' | 'good' | 'neutral'; hint: string };
  score: number | null;
  thresholds: { hot: number; cold: number };
  indicators: SentimentIndicator[];
  history: { dates: string[]; score: (number | null)[]; chase_corr: (number | null)[] };
  disclosure?: string;
}


/** 单指数/宇宙成交额序列（与扇形历史日期逐日对齐）。 */
export interface IndexVolume {
  scope?: string;
  dates: string[];
  turnover_yi: (number | null)[];
  vw_breadth: (number | null)[];
  vw_breadth_pct: (number | null)[];
}

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
  /** 该指数自己宇宙的成交额/加权系数（旧快照没有此字段） */
  volume?: IndexVolume;
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
  /** 成交量 × 主力加权系数曲线；旧快照没有此字段 */
  volume?: SentimentVolume;
  /** 主力视角冷热分项；旧快照没有此字段 */
  main_force?: MainForceBlock;
  /** 散户视角冷热分项；旧快照没有此字段 */
  retail?: RetailBlock;
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
