import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS } from '../config/services';
import { authService } from '../features/auth/services/authService';
import type { ResearchModelOption, ResearchStockRow } from '../features/research/types';
export type { ResearchModelOption, ResearchStockRow } from '../features/research/types';

export type ResearchSignal = 'buy' | 'hold' | 'sell';
export type ResearchConfidence = 'high' | 'medium' | 'watch';

export interface ResearchRunOption {
  runId: string;
  modelId: string;
  inferenceDate: string | null;
  targetDate: string | null;
  status: 'completed' | 'running' | 'failed';
  universeLabel: string;
  stockCount?: number;
  avgScore?: number;
  lastUpdatedAt?: string | null;
}

export interface ResearchOverviewData {
  activeModelId: string | null;
  activeRunId: string | null;
  models: ResearchModelOption[];
  runs: ResearchRunOption[];
  summary: {
    total: number;
    avgScore: number;
    highConfidenceCount: number;
    strongCount: number;
    lastUpdatedAt: string | null;
  };
  filters: {
    sectors: string[];
    concepts: string[];
    indices?: string[];
  };
  items: ResearchStockRow[];
  pagination?: {
    limit: number;
    offset: number;
    returned: number;
    total: number;
    hasMore: boolean;
  };
}

export interface ResearchOverviewQuery {
  modelId?: string;
  runId?: string;
  keyword?: string;
  minScore?: number;
  minConsecutiveLimitUpDays?: number;
  minTurnoverRate?: number;
  maxTurnoverRate?: number;
  minAmount?: number;
  maxAmount?: number;
  volumeTrendOnly?: boolean;
  highConfidenceOnly?: boolean;
  sectors?: string[];
  concepts?: string[];
  indices?: string[];
  sortBy?: 'score' | 'latest_change' | 'amount' | 'turnover_rate' | 'consecutive_limit_up_days' | 'updated_at';
  limit?: number;
  offset?: number;
}

interface ResearchOverviewResponse {
  code: number;
  message: string;
  data: ResearchOverviewData;
}

interface ResearchModelsResponse {
  code: number;
  message: string;
  data: {
    models: ResearchModelOption[];
  };
}

interface ResearchRunsResponse {
  code: number;
  message: string;
  data: {
    runs: ResearchRunOption[];
  };
}

interface ResearchUniverseResponse {
  code: number;
  message: string;
  data: {
    runId: string;
    summary: ResearchOverviewData['summary'];
    items: ResearchStockRow[];
    pagination?: ResearchOverviewData['pagination'];
  };
}

class ResearchService {
  private client: AxiosInstance;
  private readonly baseURL = (import.meta as any).env?.VITE_USER_API_URL || SERVICE_ENDPOINTS.USER_SERVICE;

