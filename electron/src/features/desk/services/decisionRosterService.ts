/** 决策名册服务（`/api/v1/decision/roster`；admin 读 + 保存 + 清空）。
 *
 * 走网关 8000 转发到 **trade** 服务：名册是 trade 进程自己读 `os.environ` 生效的，
 * 从 api 进程写只会落盘不生效（详见后端 `services/decision_roster_config.py`）。
 * 保存/清空失败时后端回 `{success:false, errors:[逐条人话原因]}`，这里原样抬给界面。
 */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type { RosterSaveEntry, RosterState } from '../components/decisionRosterModel';

const ROSTER_BASE = `${SERVICE_ENDPOINTS.USER_SERVICE}/decision/roster`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export class RosterApiError extends Error {
  status: number;
  errors: string[];
  constructor(status: number, message: string, errors: string[] = []) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

interface Envelope<T> {
  success?: boolean;
  data?: T;
  error?: string;
  errors?: string[];
  accepted?: Record<string, string>;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = 15000,
): Promise<Envelope<T>> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${ROSTER_BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init.headers || {}) },
      signal: controller.signal,
    });
    const text = await res.text().catch(() => '');
    let body: Envelope<T> = {};
    try {
      body = text ? (JSON.parse(text) as Envelope<T>) : {};
    } catch {
      body = {};
    }
    if (!res.ok) {
      const errors = Array.isArray(body.errors) ? body.errors : [];
      throw new RosterApiError(
        res.status,
        String(body.error || errors[0] || text.slice(0, 200) || `HTTP ${res.status}`),
        errors,
      );
    }
    return body;
  } finally {
    window.clearTimeout(timer);
  }
}

/** 名册现状 + 字段说明（`accepted` 由后端下发，前端不硬编码它的文案）。 */
export async function getRoster(): Promise<{
  state: RosterState;
  accepted: Record<string, string>;
}> {
  const body = await request<RosterState>('');
  return { state: (body.data ?? {}) as RosterState, accepted: body.accepted || {} };
}

/** 三个方法**同一个读法**：现状一律在 `data`（一个资源一种形状）。
 *  落盘细节（写了/删了哪些 key 变量）在 PUT/DELETE 的同级 `applied`，本页不用；
 *  曾经 PUT 把现状塞在 `data.state`，两个消费方各记一种读法迟早读错一边。 */
export async function saveRoster(entries: RosterSaveEntry[]): Promise<RosterState> {
  const body = await request<RosterState>('', {
    method: 'PUT',
    body: JSON.stringify({ entries }),
  });
  return (body.data ?? {}) as RosterState;
}

export async function clearRoster(): Promise<RosterState> {
  const body = await request<RosterState>('', { method: 'DELETE' });
  return (body.data ?? {}) as RosterState;
}
