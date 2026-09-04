/** TradingAgents API service */

import type { AnalysisProgress, AnalysisReport, AnalysisHistoryItem, LLMProvider } from '../types';
import { SERVICE_URLS } from '../../../config/services';

// 用前端配置的服务器地址（桌面端设置 / 环境变量），不走 vite 代理，随用户配置 IP 变化
const ENGINE_BASE = (): string => `${SERVICE_URLS.API_GATEWAY}/api/v1/trading-agents`;

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = localStorage.getItem('access_token');
  const resp = await fetch(`${ENGINE_BASE()}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...((options?.headers as Record<string, string>) || {}),
    },
    ...options,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `Request failed: ${resp.status}`);
  }
  const data = await resp.json();
  return data.data ?? data;
}

export async function startAnalysis(params: {
  ticker: string;
  trade_date: string;
  llm_provider?: string;
  deep_think_llm?: string;
  quick_think_llm?: string;
  market?: string;
}): Promise<{ analysis_id: string; ticker: string; trade_date: string; market?: string }> {
  return request('/analyze', {
    method: 'POST',
    body: JSON.stringify(params),
  });
}

export async function getProgress(analysisId: string): Promise<AnalysisProgress> {
  return request(`/progress/${analysisId}`);
}

export async function getReport(analysisId: string): Promise<AnalysisReport> {
  return request(`/report/${analysisId}`);
}

export async function getHistory(limit = 20): Promise<{ history: AnalysisHistoryItem[] }> {
  return request(`/history?limit=${limit}`);
}

export async function stopAnalysis(analysisId: string): Promise<{ message: string }> {
  return request('/stop', {
    method: 'POST',
    body: JSON.stringify({ analysis_id: analysisId }),
  });
}

export async function getConfig(): Promise<{ providers: LLMProvider[] }> {
  return request('/config');
}

export function getDownloadUrl(analysisId: string): string {
  return `${ENGINE_BASE()}/download/${analysisId}`;
}
