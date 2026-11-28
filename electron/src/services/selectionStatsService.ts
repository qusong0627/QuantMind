/**
 * 截面选股 · 单模型 TopK 历史统计客户端。
 *
 * 与 `blendService` 同一条鉴权/租户/401 链路；错误文案统一走
 * `selectionStatsErrorMessage`（后端 `detail={code,message}` 要抬 message，
 * 不能把整个对象 `String()` 成 `[object Object]`）。
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';
import type {
  SelectionStatsQuery,
  SelectionStatsResponse,
} from './selectionStatsTypes';

const BASE_PATH = '/models';
/** 全区间全市场统计实测 2~3s；留足慢盘与首次读 parquet 的余量 */
const TIMEOUT_MS = 90_000;

class SelectionStatsService {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      timeout: TIMEOUT_MS,
      headers: { 'Content-Type': 'application/json' },
    });

    this.client.interceptors.request.use((config) => {
      config.baseURL = resolveWebSafeServiceBase(
        (import.meta as any).env?.VITE_USER_API_URL,
        SERVICE_ENDPOINTS.USER_SERVICE,
      );
      return config;
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
      async (error) => authService.handle401Error(error, this.client),
    );
  }

  async fetch(
    modelId: string,
    query: SelectionStatsQuery = {},
  ): Promise<SelectionStatsResponse> {
    const params: Record<string, string | number> = {};
    if (query.start) params.start = query.start;
    if (query.end) params.end = query.end;
    if (typeof query.top_k === 'number' && Number.isFinite(query.top_k)) {
      params.top_k = Math.round(query.top_k);
    }
    if (typeof query.hold_days === 'number' && Number.isFinite(query.hold_days)) {
      params.hold_days = Math.round(query.hold_days);
    }
    if (typeof query.capital === 'number' && Number.isFinite(query.capital)) {
      params.capital = query.capital;
    }
    if (query.exclude_blacklist) params.exclude_blacklist = 'true';

    const resp = await this.client.get<SelectionStatsResponse>(
      `${BASE_PATH}/${encodeURIComponent(modelId)}/selection-stats`,
      { params },
    );
    return resp.data;
  }
}

export const selectionStatsService = new SelectionStatsService();

/** 后端业务码 → 用户能动手的文案；`detail` 为字符串时原样展示。 */
export function selectionStatsErrorMessage(error: unknown): string {
  const err = error as {
    response?: { status?: number; data?: { detail?: unknown } };
    message?: string;
  };
  const detail = err?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) {
    const text = detail.trim();
    // 旧后端没有这条路由时，`/models/{id}/selection-stats` 不匹配任何
    // 两段路径（model_training 的兜底只覆盖单段），回落成默认 404。
    if (err?.response?.status === 404 && text.toLowerCase() === 'not found') {
      return '历史统计接口未就绪：当前后端为旧版本，请更新后端后重试';
    }
    return text;
  }
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message.trim();
  }
  if (err?.response?.status === 404) {
    return '历史统计接口未就绪：当前后端为旧版本，请更新后端后重试';
  }
  if (err?.message) return err.message;
  return '历史统计计算失败，请稍后重试';
}
