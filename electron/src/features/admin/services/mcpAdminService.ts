/** 后台管理 · MCP 服务客户端（`/api/v1/admin/mcp/*`）。 */

import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../../../config/services';
import { authService } from '../../auth/services/authService';

export interface McpToolSpec {
  name: string;
  description: string;
  permission: string;
  category: string;
  version: string;
  write: boolean;
  default_enabled: boolean;
  destructive: boolean;
}

export interface McpGlobalConfig {
  enabled: boolean;
  tool_flags: Record<string, boolean>;
  limits: Record<string, number>;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface McpConfigPayload {
  global: McpGlobalConfig;
  tools: McpToolSpec[];
  defaults: { tool_flags: Record<string, boolean>; limits: Record<string, number> };
  limit_specs: Record<string, { min: number; max: number; label: string }>;
  permissions: Array<{ code: string; label: string }>;
  templates: Record<string, string[]>;
  service_url: string;
}

export interface McpClient {
  access_key: string;
  access_key_fp: string;
  name: string;
  user_id: string;
  tenant_id: string;
  permissions: string[];
  is_active: boolean;
  created_at?: string | null;
  last_used_at?: string | null;
  expires_at?: string | null;
  mcp: {
    enabled: boolean;
    tool_flags: Record<string, boolean>;
    limits: Record<string, number>;
    updated_at?: string | null;
  } | null;
}

export interface McpAuditRow {
  id: number;
  ts: string | null;
  tenant_id: string;
  user_id: string;
  access_key_fp: string;
  session_id?: string | null;
  tool: string;
  args_digest: string;
  status: string;
  error_code?: string | null;
  latency_ms?: number | null;
  request_id?: string | null;
  client_info?: string | null;
}

export interface McpHealth {
  reachable: boolean;
  status_code: number | null;
  detail: unknown;
  url: string;
}

const BASE = '/admin/mcp';

class McpAdminService {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: resolveWebSafeServiceBase(
        (import.meta as any).env?.VITE_USER_API_URL,
        SERVICE_ENDPOINTS.USER_SERVICE,
      ),
      timeout: 20000,
      headers: { 'Content-Type': 'application/json' },
    });
    this.client.interceptors.request.use((config) => {
      const token = authService.getAccessToken();
      if (token) {
        if (config.headers && typeof (config.headers as any).set === 'function') {
          (config.headers as any).set('Authorization', `Bearer ${token}`);
        } else if (config.headers) {
          (config.headers as any).Authorization = `Bearer ${token}`;
        }
      }
      return config;
    });
  }

  async getConfig(): Promise<McpConfigPayload> {
    const resp = await this.client.get(`${BASE}/config`);
    return resp.data.data;
  }

  async updateConfig(patch: {
    enabled?: boolean;
    tool_flags?: Record<string, boolean>;
    limits?: Record<string, number>;
  }): Promise<McpGlobalConfig> {
    const resp = await this.client.put(`${BASE}/config`, patch);
    return resp.data.data;
  }

  async listClients(limit = 200): Promise<McpClient[]> {
    const resp = await this.client.get(`${BASE}/clients`, { params: { limit } });
    return resp.data.data.items ?? [];
  }

  async updateClient(
    accessKey: string,
    patch: {
      enabled?: boolean;
      permissions?: string[];
      tool_flags?: Record<string, boolean>;
      limits?: Record<string, number>;
    },
  ): Promise<void> {
    await this.client.put(`${BASE}/clients/${encodeURIComponent(accessKey)}`, patch);
  }

  async revokeClient(accessKey: string): Promise<void> {
    await this.client.delete(`${BASE}/clients/${encodeURIComponent(accessKey)}`);
  }

  async issueKey(body: {
    user_id: string;
    tenant_id?: string;
    name?: string;
    template?: 'readonly' | 'research' | 'sim_trading';
    permissions?: string[];
  }): Promise<{ access_key: string; secret_key: string; name: string; user_id: string; tenant_id: string; permissions: string[] }> {
    const resp = await this.client.post(`${BASE}/clients`, body);
    return resp.data.data;
  }

  async getAudit(params: {
    limit?: number;
    offset?: number;
    user_id?: string;
    tool?: string;
    status?: string;
  } = {}): Promise<{ total: number; items: McpAuditRow[] }> {
    const resp = await this.client.get(`${BASE}/audit`, { params });
    return resp.data.data;
  }

  async health(): Promise<McpHealth> {
    const resp = await this.client.get(`${BASE}/health`);
    return resp.data.data;
  }
}

export const mcpAdminService = new McpAdminService();
