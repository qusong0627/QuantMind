/**
 * 资讯源服务 — 调用 QuantMind 后端的 /api/v1/news 代理
 * 后端再代理到 Huntly (lcomplete/huntly:latest)
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

export interface NewsSource {
  source_id: number;
  source_name: string;
  subscribe_url?: string;
  type?: string;
  folder_id: number;
  folder_name: string;
  site_avatar_url?: string;
  unread_count?: number;
}

export interface NewsFolder {
  folder_id: number;
  folder_name: string;
  source_count: number;
  unread_count: number;
}

export interface NewsEnrichment {
  tickers: string[];
  industries: string[];
  event_tags: string[];
  sentiment_score: number | null;
  sentiment_label: 'bullish' | 'bearish' | 'neutral' | null;
  sentiment_confidence: number | null;
  countries?: string[];
  regions?: string[];
  key_terms?: string[];
  date_entities?: string[];
  /** 实体级情感: { "ticker:600519.SH": 0.7, "country:美国": -0.4, "key_term:AI": 0.5, "region:欧盟": -0.2 } */
  entity_sentiments?: Record<string, number>;
  provinces?: string[];
  cities?: string[];
  politicians?: string[];
  visits?: string[];
  departments?: string[];
}

export interface NewsArticle {
  id: number;
  title: string;
  summary?: string;
  url?: string;
  source_id?: number;
  source_name?: string;
  folder_id?: number;
  published_at?: string;
  read: boolean;
  starred: boolean;
  is_financial_event: boolean;
  thumbnail?: string;
  enrichment?: NewsEnrichment;
}

export interface NewsArticleDetail extends NewsArticle {
  content?: string;
  content_html?: string;
}

export interface NewsEnrichmentStats {
  top_industries: Array<{ name: string; count: number }>;
  top_events: Array<{ name: string; count: number }>;
  top_tickers: Array<{ ticker: string; name: string; count: number }>;
  sentiment_counts: Record<string, number>;
  top_countries?: Array<{ name: string; count: number }>;
  top_regions?: Array<{ name: string; count: number }>;
  top_key_terms?: Array<{ name: string; count: number }>;
  top_dates?: Array<{ name: string; count: number }>;
  top_provinces?: Array<{ name: string; count: number }>;
  top_cities?: Array<{ name: string; count: number }>;
  top_politicians?: Array<{ name: string; count: number }>;
  top_visits?: Array<{ name: string; count: number }>;
  top_departments?: Array<{ name: string; count: number }>;
}

export interface NewsHealthInfo {
  huntly_status: 'up' | 'down' | 'unreachable';
  huntly_http_code?: number;
  huntly_base_url: string;
  error?: string;
}

class NewsService {
  async health(): Promise<NewsHealthInfo> {
    const r = await apiClient.get<NewsHealthInfo>('/news/health');
    return (r as any).data ?? (r as any);
  }

  async listSources(): Promise<{ sources: NewsSource[]; folders: NewsFolder[] }> {
    const r = await apiClient.get<{ sources: NewsSource[]; folders: NewsFolder[] }>('/news/sources');
    const body = (r as any).data ?? (r as any);
    return { sources: body.sources ?? [], folders: body.folders ?? [] };
  }

  async refreshSource(source_id: number): Promise<void> {
    await apiClient.post(`/news/sources/${source_id}/refresh`);
  }

  async listArticles(params: {
    source_id?: number;
    source_ids?: string;
    folder_id?: number;
    keyword?: string;
    only_financial_event?: boolean;
    starred?: boolean;
    tickers?: string;
    industries?: string;
    sentiment?: 'bullish' | 'bearish' | 'neutral';
    event_tags?: string;
    countries?: string;
    regions?: string;
    key_terms?: string;
    date_entities?: string;
    provinces?: string;
    cities?: string;
    politicians?: string;
    visits?: string;
    departments?: string;
    strong_only?: boolean;
    sort?: string;
    since?: string;
    until?: string;
    page?: number;
    page_size?: number;
  } = {}): Promise<{
    articles: NewsArticle[];
    total: number;
    matched_total?: number;
    page: number;
    page_size: number;
    latest_published_at?: string;
    server_time?: string;
  }> {
    const r = await apiClient.get('/news/articles', { params });
    return (r as any).data ?? (r as any);
  }

