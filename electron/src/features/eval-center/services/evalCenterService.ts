/** 评估中心 API 服务层（/api/v1/eval，api 服务经网关；数据源=eval_scores 表，前端禁止直连表） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type {
  EvalHistoryResponse,
  EvalListResponse,
  EvalObjectType,
  EvalSeriesResponse,
  StrategyHealthResponse,
} from '../types/evalCenter';

const BASE = `${SERVICE_ENDPOINTS.USER_SERVICE}/eval`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function getJson<T>(path: string, timeoutMs = 30000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, {
      headers: authHeaders(),
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new Error(`评估接口失败 ${res.status}: ${detail.slice(0, 120)}`);
    }
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

/** 支持的评分卡类型（页签枚举唯一来源） */
export function getEvalObjectTypes(): Promise<{ success: boolean; data: EvalObjectType[] }> {
  return getJson('/object-types');
}

/** 最新快照网格（每对象一条，按分数降序） */
export function listScores(params: {
  objectType: string;
  latestOnly?: boolean;
  objectId?: string;
  limit?: number;
}): Promise<EvalListResponse> {
  const qs = new URLSearchParams({ object_type: params.objectType });
  if (params.latestOnly === false) qs.set('latest_only', 'false');
  if (params.objectId) qs.set('object_id', params.objectId);
  if (params.limit) qs.set('limit', String(params.limit));
  return getJson(`/scores?${qs.toString()}`);
}

/** 单对象评分历史（升序） */
export function getScoreHistory(
  objectType: string,
  objectId: string,
  limit = 180
): Promise<EvalHistoryResponse> {
  const qs = new URLSearchParams({
    object_type: objectType,
    object_id: objectId,
    limit: String(limit),
  });
  return getJson(`/scores/history?${qs.toString()}`);
}

/**
 * 单对象长序列侧车（逐日 IC / 分档收益 / 衰减 / 换手 / 相关性）。
 *
 * `meta.available=false` 是**正常返回**（该对象尚未产出序列，或侧车版本过期），
 * 前端据此展示 `meta.note`，不要当成请求失败。
 */
export function getObjectSeries(objectType: string, objectId: string): Promise<EvalSeriesResponse> {
  const qs = new URLSearchParams({ object_type: objectType, object_id: objectId });
  return getJson(`/series?${qs.toString()}`);
}

/** 策略体检档案（最新 + 历史 + 晋级门禁预演，与执行点同源） */
export function getStrategyHealth(strategyId: string, limit = 24): Promise<StrategyHealthResponse> {
  return getJson(`/health/${encodeURIComponent(strategyId)}?limit=${limit}`);
}

export interface UploadHealthResponse {
  success: boolean;
  data: {
    report: Record<string, unknown>;
    report_text: string;
    points: number;
    disclaimer: string;
    source: string;
  };
}

/** 自助体检（T-FE-15）：上传/粘贴净值曲线 → 九项报告（只读自查，不落档案/不参与门禁） */
export async function uploadHealthCheck(content: string, trials = 1): Promise<UploadHealthResponse> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 60000);
  try {
    const res = await fetch(`${BASE}/health/upload`, {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, trials }),
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      let message = detail.slice(0, 200);
      try {
        const parsed = JSON.parse(detail);
        if (parsed && typeof parsed.detail === 'string') message = parsed.detail;
      } catch {
        // 非 JSON 错误体按原文
      }
      throw new Error(message || `自助体检失败 ${res.status}`);
    }
    return (await res.json()) as UploadHealthResponse;
  } finally {
    window.clearTimeout(timer);
  }
}
