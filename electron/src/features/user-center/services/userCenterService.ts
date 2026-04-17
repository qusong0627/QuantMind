import axios, { AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios';
import { authService } from '../../auth/services/authService';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import { getConfiguredStorageDomain } from '../constants/avatar';
import type {
  UserProfile,
  UserProfileUpdate,
  UserStrategy,
  StrategyUpdate,
  UserPortfolio,
  PortfolioCreate,
  UserActivity,
  ActivityFilter,
  SyncResult,
  UserConfig,
  UserConfigUpdate,
  AvatarUploadResponse,
  AvatarInfo,
  AvatarUploadSettings,
  ApiResponse,
  PaginatedResponse,
} from '../types';

/**
 * 基础API客户端类
 */
class BaseApiClient {
  protected axiosInstance: AxiosInstance;
  protected baseURL: string;

  constructor(baseURL: string, config?: AxiosRequestConfig) {
    this.baseURL = baseURL.replace(/\/+$/, '');
    this.axiosInstance = axios.create({
      baseURL: this.baseURL,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
      ...config,
    });

    this.setupInterceptors();
  }

  /**
   * 设置拦截器
   */
  private setupInterceptors(): void {
    // 请求拦截器
    this.axiosInstance.interceptors.request.use(
      (config) => {
        // 添加认证Token（如果本地有）
        const token = localStorage.getItem('access_token');
        if (token) {
          config.headers.Authorization = `Bearer ${token}`;
        }

        // 添加请求ID用于追踪
        config.headers['X-Request-ID'] = this.generateRequestId();

        console.log(`[API Request] ${config.method?.toUpperCase()} ${config.url}`, config.data);
        return config;
      },
      (error) => {
        console.error('[API Request Error]', error);
        return Promise.reject(error);
      }
    );

    // 响应拦截器
    this.axiosInstance.interceptors.response.use(
      (response: AxiosResponse) => {
        console.log(`[API Response] ${response.config.url}`, response.data);
        const data = response.data as any;
        if (data && typeof data === 'object' && 'code' in data && 'data' in data) {
          response.data = data.data;
        }
        return response;
      },
      async (error) => {
        // 交由 authService 统一处理 401 Token 刷新与重试
        if (error.response?.status === 401) {
          return authService.handle401Error(error, this.axiosInstance);
        }

        console.error('[API Response Error]', error);
        return Promise.reject(this.handleError(error));
      }
    );
  }

  /**
   * 生成请求ID
   */
  private generateRequestId(): string {
    return `${Date.now()}-${Math.random().toString(36).substring(7)}`;
  }

  /**
   * 处理错误
   */
  private handleError(error: any): Error {
    if (error.response) {
      // 服务器响应错误
      const status = error.response.status;
      const data = error.response.data;
      const detail = data?.detail;
      let message =
        data?.error?.message ||
        data?.message ||
        (typeof detail === 'string' ? detail : '');

      if (!message && Array.isArray(detail) && detail.length > 0) {
        const first = detail[0];
        if (first?.msg) {
          const loc = Array.isArray(first.loc) ? first.loc.join('.') : '';
          message = loc ? `${loc}: ${first.msg}` : String(first.msg);
        }
      }

      if (!message) {
        message = error.message;
      }
      if (status === 401) return new Error('未登录或登录已过期，请重新登录');
      if (status === 404) return new Error('资源不存在或路径错误');
      return new Error(`API Error(${status}): ${message}`);
    } else if (error.request) {
      // 请求发出但没有收到响应
      return new Error('Network Error: No response from server');
    } else {
      // 其他错误
      return new Error(`Error: ${error.message}`);
    }
  }

  /**
   * GET请求
   */
  protected async get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.axiosInstance.get<T>(url, config);
    return response.data;
  }

  /**
   * POST请求
   */
  protected async post<T>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.axiosInstance.post<T>(url, data, config);
    return response.data;
  }

  /**
   * PUT请求
   */
  protected async put<T>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.axiosInstance.put<T>(url, data, config);
    return response.data;
  }

  /**
   * PATCH请求
   */
  protected async patch<T>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.axiosInstance.patch<T>(url, data, config);
    return response.data;
  }

  /**
   * DELETE请求
   */
  protected async delete<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.axiosInstance.delete<T>(url, config);
    return response.data;
  }
}

