/** 副驾驶 API 服务层（/api/v1/copilot/*，/api/v1/sentinel/annotate；面板/建议卡/执行/标注） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type { CopilotAdvice, CopilotPanel } from '../components/copilotModel';

const API = `${SERVICE_ENDPOINTS.USER_SERVICE}`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${API}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init.headers || {}) },
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new Error(`${path} 失败 ${res.status}: ${detail.slice(0, 160)}`);
    }
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

/** 交易台副驾驶面板聚合（事件流 + 时延 + 预算 + 误报率） */
export async function getCopilotPanel(hours = 24): Promise<CopilotPanel> {
  const res = await request<{ success: boolean; data: CopilotPanel }>(
    `/copilot/panel?hours=${hours}`,
  );
  return res.data;
}

/** 建议卡列表 */
export async function listAdvice(status = '', limit = 20): Promise<CopilotAdvice[]> {
  const qs = new URLSearchParams();
  if (status) qs.set('status', status);
  qs.set('limit', String(limit));
  const res = await request<{ success: boolean; data: { items: CopilotAdvice[] } }>(
    `/copilot/advice?${qs}`,
  );
  return res.data.items || [];
}

/** 一键执行（OrderRouter，来源 co_pilot） */
export async function executeAdvice(adviceId: string): Promise<{
  status: string;
  executed: number;
  total: number;
  results: Array<{ symbol: string; side: string; success: boolean; message?: string }>;
}> {
  const res = await request<{ success: boolean; data: never }>(
    `/copilot/advice/${adviceId}/execute`,
    { method: 'POST' },
    30000,
  );
  return res.data;
}

/** 拒绝建议（理由留痕） */
export async function rejectAdvice(adviceId: string, reason: string): Promise<void> {
  await request(`/copilot/advice/${adviceId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  });
}

/** 告警人工标注（误报闭环入口，T-P6-15） */
export async function annotateAlert(
  alertId: string,
  annotation: 'true_positive' | 'false_positive',
  note = '',
): Promise<void> {
  await request(`/sentinel/alerts/${alertId}/annotate`, {
    method: 'POST',
    body: JSON.stringify({ annotation, note }),
  });
}