  async getArticle(id: number): Promise<NewsArticleDetail> {
    const r = await apiClient.get<NewsArticleDetail>(`/news/articles/${id}`);
    return (r as any).data ?? (r as any);
  }

  async enrichmentStats(params: {
    tickers?: string;
    industries?: string;
    sentiment?: 'bullish' | 'bearish' | 'neutral';
    event_tags?: string;
    countries?: string;
    regions?: string;
    key_terms?: string;
    date_entities?: string;
    provinces?: string;
    cities?: string;
    politicians?: string;
    visits?: string;
    departments?: string;
    strong_only?: boolean;
    keyword?: string;
    since?: string;
    until?: string;
  } = {}): Promise<NewsEnrichmentStats> {
    const r = await apiClient.get('/news/enrichment/stats', { params });
    return (r as any).data ?? (r as any);
  }

  async runEnrichmentNow(limit = 200): Promise<{ ok: boolean; written: number }> {
    const r = await apiClient.post('/news/enrichment/run', null, { params: { limit } });
    return (r as any).data ?? (r as any);
  }

  async rebuildAllEnrichment(force = false): Promise<{
    started: boolean;
    reason?: string;
    running: boolean;
    total: number;
    processed: number;
    ok: number;
    failed: number;
    elapsed_seconds?: number;
    eta_seconds?: number | null;
  }> {
    const r = await apiClient.post('/news/enrichment/rebuild-all', null, { params: { force } });
    return (r as any).data ?? (r as any);
  }

  async getRebuildProgress(): Promise<{
    running: boolean;
    total: number;
    processed: number;
    ok: number;
    failed: number;
    started_at: number | null;
    finished_at: number | null;
    error: string | null;
    force: boolean;
    elapsed_seconds: number;
    eta_seconds: number | null;
  }> {
    const r = await apiClient.get('/news/enrichment/rebuild-progress');
    return (r as any).data ?? (r as any);
  }

  /** FinBERT 中文金融情感模型健康状态（管理后台诊断面板） */
  async adminFinbertStatus(): Promise<{
    available: boolean;
    use_finbert: boolean;
    model: string;
    device: number;
    sample_inference: { label: string; confidence: number } | null;
    db_total_24h: number;
    db_finbert_ratio_24h: number | null;
    tip: string;
  }> {
    const r = await apiClient.get('/news/enrichment/finbert-status');
    return (r as any).data ?? (r as any);
  }

  async toggleStar(id: number, starred: boolean): Promise<void> {
    await apiClient.post(`/news/articles/${id}/star`, null, { params: { starred } });
  }

  async markRead(id: number, read: boolean): Promise<void> {
    await apiClient.post(`/news/articles/${id}/read`, null, { params: { read } });
  }

  // ---------- Admin: RSS 源管理 (Huntly CRUD proxy) ----------

  async adminListFolders(): Promise<{ folders: HuntlyFolder[] }> {
    const r = await apiClient.get('/news/admin/folders');
    return (r as any).data ?? (r as any);
  }

  async adminCreateFolder(name: string): Promise<HuntlyFolder> {
    const r = await apiClient.post('/news/admin/folders', { name });
    return (r as any).data ?? (r as any);
  }

  async adminRenameFolder(folderId: number, name: string): Promise<HuntlyFolder> {
    const r = await apiClient.put(`/news/admin/folders/${folderId}`, { name });
    return (r as any).data ?? (r as any);
  }