/**
 * 用户中心API服务类
 */
export class UserCenterService extends BaseApiClient {
  private normalizeAvatarUrl(rawUrl: string, fallbackFileKey = ''): string {
    const input = String(rawUrl || '').trim();
    const fileKey = String(fallbackFileKey || '').replace(/^\/+/, '');
    const buildFromKey = () => (fileKey ? `${getConfiguredStorageDomain()}/${fileKey}` : '');
    if (!input) return buildFromKey();

    const storageBase = getConfiguredStorageDomain();

    // 本地存储路径
    if (input.startsWith('/uploads/') || input.startsWith('/data/')) {
      const apiBase = (this.axiosInstance.defaults.baseURL || '').split('/api/v1')[0];
      if (apiBase) {
        return `${apiBase}${input}`;
      }
      return input;
    }

    // 仅给了 key/path 的情况，统一补存储域名
    if (!/^https?:\/\//i.test(input)) {
      const trimmed = input.replace(/^\/+/, '');
      if (trimmed) return `${storageBase}/${trimmed}`;
    }

    return input;
  }

  private mapProfileFromBackend(raw: any): UserProfile {
    const notificationDefaults = {
      email_notifications: true,
      push_notifications: true,
      strategy_alerts: true,
      portfolio_updates: true,
      system_announcements: true,
      marketing_emails: false,
    };
    const privacyDefaults = {
      profile_visibility: 'private' as const,
      show_email: false,
      show_phone: false,
      show_location: false,
      show_trading_stats: false,
      allow_messages: false,
    };

    return {
      id: Number(raw?.id || 0),
      user_id: String(raw?.user_id || ''),
      username: raw?.display_name || raw?.username || '',
      email: raw?.email || '',
      phone: raw?.phone,
      avatar: this.normalizeAvatarUrl(raw?.avatar_url || raw?.avatar),
      bio: raw?.bio,
      location: raw?.location,
      website: raw?.website,
      trading_experience: raw?.trading_experience || 'intermediate',
      risk_tolerance: raw?.risk_tolerance || 'medium',
      investment_goals: raw?.investment_goal || raw?.investment_goals,
      preferred_markets: Array.isArray(raw?.preferred_markets) ? raw.preferred_markets : [],
      notification_settings: raw?.notification_settings || notificationDefaults,
      privacy_settings: raw?.privacy_settings || privacyDefaults,
      created_at: raw?.created_at || new Date().toISOString(),
      updated_at: raw?.updated_at || raw?.created_at || new Date().toISOString(),
    };
  }

  private mapProfileUpdateToBackend(data: UserProfileUpdate): Record<string, any> {
    const payload: Record<string, any> = {};

    if (typeof data.username !== 'undefined') payload.display_name = data.username;
    if (typeof data.avatar !== 'undefined') payload.avatar_url = this.normalizeAvatarUrl(String(data.avatar || ''));
    if (typeof data.bio !== 'undefined') payload.bio = data.bio;
    if (typeof data.location !== 'undefined') payload.location = data.location;
    if (typeof data.website !== 'undefined') payload.website = data.website;
    if (typeof data.phone !== 'undefined') payload.phone = data.phone;
    if (typeof data.trading_experience !== 'undefined') payload.trading_experience = data.trading_experience;
    if (typeof data.risk_tolerance !== 'undefined') payload.risk_tolerance = data.risk_tolerance;
    if (typeof data.investment_goals !== 'undefined') payload.investment_goal = data.investment_goals;

    return payload;
  }

