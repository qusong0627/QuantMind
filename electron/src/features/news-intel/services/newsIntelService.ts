/**
 * 资讯中心 2.0 服务 — /api/v1/news-intel
 *
 * 口径：一条新闻一个主标签（决策等级），权重只用于排序；
 * 展示面不出现买入/卖出/减仓。
 */

import axios from 'axios';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import { authService } from '../../auth/services/authService';

const apiClient = axios.create({ timeout: 30000 });

apiClient.interceptors.request.use((config) => {
  config.baseURL = SERVICE_ENDPOINTS.USER_SERVICE;
  const token = authService.getAccessToken();
  if (token) {
    (config.headers as Record<string, string>).Authorization = `Bearer ${token}`;
  }
  return config;
});

export type Decision = 'P0' | 'P1' | 'P2' | 'WATCH' | 'INFO';
export type Importance = 'high' | 'medium' | 'low';
export type MarketKey = 'CN' | 'HK' | 'US' | 'FUTURES';

export interface NewsIntelItem {
  page_id: number;
  title: string;
  url: string;
  source: string;
  published_at: string | null;
  sentiment_label?: string | null;
  weight: number;
  importance: Importance;
  decision: Decision;
  action: string;
  primary_label: string;
  category: string;
  direction: string;
  event_type: string;
  tier: string;
  is_subject: boolean;
  risk_level: 'critical' | 'high' | 'none';
  speculative: boolean;
  layer: number;
  is_dup: boolean;
  aggregator: boolean;
  is_research?: boolean;
  is_roundup?: boolean;
  source_quality: number | null;
  pre5: number | null;
  pre5_bucket: string;
  reasons: string[];
  code?: string;
  name?: string;
}

export interface StockNewsResponse {
  code: string;
  name: string;
  market?: MarketKey;
  pre5: number | null;
  pre5_bucket: string;
  as_of: string;
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  items: NewsIntelItem[];
}

export interface StockProfile {
  code: string;
  name: string;
  market?: MarketKey;
  ambiguous?: boolean;
  days: number;
  n_articles: number;
  n_subject: number;
  n_sources: number;
  n_speculative: number;
  pre5: number | null;
  decisions: Record<Decision, number>;
  directions: Record<string, number>;
  events?: Record<string, number>;
  risks: Record<string, number>;
  heat: Array<{ date: string; n: number }>;
  top_events: NewsIntelItem[];
  read_rule: string;
  as_of: string;
}

export interface StockListItem {
  code: string;
  name: string;
  market: MarketKey;
  latest_at: string | null;
  n_articles: number;
  /** /stocks/search 命中来源：代码直配 / 名称或别名命中。 */
  is_code?: boolean;
  is_name?: boolean;
}

export interface StockScoreInfo {
  found: boolean;
  model_id: string;
  code?: string;
  trade_date?: string;
  data_trade_date?: string;
  score?: number | null;
  rank_pct?: number | null;
  rank?: number | null;
  total?: number;
  position?: 'top' | 'mid' | 'bottom' | null;
  reason?: string | null;
}

export interface OverviewData {
  market: MarketKey;
  days: number;
  n_articles: number;
  n_positive: number;
  n_negative: number;
  n_risk: number;
  n_speculative: number;
  topics: TopicRow[];
  as_of: string;
}

export interface TopicRow {
  name: string;
  n_articles: number;
  n_positive: number;
  n_negative: number;
  n_risk: number;
  top_tickers?: Array<{ code: string; name?: string; n: number }>;
}

export interface TopicsResponse {
  market: MarketKey;
  days: number;
  items: TopicRow[];
  as_of?: string;
}

export interface TopicNewsResponse {
  market: MarketKey;
  topic: string;
  days: number;
  items: NewsIntelItem[];
  as_of: string;
}

export interface StockSummary {
  n_articles: number;
  n_risk: number;
  n_negative: number;
  n_positive: number;
  n_neutral: number;
  n_hard_negative: number;
  n_hard_positive: number;
  n_soft_negative: number;
  n_speculative: number;
  max_weight: number;
  top_label: string;
  decision: Decision | '';
}

export interface SectorRow {
  code: string;
  name: string;
  type: 'concept' | 'industry1' | 'industry2' | 'other';
  n_articles: number;
  n_positive: number;
  n_negative: number;
  n_risk: number;
  n_spec: number;
  sent_mean: number | null;
  neg_ratio: number;
  risk_ratio: number;
  spec_ratio: number;
}

export interface SectorListResponse {
  available: boolean;
  reason?: string;
  type: string;
  days: number;
  as_of?: string;
  items: SectorRow[];
}

export interface SectorDetailResponse {
  available: boolean;
  reason?: string;
  sector: { code: string; name: string; type: string };
  days: number;
  n_articles: number;
  n_positive?: number;
  n_negative?: number;
  n_neutral?: number;
  top_stocks: Array<{ code: string; name: string; n: number }>;
  read_rule: string;
  items: NewsIntelItem[];
  as_of: string;
}

