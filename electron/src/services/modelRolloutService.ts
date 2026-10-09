/**
 * 模型晋升流程服务（P2 · 设计 §3.1/§5.3/§5.4）。
 *
 * 与后端 `/api/v1/models/rollouts/*` 一一对应：
 * 创建 rollout → 评估（G0-G7 决策卡）→ 进观察期（G0/G1 硬闸门）→
 * 观察窗满自动 gate_passed → 人工晋升 / 拒绝 / 回滚（理由必填，审计链）。
 *
 * 错误语义：404 不存在 / 409 阶段冲突（如已终态、未到 gate_passed）/
 * 400 前置不满足（如 G0 不过、理由空白）。面板直接展示 detail 原文——
 * 后端文案已是给用户看的。
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';
import type { UserModelRecord } from './modelTrainingService';

export type RolloutStage =
  | 'replay_eval'
  | 'observing'
  | 'gate_passed'
  | 'promoted'
  | 'rejected'
  | 'rolled_back';

export const ACTIVE_ROLLOUT_STAGES: RolloutStage[] = ['replay_eval', 'observing', 'gate_passed'];

export interface RolloutGate {
  gate: string;
  status: 'pass' | 'fail' | 'skip' | 'info';
  reasons: string[];
  detail?: Record<string, unknown>;
}

export interface RolloutEvaluation {
  gates: RolloutGate[];
  summary: {
    counts: Record<string, number>;
    /** flagged=有 fail；incomplete=有 skip 无 fail；all_pass=全过 */
    verdict: 'flagged' | 'incomplete' | 'all_pass';
    enforced: boolean;
  };
  thresholds: Record<string, number>;
}

/** 观察期证据（窗口 = [observation_since, 今]，来自日更推理的 rank_ic） */
export interface RolloutObservation {
  since?: string;
  sufficient?: boolean;
  n_days?: number;
  challenger_mean_ic?: number | null;
  champion_mean_ic?: number | null;
  challenger_coverage?: number | null;
  champion_coverage?: number | null;
}

export interface RolloutAdmission {
  registration_soft_gate_passed?: boolean | null;
  status?: string;
  reproducibility?: {
    seed?: unknown;
    config_yaml?: boolean;
    data_fingerprint?: unknown;
  };
}

export interface RolloutEvidence {
  admission?: RolloutAdmission;
  independence?: {
    metrics_identical?: boolean | null;
    pred_md5_challenger?: string | null;
    pred_md5_champion?: string | null;
  };
  delta_summary?: {
    sufficient?: boolean;
    reason?: string | null;
    mean?: number | null;
    t?: number | null;
    n_days?: number;
  };
  monthly?: Record<string, unknown>;
  turnover?: Record<string, unknown>;
  observation?: RolloutObservation;
  trials?: { trial_count?: number | null };
  observation_since?: string;
}

export interface ModelRolloutRecord {
  rollout_id: string;
  tenant_id: string;
  user_id: string;
  market: string;
  campaign_id: string | null;
  champion_model_id: string;
  challenger_model_id: string;
  stage: RolloutStage;
  gate_result: RolloutEvaluation | null;
  evidence: RolloutEvidence | null;
  prior_default_model_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  notes: string | null;
  /** 仅详情端点附带：两侧模型记录（证据卡展示名） */
  champion?: UserModelRecord | null;
  challenger?: UserModelRecord | null;
}

/** 后端 409/400/404 的 detail 就是给用户看的中文文案 */
export function rolloutErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (error.response?.status === 404) return 'rollout 不存在（可能已被清理）';
    return `请求失败（HTTP ${error.response?.status ?? '—'}）`;
  }
  if (error instanceof Error) return error.message;
  return '未知错误';
}

class ModelRolloutService {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      // 评估要跑 vintage 回放（分钟级），超时给足；其余端点毫秒级返回
      timeout: 300000,
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

  async listRollouts(market?: string, limit = 50): Promise<ModelRolloutRecord[]> {
    const params: Record<string, string | number> = { limit };
    if (market) params.market = market;
    const resp = await this.client.get<{ items: ModelRolloutRecord[]; total: number }>(
      '/models/rollouts',
      { params },
    );
    return resp.data.items ?? [];
  }

  async createRollout(body: {
    market: string;
    challenger_model_id: string;
    campaign_id?: string | null;
    notes?: string | null;
  }): Promise<ModelRolloutRecord> {
    const resp = await this.client.post<ModelRolloutRecord>('/models/rollouts', body);
    return resp.data;
  }

  async getRollout(rolloutId: string): Promise<ModelRolloutRecord> {
    const resp = await this.client.get<ModelRolloutRecord>(`/models/rollouts/${rolloutId}`);
    return resp.data;
  }

  /** 重算 G0-G7（观察窗满自动转 gate_passed）；回放分钟级 */
  async evaluateRollout(
    rolloutId: string,
    thresholds?: Record<string, number>,
  ): Promise<{ rollout: ModelRolloutRecord; evaluation: RolloutEvaluation; warnings: string[] }> {
    const resp = await this.client.post(`/models/rollouts/${rolloutId}/evaluate`, {
      thresholds: thresholds ?? null,
    });
    return resp.data;
  }

  async startObservation(
    rolloutId: string,
    scheduleTime?: string,
  ): Promise<{ rollout: ModelRolloutRecord; settings: Record<string, unknown> }> {
    const resp = await this.client.post(`/models/rollouts/${rolloutId}/observing`, {
      schedule_time: scheduleTime ?? null,
    });
    return resp.data;
  }

  async promote(rolloutId: string, notes: string): Promise<ModelRolloutRecord> {
    const resp = await this.client.post(`/models/rollouts/${rolloutId}/promote`, { notes });
    return resp.data;
  }

  async reject(rolloutId: string, notes: string): Promise<ModelRolloutRecord> {
    const resp = await this.client.post(`/models/rollouts/${rolloutId}/reject`, { notes });
    return resp.data;
  }

  async rollback(rolloutId: string, notes: string): Promise<ModelRolloutRecord> {
    const resp = await this.client.post(`/models/rollouts/${rolloutId}/rollback`, { notes });
    return resp.data;
  }
}

export const modelRolloutService = new ModelRolloutService();
