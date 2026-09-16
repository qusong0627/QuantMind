/** 今日交易台 API 服务层（/api/v1/desk/today；每块带 source 下钻字段） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type { DeskTodayResponse } from '../types';

const BASE = `${SERVICE_ENDPOINTS.USER_SERVICE}/desk`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface DeskTodayOptions {
  /** 是否运行体检（10 项断言，约 1-2s） */
  health?: boolean;
  /** 是否运行调仓计划预演（dry-run，约 1-3s） */
  plan?: boolean;
  timeoutMs?: number;
}

/** 今日交易台聚合（管线/信号/计划预演/执行/盈亏/影子/健康） */
export async function getDeskToday(options: DeskTodayOptions = {}): Promise<DeskTodayResponse> {
  const qs = new URLSearchParams();
  if (options.health === false) qs.set('health', 'false');
  if (options.plan === false) qs.set('plan', 'false');
  const suffix = qs.toString() ? `?${qs}` : '';

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), options.timeoutMs ?? 60000);
  try {
    const res = await fetch(`${BASE}/today${suffix}`, {
      headers: authHeaders(),
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new Error(`交易台接口失败 ${res.status}: ${detail.slice(0, 120)}`);
    }
    return (await res.json()) as DeskTodayResponse;
  } finally {
    window.clearTimeout(timer);
  }
}