  constructor() {
    const envBase =
      import.meta.env.VITE_USER_CENTER_API_URL ||
      import.meta.env.VITE_USER_API_URL ||
      SERVICE_ENDPOINTS.USER_SERVICE;

    const normalizedBaseURL = envBase.replace(/\/+$/, '');
    const baseWithApiPrefix = normalizedBaseURL.endsWith('/api/v1')
      ? normalizedBaseURL
      : `${normalizedBaseURL}/api/v1`;

    super(baseWithApiPrefix);

    if (import.meta.env.MODE === 'development') {
      console.info('[UserCenterService] baseURL =', baseWithApiPrefix);
    }
    this.axiosInstance.interceptors.response.use(
      (resp) => resp,
      async (error) => {
        const isNetwork = !error.response;
        if (isNetwork) {
          return Promise.reject(new Error('Network Error: No response from server'));
        }
        return Promise.reject(error);
      }
    );
  }

  // ============ 用户档案管理 ============

  /**
   * 获取用户档案
   */
  async getUserProfile(_userId: string): Promise<UserProfile> {
    const raw = await this.get<any>(`/profiles/me/profile`);
    return this.mapProfileFromBackend(raw);
  }

  /**
   * 更新用户档案
   */
  async updateUserProfile(_userId: string, data: UserProfileUpdate): Promise<UserProfile> {
    const payload = this.mapProfileUpdateToBackend(data);
    const raw = await this.put<any>(`/profiles/me/profile`, payload);
    return this.mapProfileFromBackend(raw);
  }

  async changePassword(oldPassword: string, newPassword: string): Promise<void> {
    await this.post(`/auth/change-password`, { old_password: oldPassword, new_password: newPassword });
  }

  // ============ 策略管理 ============

  /**
   * 获取用户策略列表（带分页）
   */
  async getUserStrategies(
    _userId: string, // Keep arg for compatibility but ignore it
    params?: {
      page?: number;
      page_size?: number;
      status?: string;
    }
  ): Promise<PaginatedResponse<UserStrategy>> {
    const page = params?.page || 1;
    const pageSize = params?.page_size || 20;

    // 后端 StrategyListResponse 格式: { total: number, strategies: [...] }
    // 不传 skip/limit，后端目前不支持分页参数
    const raw = await this.get<any>(`/strategies`);

    // 适配后端响应格式 → strategiesSlice 期望的 PaginatedResponse 格式
    const strategies: UserStrategy[] = Array.isArray(raw?.strategies)
      ? raw.strategies.map((s: any) => this.mapStrategyFromBackend(s))
      : Array.isArray(raw?.items)
        ? raw.items.map((s: any) => this.mapStrategyFromBackend(s))
        : Array.isArray(raw?.list)
          ? raw.list.map((s: any) => this.mapStrategyFromBackend(s))
        : Array.isArray(raw)
          ? raw.map((s: any) => this.mapStrategyFromBackend(s))
          : [];

    const total = raw?.total ?? strategies.length;
    return {
      items: strategies,
      total,
      page,
      page_size: pageSize,
      total_pages: Math.ceil(total / pageSize),
    };
  }

  /**
   * 将后端策略格式映射为前端 UserStrategy 格式
   */
  private mapStrategyFromBackend(s: any): UserStrategy {
    const normalizedStatus = String(s?.status || 'draft').toLowerCase();
    const strategyName = s?.name || s?.strategy_name || '未命名策略';
    const emptyPerf = {
      total_return: 0,
      total_return_pct: 0,
      sharpe_ratio: 0,
      max_drawdown: 0,
      win_rate: 0,
      profit_factor: 0,
      avg_trade_duration: 0,
      total_trades: 0,
    };
    return {
      id: String(s?.id ?? ''),
      user_id: String(s?.user_id ?? ''),
      strategy_id: String(s?.strategy_id ?? s?.id ?? ''),
      name: strategyName,
      strategy_type: s?.strategy_type || 'quantitative',
      status: normalizedStatus as UserStrategy['status'],
      is_favorite: s?.is_favorite ?? false,
      performance_summary: s?.performance_summary ?? emptyPerf,
      last_backtest_id: s?.last_backtest_id,
      last_backtest_date: s?.last_backtest_date,
      notes: s?.notes,
      tags: Array.isArray(s?.tags) ? s.tags : [],
      code: s?.code,
      cos_url: s?.cos_url,
      code_hash: s?.code_hash,
      created_at: s?.created_at || new Date().toISOString(),
      updated_at: s?.updated_at || new Date().toISOString(),
    };
  }

