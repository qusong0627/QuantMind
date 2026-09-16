/** 评估中心 API 服务层（/api/v1/eval，api 服务经网关；数据源=eval_scores 表，前端禁止直连表） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type {
  EvalHistoryResponse,
  EvalListResponse,
  EvalObjectType,
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

/** 策略体检档案（最新 + 历史 + 晋级门禁预演，与执行点同源） */
export function getStrategyHealth(strategyId: string, limit = 24): Promise<StrategyHealthResponse> {
  return getJson(`/health/${encodeURIComponent(strategyId)}?limit=${limit}`);
}