  constructor() {
    this.client = axios.create({
      baseURL: this.baseURL,
      timeout: 120000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    this.client.interceptors.request.use((config) => {
      const token = authService.getAccessToken();
      if (token) {
        if (config.headers && typeof config.headers.set === 'function') {
          config.headers.set('Authorization', `Bearer ${token}`);
        } else if (config.headers) {
          config.headers.Authorization = `Bearer ${token}`;
        }
      }

      const tenantId = authService.getTenantId?.() || 'default';
      if (config.headers && typeof config.headers.set === 'function') {
        if (!config.headers.has('X-Tenant-Id') && !config.headers.has('x-tenant-id')) {
          config.headers.set('X-Tenant-Id', tenantId);
        }
      } else if (config.headers) {
        if (!config.headers['X-Tenant-Id'] && !config.headers['x-tenant-id']) {
          config.headers['X-Tenant-Id'] = tenantId;
        }
      }
      return config;
    });

    this.client.interceptors.response.use(
      (response) => response,
      async (error) => authService.handle401Error(error, this.client)
    );
  }

  async getOverview(query: ResearchOverviewQuery): Promise<ResearchOverviewData> {
    const params = new URLSearchParams();

    const append = (key: string, value: string | number | boolean | undefined | null): void => {
      if (value === undefined || value === null || value === '') return;
      params.append(key, String(value));
    };

    append('model_id', query.modelId);
    append('run_id', query.runId);
    append('keyword', query.keyword?.trim());
    append('min_score', query.minScore);
    append('min_consecutive_limit_up_days', query.minConsecutiveLimitUpDays);
    append('min_turnover_rate', query.minTurnoverRate);
    append('max_turnover_rate', query.maxTurnoverRate);
    append('min_amount', query.minAmount);
    append('max_amount', query.maxAmount);
    append('volume_trend_only', query.volumeTrendOnly);
    append('high_confidence_only', query.highConfidenceOnly);
    append('sort_by', query.sortBy);
    append('limit', query.limit);
    append('offset', query.offset);

    (query.sectors || []).forEach((sector) => append('sectors', sector));
    (query.concepts || []).forEach((concept) => append('concepts', concept));
    (query.indices || []).forEach((indexName) => append('indices', indexName));

    const queryString = params.toString();
    const url = queryString ? `/research/overview?${queryString}` : '/research/overview';
    const resp = await this.client.get<ResearchOverviewResponse>(url);
    return resp.data.data;
  }

  // ============ 自选接口 ============

  async addToWatchlist(symbol: string, options?: { runId?: string; stockName?: string; featuresSnapshot?: any }): Promise<void> {
    await this.client.post(`/research/watchlist/${symbol}`, {
      run_id: options?.runId,
      stock_name: options?.stockName,
      features_snapshot: options?.featuresSnapshot
    });
  }

  async removeFromWatchlist(symbol: string): Promise<void> {
    await this.client.delete(`/research/watchlist/${symbol}`);
  }

  async getWatchlist(limit = 50, offset = 0): Promise<{ items: WatchlistItem[]; total: number }> {
    const resp = await this.client.get<WatchlistResponse>(`/research/watchlist?limit=${limit}&offset=${offset}`);
    return resp.data.data;
  }

  // ============ 研究池接口 ============

  async addToResearchPool(symbol: string, options?: {
    runId?: string;
    stockName?: string;
    modelId?: string;
    fusionScore?: number;
    thesisSummary?: string;
    featuresSnapshot?: any;
  }): Promise<void> {
    await this.client.post(`/research/pool/${symbol}`, {
      run_id: options?.runId,
      stock_name: options?.stockName,
      model_id: options?.modelId,
      fusion_score: options?.fusionScore,
      thesis_summary: options?.thesisSummary,
      features_snapshot: options?.featuresSnapshot
    });
  }

  async removeFromResearchPool(symbol: string): Promise<void> {
    await this.client.delete(`/research/pool/${symbol}`);
  }

  async getResearchPool(options?: { status?: string; limit?: number; offset?: number }): Promise<{ items: ResearchPoolItem[]; total: number }> {
    const params = new URLSearchParams();
    if (options?.status) params.append('status', options.status);
    if (options?.limit) params.append('limit', String(options.limit));
    if (options?.offset) params.append('offset', String(options.offset));
    const url = params.toString() ? `/research/pool?${params}` : '/research/pool';
    const resp = await this.client.get<ResearchPoolResponse>(url);
    return resp.data.data;
  }

  async getFeaturesBySymbols(symbols: string[], options?: { lite?: boolean }): Promise<ResearchStockRow[]> {
    if (!symbols || symbols.length === 0) return [];
    const lite = options?.lite ? '?lite=true' : '';
    const resp = await this.client.post<{ data: { items: ResearchStockRow[] } }>(`/research/symbols/features${lite}`, { symbols });
    return resp.data?.data?.items || [];
  }

  // ============ K 线数据接口 ============

  async getKlineData(symbol: string, days = 60): Promise<KlineDataItem[]> {
    const resp = await this.client.get<KlineResponse>(`/research/kline/${symbol}?days=${days}`);
    return resp.data.data.items || [];
  }

  // ============ 兼容方法（对接模型中心） ============

  async getAvailableModels(): Promise<ResearchModelOption[]> {
    // 使用轻量接口避免 overview 重查询导致首屏模型加载超时
    try {
      const resp = await this.client.get<ResearchModelsResponse>('/research/models');
      return resp.data?.data?.models || [];
    } catch (error) {
      console.error('[ResearchService] getAvailableModels failed:', error);
      return [];
    }
  }

  async getInferenceRuns(modelId: string): Promise<ResearchRunOption[]> {
    // 使用轻量接口避免 overview 重查询导致批次加载超时
    try {
      const resp = await this.client.get<ResearchRunsResponse>(`/research/runs?model_id=${encodeURIComponent(modelId)}`);
      return resp.data?.data?.runs || [];
    } catch (error) {
      console.error('[ResearchService] getInferenceRuns failed:', error);
      return [];
    }
  }

  async getResearchUniverse(runId: string, limit: number = 2000, offset: number = 0): Promise<{ candidates: any[], summary: any }> {
    const resp = await this.client.get<ResearchUniverseResponse>(
      `/research/universe?run_id=${encodeURIComponent(runId)}&limit=${limit}&offset=${offset}`
    );
    const data = resp.data.data;
    return {
      candidates: data.items || [],
      summary: data.summary || { total: 0, avgScore: 0, highConfidenceCount: 0, strongCount: 0, lastUpdatedAt: null }
    };
  }
}

export interface KlineDataItem {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface KlineResponse {
  code: number;
  message: string;
  data: {
    symbol: string;
    items: KlineDataItem[];
    count: number;
  };
}

export interface WatchlistItem {
  symbol: string;
  stockName: string | null;
  addedAt: string | null;
  sourceRunId: string | null;
  notes: string | null;
  tags: string[];
}

export interface ResearchPoolItem {
  symbol: string;
  stockName: string | null;
  addedAt: string | null;
  sourceRunId: string | null;
  modelId: string | null;
  fusionScore: number | null;
  thesisSummary: string | null;
  status: string;
  notes: string | null;
  tags: string[];
}

interface WatchlistResponse {
  code: number;
  message: string;
  data: { items: WatchlistItem[]; total: number };
}

interface ResearchPoolResponse {
  code: number;
  message: string;
  data: { items: ResearchPoolItem[]; total: number };
}

export const researchService = new ResearchService();