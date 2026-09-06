import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';

export interface FeatureDriverItem {
  name: string;
  category?: string;
  value?: number;
  impact: number;
  direction: 'positive' | 'negative';
}

export interface ModelConsensusItem {
  model_id: string;
  model_name: string;
  model_type: string;
  score: number;
  expected_return: number;
  rating: 'STRONG_BUY' | 'BUY' | 'HOLD' | 'SELL';
  horizon: number;
}

export interface ForecastPoint {
  step: number;
  date: string;
  p10: number;
  p50: number;
  p90: number;
  predicted_price: number;
  upper_price: number;
  lower_price: number;
}

export interface SingleStockPredictionResponse {
  status: string;
  symbol: string;
  stock_name: string;
  model_id: string;
  model_name: string;
  model_type: string;
  as_of_date: string;
  current_price: number;
  horizon: number;
  predicted_score: number;
  expected_return: number;
  confidence: number;
  rating: 'STRONG_BUY' | 'BUY' | 'HOLD' | 'SELL';
  p10_return: number | null;
  p50_return: number;
  p90_return: number | null;
  forecast_curve: ForecastPoint[];
  forecast_warning?: string | null;
  drivers: FeatureDriverItem[];
  consensus: ModelConsensusItem[];
  consensus_score: number;
  /** 仅为真实持久化模型推理分数。 */
  data_source?: 'persisted';
  /** 仅在模型可提供时返回真实 SHAP。 */
  drivers_source?: 'shap';
  error?: string | null;
}

export interface SingleStockPredictionRequest {
  symbol: string;
  model_id?: string;
  date?: string;
  horizon?: number;
  market?: string;
  /** 共识矩阵成员（最多4个真实模型）；空=自动取当日全部有分数模型 */
  consensus_model_ids?: string[];
  /** 是否立即执行已注册模型；false 时仅读取已有真实结果。 */
  execute?: boolean;
}

export interface KlineItem {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface AvailableModelOption {
  modelId: string;
  modelName: string;
  modelType: string;
  description?: string;
  accuracy?: number;
  isEnsemble?: boolean;
  hasInference?: boolean;
}

class InferenceCenterService {
  private get client(): AxiosInstance {
    const baseURL = resolveWebSafeServiceBase(
      (import.meta as any).env?.VITE_USER_API_URL,
      SERVICE_ENDPOINTS.API_GATEWAY || SERVICE_ENDPOINTS.USER_SERVICE,
    );
    const client = axios.create({
      baseURL,
      // 实际模型执行会跑完整个推理批次，30 秒不足以覆盖生产模型冷启动与落库。
      timeout: 120000,
    });
    client.interceptors.request.use((config) => {
      const token = authService.getAccessToken();
      if (token) {
        if (config.headers && typeof config.headers.set === 'function') {
          config.headers.set('Authorization', `Bearer ${token}`);
        } else if (config.headers) {
          config.headers['Authorization'] = `Bearer ${token}`;
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
    return client;
  }

  async getAvailableModels(market?: string): Promise<AvailableModelOption[]> {
    try {
      const params: Record<string, string> = {};
      if (market) params.market = market;
      const resp = await this.client.get('/research/models', { params });
      const body = (resp.data ?? {}) as any;
      const items =
        body?.data?.models ??
        body?.data?.items ??
        body?.models ??
        body?.items ??
        [];
      return items.map((m: any) => ({
        modelId: m.modelId ?? m.model_id,
        modelName: m.name || m.modelName || m.model_name || m.modelId,
        modelType: m.modelType || m.model_type || '',
        description: m.description,
        accuracy: m.ic ?? m.accuracy ?? m.ic_value,
        hasInference: m.hasInference ?? m.has_inference ?? false,
      }));
    } catch (e) {
      console.warn('获取可用模型列表失败:', e);
      return [];
    }
  }

  async getStockKline(symbol: string, days: number = 60): Promise<KlineItem[]> {
    try {
      const resp = await this.client.get<{ code: number; data: { items: KlineItem[] } }>(`/research/kline/${encodeURIComponent(symbol)}?days=${days}`);
      return resp.data?.data?.items || [];
    } catch (e) {
      console.warn('获取股票K线失败:', e);
      return [];
    }
  }

  async predictSingleStock(req: SingleStockPredictionRequest): Promise<SingleStockPredictionResponse> {
    const resp = await this.client.post<{ code?: number; data?: SingleStockPredictionResponse } | SingleStockPredictionResponse>('/research/predict-stock', req);
    if ((resp.data as any)?.data) {
      return (resp.data as any).data;
    }
    return resp.data as SingleStockPredictionResponse;
  }
}

export const inferenceCenterService = new InferenceCenterService();
