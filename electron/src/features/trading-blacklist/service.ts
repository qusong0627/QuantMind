/**
 * 交易黑名单 API（个人中心「交易黑名单」表格）。
 *
 * 后端信封是 `{success, data}`，这里统一拆包只回 `data`——调用方拿到的是行数组
 * 而不是 `resp.data.data`，否则每个调用点都要重复一次信封知识，改一次漏一处。
 * 失败一律抛 Error（带后端 detail），由面板显示原文：这些错误全是输入问题
 * （代码不认、日期不合法），吞成「保存失败」会让用户反复重试同一个错误输入。
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../../config/services';
import { authService } from '../auth/services/authService';
import type {
  ActionFilter,
  BlacklistListResponse,
  BlacklistMetaResponse,
  BlacklistAction,
} from './types';

const TIMEOUT_MS = 20000;

export interface ListBlacklistParams {
  q?: string;
  action?: ActionFilter;
  page?: number;
  pageSize?: number;
  includeExpired?: boolean;
}

export interface UpsertBlacklistParams {
  symbol: string;
  action: BlacklistAction;
  reason?: string;
  note?: string;
  expire?: string | null;
}

function detailOf(err: unknown): string {
  const resp = (err as { response?: { data?: { detail?: unknown } } })?.response;
  const detail = resp?.data?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    // FastAPI 的 422 校验错误是数组，取第一条的 msg（拼 [object Object] 等于没说）
    const first = detail[0] as { msg?: string };
    return first?.msg || '请求参数不合法';
  }
  return (err as Error)?.message || '请求失败';
}

export class TradingBlacklistService {
  protected get client(): AxiosInstance {
    const baseURL = resolveWebSafeServiceBase(
      (import.meta as any).env?.VITE_USER_API_URL,
      SERVICE_ENDPOINTS.API_GATEWAY,
    );
    const client = axios.create({ baseURL, timeout: TIMEOUT_MS });
    client.interceptors.request.use((config) => {
      const token = authService.getAccessToken();
      if (token) {
        if (config.headers && typeof (config.headers as any).set === 'function') {
          (config.headers as any).set('Authorization', `Bearer ${token}`);
        } else if (config.headers) {
          (config.headers as any)['Authorization'] = `Bearer ${token}`;
        }
      }
      return config;
    });
    return client;
  }

  async list(params: ListBlacklistParams = {}): Promise<BlacklistListResponse> {
    try {
      const resp = await this.client.get('/exclusion/entries', {
        params: {
          q: params.q || undefined,
          action: params.action && params.action !== 'all' ? params.action : undefined,
          page: params.page ?? 1,
          page_size: params.pageSize ?? 50,
          include_expired: params.includeExpired ?? true,
        },
      });
      return resp.data.data as BlacklistListResponse;
    } catch (err) {
      throw new Error(detailOf(err));
    }
  }

  async meta(): Promise<BlacklistMetaResponse> {
    try {
      const resp = await this.client.get('/exclusion/meta');
      return resp.data.data as BlacklistMetaResponse;
    } catch (err) {
      throw new Error(detailOf(err));
    }
  }

  async upsert(params: UpsertBlacklistParams): Promise<void> {
    try {
      await this.client.post('/exclusion/entries', {
        symbol: params.symbol,
        action: params.action,
        reason: params.reason ?? '',
        note: params.note ?? '',
        expire: params.expire || null,
      });
    } catch (err) {
      throw new Error(detailOf(err));
    }
  }

  /** 撤销一条本人改动。返回 false 表示本来就没有（连点两次删除是正常操作）。 */
  async remove(symbol: string): Promise<boolean> {
    try {
      const resp = await this.client.delete(`/exclusion/entries/${encodeURIComponent(symbol)}`);
      return Boolean(resp.data?.data?.removed);
    } catch (err) {
      throw new Error(detailOf(err));
    }
  }
}

export const tradingBlacklistService = new TradingBlacklistService();
