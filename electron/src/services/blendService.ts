/**
 * 组合选股 API 客户端（`/api/v1/models/blends`）。
 *
 * 为什么不塞进 `modelTrainingService.ts`：那个文件已 1200+ 行，组合选股是
 * 一套独立领域（榜单/滚动/纸面计划/证据），分开放才能单独演进。
 *
 * 三个硬约束：
 * 1. 只读纸面台账——本服务**没有任何下单/委托方法**；
 * 2. 证据包只读 `/evidence`，前端不现算、不编数；
 * 3. 错误文案统一走 `blendErrorMessage`：后端 `detail={code,message}` 要抬 message，
 *    不能把整个对象 `String()` 成 `[object Object]`。
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';
import type {
  BlendBoundsResponse,
  BlendCreateRequest,
  BlendDatesResponse,
  BlendEvidenceResponse,
  BlendPatchRequest,
  BlendPeriodQuery,
  BlendPeriodResponse,
  BlendPlan,
  BlendPlanRequest,
  BlendPreviewRequest,
  BlendRankingResponse,
  BlendRecord,
  BlendRollingResponse,
  ComboAutoCandidatesResponse,
  ComboEligibleResponse,
} from './blendTypes';

const BASE_PATH = '/models/blends';

/** 计划冷启动实测 ~8s（含行情/成交额读取），给足余量；轮询类请求仍用 30s */
const PLAN_TIMEOUT_MS = 60_000;
const ROLLING_TIMEOUT_MS = 45_000;
/** 周期收益冷启动实测 ~6s（32 评分日）；300 交易日上限按 kill 判据留足余量 */
const PERIOD_TIMEOUT_MS = 90_000;

class BlendService {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      timeout: 30000,
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

  // ── 组合 CRUD ────────────────────────────────────────────────────────

  async listBlends(includeArchived = false): Promise<BlendRecord[]> {
    const resp = await this.client.get<{ items: BlendRecord[] }>(BASE_PATH, {
      params: includeArchived ? { include_archived: true } : undefined,
    });
    return resp.data.items || [];
  }

  async createBlend(body: BlendCreateRequest): Promise<BlendRecord> {
    const resp = await this.client.post<BlendRecord>(BASE_PATH, body);
    return resp.data;
  }

  async patchBlend(blendId: string, body: BlendPatchRequest): Promise<BlendRecord> {
    const resp = await this.client.patch<BlendRecord>(`${BASE_PATH}/${blendId}`, body);
    return resp.data;
  }

  async archiveBlend(blendId: string): Promise<void> {
    await this.client.delete(`${BASE_PATH}/${blendId}`);
  }

  // ── 候选模型 / 模板 ─────────────────────────────────────────────────

  async getEligible(market = 'CN'): Promise<ComboEligibleResponse> {
    const resp = await this.client.get<ComboEligibleResponse>(`${BASE_PATH}/eligible`, {
      params: { market },
    });
    return resp.data;
  }

  async getAutoCandidates(market = 'CN'): Promise<ComboAutoCandidatesResponse> {
    const resp = await this.client.get<ComboAutoCandidatesResponse>(
      `${BASE_PATH}/auto-candidates`,
      { params: { market } },
    );
    return resp.data;
  }

  /** 未保存组合的当日榜单（构建器预览） */
  async preview(body: BlendPreviewRequest): Promise<BlendRankingResponse> {
    const resp = await this.client.post<BlendRankingResponse>(`${BASE_PATH}/preview`, body);
    return resp.data;
  }

  // ── 榜单 / 滚动 / 日期 ──────────────────────────────────────────────

  async getDates(blendId: string, limit = 60): Promise<BlendDatesResponse> {
    const resp = await this.client.get<BlendDatesResponse>(
      `${BASE_PATH}/${blendId}/dates`,
      { params: { limit } },
    );
    return resp.data;
  }

  async getRanking(
    blendId: string,
    params: { date?: string; top?: number; excludeBlacklist?: boolean } = {},
  ): Promise<BlendRankingResponse> {
    const resp = await this.client.get<BlendRankingResponse>(
      `${BASE_PATH}/${blendId}/ranking`,
      {
        params: {
          date: params.date,
          top: params.top ?? 500,
          ...(params.excludeBlacklist ? { exclude_blacklist: true } : {}),
        },
      },
    );
    return resp.data;
  }

