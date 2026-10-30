/**
 * 推理中性化（Neutralization Layer）前端服务。
 *
 * 后端路由见 `backend/services/api/routers/neutralization.py`：
 * 配置 / 重算 / 诊断 / 明细 / 治理总览 / 审计。
 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';

export type NeutralizationMode = 'off' | 'shadow' | 'enforce';

export interface NeutralizationSettings {
  mode: NeutralizationMode;
  universe_id: string;
  controls: string[];
  min_n: number;
  winsor_pct: number;
  control_lag: number;
  config_version: string;
  updated_by: string;
  updated_at: string | null;
  exists: boolean;
}

export interface NeutralizationDiagnostic {
  neutral_run_id: string;
  source_run_id: string | null;
  data_trade_date: string;
  universe_scope: string;
  status: 'ok' | 'degraded' | 'failed';
  n_symbols: number;
  n_total: number;
  coverage: number;
  r2: number | null;
  r2_pass1: number | null;
  condition_number: number | null;
  exposure: Record<string, number | null> | null;
  reason: string | null;
  config_version: string;
  created_at: string;
}

export interface NeutralizationOverviewRow extends NeutralizationSettings {
  model_id: string;
  market: string;
  last_status: string | null;
  data_trade_date: string | null;
  coverage: number | null;
  r2: number | null;
  reason: string | null;
  universe_scope: string | null;
  last_computed_at: string | null;
}

export interface NeutralizationScoreRow {
  symbol: string;
  raw_score: number;
  neutral_score: number | null;
  flag: string | null;
  config_version: string;
}

export interface NeutralizationSettingsBundle {
  settings: NeutralizationSettings;
  diagnostics: NeutralizationDiagnostic[];
}

export interface NeutralizationSettingsInput {
  mode: NeutralizationMode;
  universe_id?: string;
  controls?: string[];
  min_n?: number;
  winsor_pct?: number;
  control_lag?: number;
}

interface Envelope<T> {
  ok: boolean;
  data: T;
  meta?: Record<string, unknown>;
}

class NeutralizationService {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({ timeout: 30000, headers: { 'Content-Type': 'application/json' } });

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

  async getSettings(modelId: string): Promise<NeutralizationSettingsBundle> {
    const resp = await this.client.get<Envelope<NeutralizationSettingsBundle>>(
      `/models/${encodeURIComponent(modelId)}/neutralization`,
    );
    return resp.data.data;
  }

  async putSettings(
    modelId: string,
    body: NeutralizationSettingsInput,
  ): Promise<NeutralizationSettings> {
    const resp = await this.client.put<Envelope<NeutralizationSettings>>(
      `/models/${encodeURIComponent(modelId)}/neutralization`,
      body,
    );
    return resp.data.data;
  }

  async recompute(
    modelId: string,
    dataTradeDate: string,
    runId?: string,
  ): Promise<Record<string, unknown>> {
    const resp = await this.client.post<Envelope<Record<string, unknown>>>(
      `/models/${encodeURIComponent(modelId)}/neutralization/recompute`,
      { data_trade_date: dataTradeDate, run_id: runId },
    );
    return resp.data.data;
  }

  async getDiagnostics(
    modelId: string,
    params: { start_date?: string; end_date?: string; limit?: number } = {},
  ): Promise<NeutralizationDiagnostic[]> {
    const resp = await this.client.get<Envelope<NeutralizationDiagnostic[]>>(
      `/models/${encodeURIComponent(modelId)}/neutralization/diagnostics`,
      { params },
    );
    return resp.data.data;
  }

  async getScores(
    modelId: string,
    dataTradeDate: string,
    limit = 200,
    offset = 0,
  ): Promise<{ rows: NeutralizationScoreRow[]; total: number }> {
    const resp = await this.client.get<Envelope<NeutralizationScoreRow[]>>(
      `/models/${encodeURIComponent(modelId)}/neutralization/scores`,
      { params: { data_trade_date: dataTradeDate, limit, offset } },
    );
    return { rows: resp.data.data, total: Number(resp.data.meta?.total ?? 0) };
  }

  async getOverview(): Promise<NeutralizationOverviewRow[]> {
    const resp = await this.client.get<Envelope<NeutralizationOverviewRow[]>>(
      '/models/neutralization/overview',
    );
    return resp.data.data;
  }
}

export const neutralizationService = new NeutralizationService();