  async adminDeleteFolder(folderId: number): Promise<void> {
    await apiClient.delete(`/news/admin/folders/${folderId}`);
  }

  async adminPreviewFeed(subscribeUrl: string): Promise<HuntlyFeedPreview> {
    const r = await apiClient.get('/news/admin/preview', {
      params: { subscribe_url: subscribeUrl },
    });
    return (r as any).data ?? (r as any);
  }

  async adminCreateSource(payload: {
    subscribe_url: string;
    folder_id?: number | null;
    name?: string;
  }): Promise<{ ok: boolean; connector_id?: number }> {
    const r = await apiClient.post('/news/admin/sources', payload);
    return (r as any).data ?? (r as any);
  }

  async adminUpdateSource(
    connectorId: number,
    payload: Partial<{
      name: string;
      folder_id: number | null;
      fetch_interval_minutes: number;
      enabled: boolean;
      crawl_full_content: boolean;
    }>,
  ): Promise<void> {
    await apiClient.put(`/news/admin/sources/${connectorId}`, payload);
  }

  async adminDeleteSource(connectorId: number): Promise<void> {
    await apiClient.delete(`/news/admin/sources/${connectorId}`);
  }

  async adminGetSourceSetting(connectorId: number): Promise<HuntlyFeedSetting> {
    const r = await apiClient.get(`/news/admin/sources/${connectorId}/setting`);
    return (r as any).data ?? (r as any);
  }

  // ---------- Admin: 标签管理 (finance_lexicon CRUD) ----------

  async adminListTags(params: {
    page?: number;
    page_size?: number;
    event_tag?: string;
    kind?: string;
    keyword?: string;
  } = {}): Promise<{ items: LexiconTag[]; total: number; page: number; page_size: number }> {
    const r = await apiClient.get('/news/admin/tags', { params });
    return (r as any).data ?? (r as any);
  }

  async adminCreateTag(payload: {
    term: string;
    kind: string;
    event_tag?: string;
    weight?: number;
    note?: string;
  }): Promise<LexiconTag> {
    const r = await apiClient.post('/news/admin/tags', payload);
    return (r as any).data ?? (r as any);
  }

  async adminUpdateTag(tagId: number, payload: Partial<{
    term: string;
    kind: string;
    event_tag: string;
    weight: number;
    note: string;
    enabled: boolean;
  }>): Promise<LexiconTag> {
    const r = await apiClient.put(`/news/admin/tags/${tagId}`, payload);
    return (r as any).data ?? (r as any);
  }

  async adminDeleteTag(tagId: number): Promise<void> {
    await apiClient.delete(`/news/admin/tags/${tagId}`);
  }

  async adminToggleTag(tagId: number): Promise<LexiconTag> {
    const r = await apiClient.patch(`/news/admin/tags/${tagId}/toggle`);
    return (r as any).data ?? (r as any);
  }
}

export interface HuntlyConnector {
  id: number;
  name?: string;
  subscribeUrl?: string;
  type?: string;
  iconUrl?: string;
  inboxCount?: number;
}

export interface HuntlyFolder {
  id: number | null;
  name: string | null;
  displaySequence?: number | null;
  createdAt?: string | null;
  connectors?: HuntlyConnector[];
}

export interface HuntlyFeedPreview {
  title?: string;
  description?: string;
  siteLink?: string;
  feedUrl?: string;
  siteFaviconUrl?: string;
  subscribed?: number | null;
}

export interface HuntlyFeedSetting {
  connectorId: number;
  folderId: number | null;
  name: string;
  subscribeUrl: string;
  crawlFullContent?: boolean | null;
  defaultFetchIntervalMinutes?: number;
  fetchIntervalMinutes?: number | null;
  enabled: boolean;
}

export interface LexiconTag {
  id: number;
  term: string;
  kind: string;
  event_tag: string | null;
  weight: number;
  note: string | null;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
}

export const newsService = new NewsService();