  /** 历史数据端口：pred 测试集优先，整组合不可用才降级 runs */
  async getBounds(blendId: string): Promise<BlendBoundsResponse> {
    const resp = await this.client.get<BlendBoundsResponse>(
      `${BASE_PATH}/${blendId}/bounds`,
    );
    return resp.data;
  }

  async getRolling(
    blendId: string,
    params: { date?: string; window?: number; per_day?: number; capital?: number } = {},
  ): Promise<BlendRollingResponse> {
    const resp = await this.client.get<BlendRollingResponse>(
      `${BASE_PATH}/${blendId}/rolling`,
      {
        params: {
          date: params.date,
          window: params.window ?? 10,
          per_day: params.per_day ?? 5,
          capital: params.capital,
        },
        timeout: ROLLING_TIMEOUT_MS,
      },
    );
    return resp.data;
  }

  /**
   * 周期收益：固定持有期滚动桶（收盘价建仓/退出，无手续费与滑点）。
   * 只读研究计算，不产生任何委托。
   */
  async getPeriod(
    blendId: string,
    params: BlendPeriodQuery = {},
  ): Promise<BlendPeriodResponse> {
    const resp = await this.client.get<BlendPeriodResponse>(
      `${BASE_PATH}/${blendId}/period`,
      {
        params: {
          start: params.start,
          end: params.end,
          capital: params.capital,
          positions: params.positions,
        },
        timeout: PERIOD_TIMEOUT_MS,
      },
    );
    return resp.data;
  }

  // ── 纸面调仓计划 ────────────────────────────────────────────────────

  async createPlan(blendId: string, body: BlendPlanRequest = {}): Promise<BlendPlan> {
    const resp = await this.client.post<BlendPlan>(
      `${BASE_PATH}/${blendId}/plans`,
      body,
      { timeout: PLAN_TIMEOUT_MS },
    );
    return resp.data;
  }

  async listPlans(
    blendId: string,
    params: { date?: string; limit?: number } = {},
  ): Promise<{ items: BlendPlan[]; recomputed?: boolean }> {
    const resp = await this.client.get<{ items: BlendPlan[]; recomputed?: boolean }>(
      `${BASE_PATH}/${blendId}/plans`,
      { params: { date: params.date, limit: params.limit ?? 20 }, timeout: PLAN_TIMEOUT_MS },
    );
    return resp.data;
  }

  /** 确认纸面计划：**只写本地纸面台账**，不产生任何委托 */
  async confirmPlan(blendId: string, planId: string): Promise<BlendPlan> {
    const resp = await this.client.post<BlendPlan>(
      `${BASE_PATH}/${blendId}/plans/${planId}/confirm`,
    );
    return resp.data;
  }

  // ── 证据包 ──────────────────────────────────────────────────────────

  async getEvidence(): Promise<BlendEvidenceResponse> {
    const resp = await this.client.get<BlendEvidenceResponse>(`${BASE_PATH}/evidence`);
    return resp.data;
  }
}

/**
 * 后端错误 → 可展示文案。
 *
 * `detail` 可能是字符串、`{code,message}`（本项目 BlendError 的统一形状）或
 * FastAPI 校验错误数组（422）。三种都要读得懂，绝不 `String(detail)`。
 */
export function blendErrorMessage(error: unknown): string {
  const err = error as {
    response?: { status?: number; data?: { detail?: unknown } };
    message?: string;
  };
  const detail = err?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) {
    const text = detail.trim();
    // 旧后端没挂组合路由时，/models/blends/* 会被 model_training 的
    // `GET /{model_id}` 兜底捕获并回英文 `Model not found`。
    // 这不是「模型不存在」，而是「后端版本落后」——必须给能动手的文案。
    if (
      err?.response?.status === 404 &&
      text.toLowerCase() === 'model not found'
    ) {
      return '组合排名接口未就绪：当前后端为旧版本，请更新后端后重试';
    }
    return text;
  }
  if (detail && typeof detail === 'object') {
    const obj = detail as { message?: unknown; code?: unknown };
    if (typeof obj.message === 'string' && obj.message.trim()) return obj.message;
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: unknown } | undefined;
      if (first && typeof first.msg === 'string') return first.msg;
    }
  }
  if (typeof err?.message === 'string' && err.message.trim()) return err.message;
  return '请求失败，请稍后重试';
}

export const blendService = new BlendService();
