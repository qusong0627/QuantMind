import axios, { AxiosInstance } from 'axios';
import { authService } from '../features/auth/services/authService';
import { SERVICE_ENDPOINTS } from '../config/services';

export interface HubModelItem {
  id: string;
  author_username: string;
  name: string;
  description: string;
  market: string;
  algorithm: string;
  target_horizon: string;
  target_mode: string;
  test_ic: number;
  rank_ic: number;
  sharpe_ratio: number;
  annual_return: number;
  max_drawdown: number;
  calmar_ratio: number;
  psi: number;
  equity_curve?: Array<{ date: string; value: number; benchmark?: number }>;
  factors_summary?: string[] | { count: number; items?: string[] };
  file_size_bytes: number;
  visibility: string;
  status: string;
  is_verified: boolean;
  downloads_count: number;
  likes_count: number;
  created_at: string;
  updated_at: string;
}

export interface HubModelListResponse {
  total: number;
  page: number;
  page_size: number;
  items: HubModelItem[];
}

export interface CreateUploadTicketPayload {
  name: string;
  description?: string;
  market?: string;
  algorithm: string;
  target_horizon?: string;
  target_mode?: string;
  test_ic?: number;
  rank_ic?: number;
  sharpe_ratio?: number;
  annual_return?: number;
  max_drawdown?: number;
  calmar_ratio?: number;
  psi?: number;
  equity_curve?: any;
  factors_summary?: any;
  extra_metrics?: any;
  file_size_bytes?: number;
  visibility?: string;
}

export interface UploadTicketResponse {
  model_id: string;
  upload_url: string;
  cos_key: string;
  expire_in: number;
}

export interface DownloadTicketResponse {
  model_id: string;
  download_url: string;
  expire_in: number;
  file_size_bytes: number;
}

class ModelHubService {
  private axiosInstance: AxiosInstance;
  private readonly defaultQuantDBHost = 'https://quantdb.quantmind.cloud';

  constructor() {
    this.axiosInstance = axios.create({
      baseURL: this.getQuantDBApiHost(),
      timeout: 30000,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  /**
   * 写操作（上传/发布/点赞/下架）经平台网关代理转发到模型广场，
   * 由后端注入服务端已配置的 QUANTDB_API_KEY，前端无需携带明文 Key。
   */
  private async gatewayWrite<T>(
    method: 'post' | 'delete',
    path: string,
    data?: unknown,
  ): Promise<T> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = authService.getAccessToken();
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    const resp = await axios.request<T>({
      method,
      url: `${SERVICE_ENDPOINTS.API_GATEWAY}${path}`,
      data,
      headers,
    });
    return resp.data;
  }

  private getQuantDBApiHost(): string {
    if (typeof window !== 'undefined') {
      const customHost = localStorage.getItem('quantdb_api_host');
      if (customHost && customHost.startsWith('http')) {
        return customHost.trim();
      }
    }
    return (import.meta as any).env?.VITE_QUANTDB_API_HOST || this.defaultQuantDBHost;
  }

  /**
   * 获取广场模型列表
   */
  async listModels(params: {
    market?: string;
    algorithm?: string;
    sort_by?: string;
    query?: string;
    author?: string;
    page?: number;
    page_size?: number;
  }): Promise<HubModelListResponse> {
    try {
      const resp = await this.axiosInstance.get('/api/v1/hub/models', {
        params: {
          market: params.market && params.market !== 'ALL' ? params.market : undefined,
          algorithm: params.algorithm && params.algorithm !== 'ALL' ? params.algorithm : undefined,
          sort_by: params.sort_by || 'sharpe',
          q: params.query || undefined,
          author: params.author || undefined,
          page: params.page || 1,
          page_size: params.page_size || 20,
        },
      });
      return resp.data;
    } catch (err: any) {
      console.warn('请求 QuantDB 模型广场失败，尝试使用本地网关回退:', err);
      // 回退使用统一网关
      const fallbackUrl = `${SERVICE_ENDPOINTS.API_GATEWAY}/api/v1/hub/models`;
      const fallbackResp = await axios.get(fallbackUrl, {
        params,
        headers: {
          Authorization: `Bearer ${authService.getAccessToken() || ''}`,
        },
      }).catch(() => null);

      if (fallbackResp?.data) {
        return fallbackResp.data;
      }
      throw err;
    }
  }

  /**
   * 获取单个模型详情（含净值曲线与特征列表）
   */
  async getModelDetail(modelId: string): Promise<HubModelItem> {
    const resp = await this.axiosInstance.get(`/api/v1/hub/models/${modelId}`);
    return resp.data;
  }

  /**
   * 申请上传直传凭证
   */
  async createUploadTicket(payload: CreateUploadTicketPayload): Promise<UploadTicketResponse> {
    return this.gatewayWrite('post', '/hub/models/upload-ticket', payload);
  }

  /**
   * 确认上传完成并激活模型
   */
  async publishModel(modelId: string): Promise<{ message: string; model_id: string }> {
    return this.gatewayWrite('post', `/hub/models/${modelId}/publish`);
  }

  /**
   * 发布本地模型到广场（后端打包 tar.gz → 上传 COS → 激活发布）
   */
  async publishLocalModel(payload: {
    model_id: string;
    name: string;
    description?: string;
    market?: string;
    algorithm?: string;
    target_horizon?: string;
    target_mode?: string;
    test_ic?: number;
    rank_ic?: number;
    sharpe_ratio?: number;
    annual_return?: number;
    max_drawdown?: number;
    calmar_ratio?: number;
    visibility?: string;
  }): Promise<{ success: boolean; model_id: string; packaged_size: number; detail: any }> {
    return this.gatewayWrite('post', '/hub/publish-local', payload);
  }

  /**
   * 获取下载直链
   */
  async getDownloadTicket(modelId: string): Promise<DownloadTicketResponse> {
    const resp = await this.axiosInstance.get(`/api/v1/hub/models/${modelId}/download-ticket`);
    return resp.data;
  }

  /**
   * 为模型点赞
   */
  async likeModel(modelId: string): Promise<{ message: string; model_id: string }> {
    return this.gatewayWrite('post', `/hub/models/${modelId}/like`);
  }

  /**
   * 下架或删除模型
   */
  async deleteModel(modelId: string): Promise<{ message: string; model_id: string }> {
    return this.gatewayWrite('delete', `/hub/models/${modelId}`);
  }
}

export const modelHubService = new ModelHubService();
