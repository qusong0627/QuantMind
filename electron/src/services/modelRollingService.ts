/**
 * 模型管理「滚动训练」服务（P1 · 设计文档《滚动训练与模型生命周期》）。
 *
 * 与后端 `/api/v1/models/rolling/*` 一一对应（JWT 用户态，前端绝不持有内部密钥）：
 * - 配方摘要（内建 + 从模型派生的用户配方）
 * - 滚动台账（qm_rolling_campaigns）
 * - 重训调度配置（读写，PUT 校验与内部端点同口径）
 * - 手动派发（trigger 服务端定死 manual；低内存 409 拒绝）
 * - 从模型派生配方（云端导入模型同样可用：读模型目录 metadata.json + config.yaml）
 *
 * 错误语义：404 配方/模型不存在；400 校验不过（越界、信息不足、市场错配）；
 * 409 内存不足。面板直接展示 detail 原文——后端文案已是给用户看的。
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';

export type RecipeSource = 'builtin' | 'user';

export interface RollingWindowPolicy {
  train_days: number;
  valid_days: number;
  test_days: number;
  mode: 'sliding' | 'expanding';
  purge_days: number | null;
}

export interface RollingRecipeSummary {
  recipe_id: string;
  valid: boolean;
  source?: RecipeSource;
  market?: string;
  calendar_market?: string;
  factor_market?: string;
  factor_source?: string;
  target_horizon_days?: number;
  window_policy?: RollingWindowPolicy;
  recipe_hash?: string;
  description?: string;
  source_model_id?: string | null;
  derived_at?: string | null;
  error?: string;
}

export type CampaignStatus = 'planned' | 'dispatched' | 'registered' | 'failed' | 'skipped';

export interface RollingCampaign {
  campaign_id: string;
  market: string;
  recipe_id: string;
  recipe_hash: string;
  trigger: string;
  status: CampaignStatus;
  window_index: number;
  anchor_date: string;
  purge_days: number;
  window_plan?: Record<string, unknown> | null;
  run_id: string | null;
  model_id: string | null;
  attempts: number;
  detail?: Record<string, unknown> | null;
  created_at?: string;
  updated_at?: string;
  dispatched_at?: string | null;
  finished_at?: string | null;
}

export interface RetrainSchedule {
  enabled: boolean;
  day_rule: string;
  time: string;
  recipe_id: string;
  window_policy: null;
  purge_days: null;
  observation_days: number;
  max_time_minutes: number;
  executor: 'local';
  last_run: string | null;
}

/** 派发器心跳（后端 scheduler_registry.read_heartbeats 判定，与体检 C07 同口径） */
export interface SchedulerHeartbeat {
  key: string;
  name: string;
  enabled: boolean;
  state: 'ok' | 'stale' | 'off' | 'missing';
  age: number | null;
  ttl: number | null;
}

export interface ScheduleUpdateBody {
  enabled?: boolean;
  day_rule?: string;
  time?: string;
  recipe_id: string;
  observation_days?: number;
  max_time_minutes?: number;
  executor?: 'local' | 'remote';
  window_policy?: null;
  purge_days?: null;
}

export interface DispatchBody {
  market: string;
  recipe_id: string;
  dry_run?: boolean;
  anchor_date?: string | null;
}

export interface DispatchPlan {
  anchor_date: string;
  mode: string;
  purge_days: number;
  window_index: number;
  train: [string, string];
  valid: [string, string];
  test: [string, string];
}

export interface DispatchResult {
  status: 'dispatched' | 'duplicate' | 'skipped' | 'dry_run';
  ready?: boolean;
  reason?: string;
  message?: string;
  campaign_id?: string;
  campaign_status?: string;
  run_id?: string | null;
  market?: string;
  recipe_id?: string;
  anchor_date?: string;
  window_index?: number;
  attempts?: number;
  factor_min_date?: string;
  factor_max_date?: string;
  detail?: Record<string, unknown>;
  plan?: DispatchPlan;
}

export interface DeriveResult {
  status: 'preview' | 'saved' | 'unchanged';
  recipe_id: string;
  recipe_hash: string;
  market: string;
  factor_market: string;
  factor_source: string;
  feature_count: number;
  target_horizon_days: number;
  window_policy: RollingWindowPolicy;
  description: string;
  source_model_id: string;
  derived_at: string;
  source_files: string[];
  warnings: string[];
  path: string | null;
}

/** 后端 400/404/409 detail 已是给用户看的中文文案，原样透出；422 为 pydantic 校验列表，取首条 */
export function rollingErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { loc?: unknown[]; msg?: unknown } | undefined;
      const msg = typeof first?.msg === 'string' ? first.msg : '';
      const loc = Array.isArray(first?.loc)
        ? first.loc.filter(p => typeof p === 'string' || typeof p === 'number').join('.')
        : '';
      if (msg) return loc ? `参数校验失败（${loc}）：${msg}` : `参数校验失败：${msg}`;
      return '参数校验失败（后端 422）';
    }
    if (error.response?.status === 404) return '目标不存在（可能已被清理）';
    return `请求失败（HTTP ${error.response?.status ?? '—'}）`;
  }
  if (error instanceof Error) return error.message;
  return '未知错误';
}

class ModelRollingService {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      // 派发是同步记账（不等待训练完成），但就绪探测/提交秒级；给 60s 余量
      timeout: 60000,
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

  async listRecipes(): Promise<RollingRecipeSummary[]> {
    const resp = await this.client.get<{ recipes: RollingRecipeSummary[]; count: number }>(
      '/models/rolling/recipes',
    );
    return resp.data.recipes ?? [];
  }

  async listCampaigns(params?: {
    market?: string;
    status?: string;
    limit?: number;
  }): Promise<RollingCampaign[]> {
    const query: Record<string, string | number> = { limit: params?.limit ?? 50 };
    if (params?.market) query.market = params.market;
    if (params?.status) query.status = params.status;
    const resp = await this.client.get<{ campaigns: RollingCampaign[]; count: number }>(
      '/models/rolling/campaigns',
      { params: query },
    );
    return resp.data.campaigns ?? [];
  }

  async getSchedules(): Promise<{
    schedules: Record<string, RetrainSchedule>;
    markets: string[];
    dispatch: SchedulerHeartbeat | null;
  }> {
    const resp = await this.client.get<{
      schedules: Record<string, RetrainSchedule>;
      markets: string[];
      dispatch: SchedulerHeartbeat | null;
    }>('/models/rolling/schedule');
    return resp.data;
  }

  async saveSchedule(
    market: string,
    body: ScheduleUpdateBody,
  ): Promise<{ market: string; schedule: RetrainSchedule }> {
    const resp = await this.client.put<{ market: string; schedule: RetrainSchedule }>(
      `/models/rolling/schedule/${encodeURIComponent(market)}`,
      body,
    );
    return resp.data;
  }

  /** 手动派发一次滚动重训（dry_run 只算窗口计划不提交） */
  async dispatch(body: DispatchBody): Promise<DispatchResult> {
    const resp = await this.client.post<DispatchResult>('/models/rolling/dispatch', body);
    return resp.data;
  }

  /** 从模型目录派生滚动配方（dry_run 预览不落盘；保存后进 /recipes 的 user 源） */
  async deriveRecipe(body: {
    model_id: string;
    dry_run?: boolean;
    window_policy?: Partial<RollingWindowPolicy> | null;
  }): Promise<DeriveResult> {
    const resp = await this.client.post<DeriveResult>('/models/rolling/derive', body);
    return resp.data;
  }
}

export const modelRollingService = new ModelRollingService();
