/** 实时推理管理服务（/api/v1/admin/realtime/infer/*；admin 读 + 写） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type {
  InferConfigView,
  InferStatusView,
  ModelOnnxStatus,
} from '../components/realtimeInferModel';

const INFER_BASE = `${SERVICE_ENDPOINTS.USER_SERVICE}/admin/realtime/infer`;

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

/** 实时推理候选模型（GET /infer/models；CN，轻量扫描） */
export interface InferModelOption {
  model_dir: string;
  name: string;
  dir_name: string;
  has_onnx: boolean;
  feature_count: number;
  updated_at: string;
}

export interface ExportOnnxResult {
  model_dir: string;
  report: { ok?: boolean; reason?: string } & Record<string, unknown>;
  model_onnx: ModelOnnxStatus;
}

export class InferApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${INFER_BASE}${path}`, {
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
  model_onnx?: ModelOnnxStatus;
  baseline_source?: string;
}> {
  const res = await request<{
    success: boolean;
    data: {
      config: InferConfigView;
      status: InferStatusView;
      model_onnx?: ModelOnnxStatus;
      baseline_source?: string;
    };
  }>('/config');
  return res.data;
}

export async function setInferConfig(payload: InferConfigPayload): Promise<InferConfigView> {
  const res = await request<{ success: boolean; data: { config: InferConfigView } }>('/config', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  return res.data.config;
}

/** 候选模型列表（选择 model_dir 用） */
export async function listInferModels(): Promise<InferModelOption[]> {
  const res = await request<{ success: boolean; data: { models: InferModelOption[] } }>('/models');
  return res.data.models;
}

/** 手动导出/重建 ONNX（不传 modelDir 时导出当前配置模型；含对照校验结果） */
export async function exportOnnx(modelDir?: string): Promise<ExportOnnxResult> {
  const res = await request<{ success: boolean; data: ExportOnnxResult }>(
    '/export-onnx',
    { method: 'POST', body: JSON.stringify({ model_dir: modelDir || undefined }) },
    60000,
  );
  return res.data;
}