  /**
   * 获取单个策略详情
   */
  async getStrategyDetail(_userId: string, strategyId: string): Promise<UserStrategy> {
    return this.get<UserStrategy>(`/strategies/${strategyId}`);
  }

  /**
   * 创建策略
   */
  async createStrategy(
    _userId: string,
    data: Omit<UserStrategy, 'id' | 'created_at' | 'updated_at'>
  ): Promise<UserStrategy> {
    return this.post<UserStrategy>(`/strategies`, data);
  }

  /**
   * 更新策略
   */
  async updateStrategy(
    _userId: string,
    strategyId: string,
    data: StrategyUpdate
  ): Promise<UserStrategy> {
    return this.put<UserStrategy>(`/strategies/${strategyId}`, data);
  }

  /**
   * 删除策略
   */
  async deleteStrategy(_userId: string, strategyId: string): Promise<{ message: string }> {
    return this.delete<{ message: string }>(`/strategies/${strategyId}`);
  }

  /**
   * 从社区导入策略
   */
  async importStrategyFromCommunity(data: {
    strategy_name: string;
    strategy_type: string;
    description?: string;
    config?: Record<string, any>;
    tags?: string[];
    notes?: string;
    source: 'community';
    source_post_id: number;
    performance_summary?: any;
  }): Promise<UserStrategy> {
    return this.post<UserStrategy>(`/strategies/import`, data);
  }

  /**
   * 管理用户策略（添加/删除/收藏/归档/启用/禁用）
   */
  async manageUserStrategy(
    _userId: string,
    strategyId: string,
    action: 'add' | 'remove' | 'favorite' | 'unfavorite' | 'archive' | 'enable' | 'disable'
  ): Promise<UserStrategy> {
    if (action === 'favorite' || action === 'unfavorite') {
      return this.post<UserStrategy>(`/strategies/${strategyId}/like`, {});
    }
    if (action === 'archive') {
      return this.updateStrategy(_userId, strategyId, { status: 'archived' });
    }
    if (action === 'enable') {
      return this.updateStrategy(_userId, strategyId, { status: 'active' });
    }
    if (action === 'disable') {
      return this.updateStrategy(_userId, strategyId, { status: 'paused' });
    }
    throw new Error(`不支持的策略操作: ${action}`);
  }

  /**
   * 同步用户策略
   */
  async syncUserStrategies(userId: string): Promise<SyncResult> {
    return this.post<SyncResult>(`/sync/${userId}/strategies`);
  }

  // ============ 投资组合管理 ============

  /**
   * 获取用户投资组合列表
   */
  async getUserPortfolios(userId: string): Promise<UserPortfolio[]> {
    return this.get<UserPortfolio[]>(`/portfolios/${userId}`);
  }

  /**
   * 创建投资组合
   */
  async createPortfolio(userId: string, data: PortfolioCreate): Promise<UserPortfolio> {
    return this.post<UserPortfolio>(`/portfolios/${userId}`, data);
  }

  /**
   * 获取单个投资组合详情
   */
  async getPortfolioDetail(portfolioId: number): Promise<UserPortfolio> {
    return this.get<UserPortfolio>(`/portfolios/detail/${portfolioId}`);
  }

  // ============ 用户活动追踪 ============

  /**
   * 获取用户活动记录
   */
  async getUserActivities(
    userId: string,
    filter?: ActivityFilter
  ): Promise<{ activities: UserActivity[]; total_count: number }> {
    return this.get<{ activities: UserActivity[]; total_count: number }>(
      `/activities/${userId}`,
      { params: filter }
    );
  }

  /**
   * 记录用户活动（由前端主动记录）
   */
  async recordActivity(
    userId: string,
    activityType: string,
    activityData: Record<string, any>
  ): Promise<void> {
    return this.post<void>(`/activities/${userId}/record`, {
      activity_type: activityType,
      activity_data: activityData,
    });
  }