export interface SubscriptionRow {
  symbol: string;
  name: string | null;
  pinned: boolean;
  sort_order: number;
  source?: 'manual' | 'live' | 'sim';
  created_at: string | null;
}

class NewsIntelService {
  async stockNews(
    code: string,
    params: {
      days?: number;
      limit?: number;
      offset?: number;
      sort?: 'weight' | 'time';
      bucket?: 'all' | 'important' | 'risk' | 'spec';
      direction?: string;
      decision?: string;
      risk_type?: string;
      subject_only?: boolean;
      spec_only?: boolean;
    } = {},
  ): Promise<StockNewsResponse> {
    const r = await apiClient.get(`/news-intel/stocks/${encodeURIComponent(code)}/news`, {
      params,
    });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async stockProfile(code: string, days = 90): Promise<StockProfile> {
    const r = await apiClient.get(`/news-intel/stocks/${encodeURIComponent(code)}/profile`, {
      params: { days },
    });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async stocksSummary(codes: string[], days = 7): Promise<{ days: number; items: Record<string, StockSummary> }> {
    const r = await apiClient.get('/news-intel/stocks/summary', {
      params: { codes: codes.join(','), days },
    });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async stockScore(code: string, modelId: string): Promise<StockScoreInfo> {
    const r = await apiClient.get('/news-intel/stock-score', {
      params: { code, model_id: modelId },
    });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async listStocks(market: MarketKey, days = 7, limit = 300): Promise<{ items: StockListItem[]; as_of?: string }> {
    const r = await apiClient.get('/news-intel/stocks', {
      params: { market, days, limit },
    });
    return (r.data as any)?.data ?? (r.data as any);
  }

  /** 关键词（代码/名称/新闻标题词）→ 股票候选。 */
  async searchStocks(
    q: string,
    market: MarketKey,
    days = 30,
    limit = 30,
  ): Promise<{ items: StockListItem[]; q?: string; market?: string }> {
    const r = await apiClient.get('/news-intel/stocks/search', {
      params: { q, market, days, limit },
    });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async sectors(type: 'concept' | 'industry1' | 'industry2', days = 5, limit = 60): Promise<SectorListResponse> {
    const r = await apiClient.get('/news-intel/sectors', { params: { type, days, limit } });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async sectorDetail(code: string, days = 5, limit = 40): Promise<SectorDetailResponse> {
    const r = await apiClient.get(`/news-intel/sectors/${encodeURIComponent(code)}`, {
      params: { days, limit },
    });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async overview(market: MarketKey, days = 1): Promise<OverviewData> {
    const r = await apiClient.get('/news-intel/overview', { params: { market, days } });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async topics(market: MarketKey, days = 5, limit = 40): Promise<TopicsResponse> {
    const r = await apiClient.get('/news-intel/topics', { params: { market, days, limit } });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async commodities(days = 5, limit = 30): Promise<TopicsResponse> {
    const r = await apiClient.get('/news-intel/commodities', { params: { days, limit } });
    const data = (r.data as any)?.data ?? (r.data as any);
    return { market: 'FUTURES', days: data?.days ?? days, items: data?.items ?? [], as_of: data?.as_of };
  }

  async topicNews(
    market: MarketKey,
    topic: string,
    days = 5,
    limit = 40,
  ): Promise<TopicNewsResponse> {
    const r = await apiClient.get('/news-intel/topic-news', {
      params: { market, topic, days, limit },
    });
    return (r.data as any)?.data ?? (r.data as any);
  }

  async syncPositions(): Promise<{
    synced: number;
    live?: number;
    sim?: number;
    symbols: string[];
    reason?: string;
  }> {
    const r = await apiClient.post('/news-intel/subscriptions/sync-positions');
    return (r.data as any)?.data ?? (r.data as any);
  }

  async listSubscriptions(): Promise<{ items: SubscriptionRow[] }> {
    const r = await apiClient.get('/news-intel/subscriptions');
    return (r.data as any)?.data ?? (r.data as any);
  }

  async addSubscription(code: string, name?: string, pinned = true): Promise<void> {
    await apiClient.post('/news-intel/subscriptions', { code, name, pinned });
  }

  async updateSubscription(
    code: string,
    payload: { pinned?: boolean; sort_order?: number },
  ): Promise<void> {
    await apiClient.patch(`/news-intel/subscriptions/${encodeURIComponent(code)}`, payload);
  }

  async removeSubscription(code: string): Promise<void> {
    await apiClient.delete(`/news-intel/subscriptions/${encodeURIComponent(code)}`);
  }

  async feed(days = 14, limit = 50): Promise<{ items: NewsIntelItem[]; subscribed: SubscriptionRow[] }> {
    const r = await apiClient.get('/news-intel/feed', { params: { days, limit } });
    return (r.data as any)?.data ?? (r.data as any);
  }
}

export const newsIntelService = new NewsIntelService();
