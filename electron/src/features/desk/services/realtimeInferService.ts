/** 实时推理管理服务（/api/v1/admin/realtime/infer/config；admin 读 + 写） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type { InferConfigView, InferStatusView } from '../components/realtimeInferModel';

const BASE = `${SERVICE_ENDPOINTS.USER_SERVICE}/admin/realtime/infer/config`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface InferConfigPayload {
  enabled?: boolean;
  model_dir?: string;
  cadence_s?: number;
  override_whitelist?: string[];
  min_live_coverage?: number;
}

export class InferApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(init: RequestInit = {}, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(BASE, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init.headers || {}) },
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new InferApiError(res.status, detail.slice(0, 200));
    }
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

export async function getInferConfig(): Promise<{
  config: InferConfigView;
  status: InferStatusView;
}> {
  const res = await request<{ success: boolean; data: { config: InferConfigView; status: InferStatusView } }>();
  return res.data;
}

export async function setInferConfig(payload: InferConfigPayload): Promise<InferConfigView> {
  const res = await request<{ success: boolean; data: { config: InferConfigView } }>({
    method: 'POST',
    body: JSON.stringify(payload),
  });
  return res.data.config;
}