  // ============ 用户配置管理 ============

  /**
   * 获取用户配置
   */
  async getUserConfig(userId: string): Promise<UserConfig> {
    return this.get<UserConfig>(`/config/${userId}`);
  }

  /**
   * 更新用户配置
   */
  async updateUserConfig(userId: string, data: UserConfigUpdate): Promise<UserConfig> {
    return this.put<UserConfig>(`/config/${userId}`, data);
  }

  // ============ 头像管理 ============

  /**
   * 上传用户头像
   * 采用腾讯云COS方案：
   * 1. 上传文件到COS（通过网关/files/upload接口）
   * 2. 获取COS URL后更新用户档案
   */
  async uploadAvatar(
    userId: string,
    file: File,
    onProgress?: (percent: number) => void
  ): Promise<AvatarUploadResponse> {
    // 1. 上传文件到COS（统一走网关 files 上传接口）
    const formData = new FormData();
    formData.append('file', file);
    formData.append('user_id', userId);
    formData.append('category', 'image');
    formData.append('description', 'user-avatar');

    const token = localStorage.getItem('access_token');
    const candidateUploadUrls = Array.from(
      new Set([
        `${SERVICE_ENDPOINTS.API_GATEWAY}/files/upload`,
        `${this.baseURL}/files/upload`,
      ])
    );

    let gatewayResp: any = null;
    let lastErr: any = null;
    for (const url of candidateUploadUrls) {
      try {
        gatewayResp = await axios.post(url, formData, {
          timeout: 30000,
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          onUploadProgress: (progressEvent) => {
            if (onProgress && progressEvent.total) {
              const percent = Math.round((progressEvent.loaded * 100) / progressEvent.total);
              onProgress(percent);
            }
          },
        });
        lastErr = null;
        break;
      } catch (err: any) {
        lastErr = err;
        const status = err?.response?.status;
        if (status === 404) {
          continue;
        }
        throw err;
      }
    }
    if (!gatewayResp) {
      const status = lastErr?.response?.status;
      const requestUrl = lastErr?.config?.url;
      throw new Error(`头像上传失败：上传接口不可用（status=${status || 'unknown'} url=${requestUrl || 'unknown'}）`);
    }

    const rawUploadResp = gatewayResp?.data || {};
    const hasCode = typeof rawUploadResp?.code !== 'undefined';
    if (hasCode) {
      const codeNum = Number(rawUploadResp.code);
      if (!Number.isNaN(codeNum) && codeNum !== 0 && codeNum !== 200) {
        throw new Error(`头像上传失败：${rawUploadResp.message || `后端返回错误码 ${codeNum}`}`);
      }
    }
    const uploadPayload = rawUploadResp?.data || rawUploadResp || {};
    const fileKey = String(
      uploadPayload.file_key || uploadPayload.file_id || uploadPayload.key || ''
    ).replace(/^\/+/, '');
    let avatarUrl = this.normalizeAvatarUrl(
      uploadPayload.avatar_url || uploadPayload.file_url || uploadPayload.url || '',
      fileKey
    );

    // 兜底：若上游只返回了域名，使用 file_key 补齐完整路径
    if (fileKey) {
      if (!avatarUrl) {
        avatarUrl = `${getConfiguredStorageDomain()}/${fileKey}`;
      } else {
        try {
          const parsed = new URL(avatarUrl);
          if (!parsed.pathname || parsed.pathname === '/') {
            avatarUrl = `${getConfiguredStorageDomain()}/${fileKey}`;
          }
        } catch {
          avatarUrl = `${getConfiguredStorageDomain()}/${fileKey}`;
        }
      }
    }

    if (!avatarUrl) {
      throw new Error('头像上传失败：未获取到文件URL');
    }

    // 2. 更新用户档案中的 avatar_url（后端字段）
    await this.updateUserProfile(userId, {
      avatar: avatarUrl
    });

    const result: AvatarUploadResponse = {
      success: true,
      avatar_url: avatarUrl,
      file_key: fileKey,
      file_size: Number(uploadPayload.file_size || file.size || 0),
      upload_time: uploadPayload.upload_time || new Date().toISOString(),
      message: uploadPayload.message,
    };

    return result;
  }

  /**
   * 删除用户头像
   */
  async deleteAvatar(userId: string, fileKey: string): Promise<{ message: string }> {
    // 1. 删除COS文件（网关 delete 需要 body: { file_key, user_id }）
    const token = localStorage.getItem('access_token');
    const candidateDeleteUrls = Array.from(
      new Set([
        `${SERVICE_ENDPOINTS.API_GATEWAY}/files/delete`,
        `${this.baseURL}/files/delete`,
      ])
    );
    let deleteResult: any = null;
    let lastErr: any = null;
    for (const url of candidateDeleteUrls) {
      try {
        deleteResult = await axios.delete(url, {
          data: { file_key: fileKey, user_id: userId },
          timeout: 30000,
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        lastErr = null;
        break;
      } catch (err: any) {
        lastErr = err;
        const status = err?.response?.status;
        if (status === 404) {
          continue;
        }
        throw err;
      }
    }
    if (!deleteResult) {
      const status = lastErr?.response?.status;
      const requestUrl = lastErr?.config?.url;
      throw new Error(`头像删除失败：删除接口不可用（status=${status || 'unknown'} url=${requestUrl || 'unknown'}）`);
    }

    // 2. 清空用户档案中的 avatar
    await this.updateUserProfile(userId, {
      avatar: '' // 设为空字符串以清除
    });

    return { message: deleteResult?.data?.message || '头像删除成功' };
  }

  /**
   * 获取头像信息
   */
  async getAvatarInfo(userId: string): Promise<AvatarInfo> {
    return this.get<AvatarInfo>(`/avatar/info/${userId}`);
  }

  /**
   * 获取头像上传设置
   */
  async getAvatarUploadSettings(): Promise<AvatarUploadSettings> {
    return this.get<AvatarUploadSettings>('/avatar/settings');
  }

  // ============ 设备管理 ============

  /**
   * 获取设备列表
   */
  async getDevices(): Promise<any[]> {
    return this.get<any[]>('/devices');
  }

  /**
   * 移除设备
   */
  async revokeDevice(deviceId: string): Promise<{ message: string }> {
    return this.delete<{ message: string }>(`/devices/${deviceId}`);
  }

  // ============ 审计日志 ============

  /**
   * 获取审计日志
   */
  async getAuditLogs(params: { page: number; pageSize: number }): Promise<{ logs: any[]; total: number }> {
    const response = await this.get<any>('/audit/my-logs', {
      params: {
        limit: params.pageSize,
        offset: (params.page - 1) * params.pageSize,
      }
    });

    // 兼容后端返回格式
    if (response && typeof response === 'object') {
      // 如果后端直接返回 { logs: [], total: ... }
      if (Array.isArray(response.logs)) {
        return response;
      }
      // 如果后端返回 { code: 200, data: { logs: ... } }，BaseApiClient 拦截器已经解包了 data
      // 所以这里应该是 { logs: ..., total: ... }
    }

    return { logs: [], total: 0 };
  }


  // ============ LLM API Key 管理 ============

  /**
   * 获取 LLM 配置状态
   */
  async getLLMConfig(): Promise<{ has_key: boolean; masked_key: string }> {
    const response = await this.get<any>('/ai-ide/config/llm');
    return {
      has_key: response?.has_key || false,
      masked_key: response?.masked_key || '',
    };
  }

  /**
   * 保存 LLM API Key
   */
  async saveLLMConfig(apiKey: string): Promise<{ success: boolean; message?: string }> {
    return this.post('/ai-ide/config/llm', { qwen_api_key: apiKey });
  }


  // ============ 健康检查 ============

  /**
   * 健康检查
   */
  async healthCheck(): Promise<{ status: string; timestamp: string }> {
    return this.get<{ status: string; timestamp: string }>('/health');
  }
}

// 导出单例实例
export const userCenterService = new UserCenterService();

// 导出类型
export type { BaseApiClient };
