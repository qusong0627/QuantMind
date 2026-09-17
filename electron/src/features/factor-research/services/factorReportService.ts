/** 因子报告 API 服务层（/api/v1/factor-report，引擎服务经网关转发） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type {
  FactorClusterResponse,
  FactorPortfolioResponse,
  FactorCorrelation,
  FactorDatasetList,
  FactorDetail,
  FactorRelated,
  FactorSummaryResponse,
} from '../types/factorReport';

const BASE = `${SERVICE_ENDPOINTS.USER_SERVICE}/factor-report`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function getJson<T>(path: string, timeoutMs = 60000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, { headers: authHeaders(), signal: controller.signal });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new Error(`因子报告接口失败 ${res.status}: ${detail.slice(0, 120)}`);
    }
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

/** 可选数据集及各自快照状态 */
export function getFactorDatasets(): Promise<FactorDatasetList> {
  return getJson<FactorDatasetList>('/datasets');
}

/** 快照摘要：因子排行（IC / ICIR / 分位价差 / 换手） */
export function getFactorSummary(params: {
  dataset?: string;
  library?: string;
  sort?: 'abs_ic' | 'icir' | 'turnover' | 'ls_mean' | 'name';
  limit?: number;
} = {}): Promise<FactorSummaryResponse> {
  const qs = new URLSearchParams();
  if (params.dataset) qs.set('dataset', params.dataset);
  if (params.library) qs.set('library', params.library);
  if (params.sort) qs.set('sort', params.sort);
  if (params.limit) qs.set('limit', String(params.limit));
  const suffix = qs.toString() ? `?${qs}` : '';
  return getJson<FactorSummaryResponse>(`/summary${suffix}`);
}

/** 单因子明细（预计算序列，毫秒级） */
export function getFactorDetail(
  factor: string,
  dataset: string,
  horizon = 'fwd_ret_5',
  lookback = 250,
): Promise<FactorDetail> {
  const qs = new URLSearchParams({ factor, dataset, horizon, lookback: String(lookback) });
  return getJson<FactorDetail>(`/detail?${qs}`, 90000);
}

/** 因子相关性子矩阵 */
export function getFactorCorrelation(factors: string[], dataset: string): Promise<FactorCorrelation> {
  const qs = new URLSearchParams({ factors: factors.join(','), dataset });
  return getJson<FactorCorrelation>(`/correlation?${qs}`);
}

/** 与某因子最相关（含负相关）的因子 */
export function getFactorRelated(factor: string, dataset: string, top = 8): Promise<FactorRelated> {
  const qs = new URLSearchParams({ factor, dataset, top: String(top) });
  return getJson<FactorRelated>(`/related?${qs}`);
}

/** 因子去重清单：|ρ| ≥ 阈值 的同源因子簇，每簇留一个代表 */
export function getFactorClusters(
  dataset: string,
  threshold = 0.9,
  keep: 'icir' | 'abs_ic' | 'ls' = 'icir',
): Promise<FactorClusterResponse> {
  const qs = new URLSearchParams({ dataset, threshold: String(threshold), keep });
  return getJson<FactorClusterResponse>(`/clusters?${qs}`);
}

/** 推荐因子组合（含权重、方向、淘汰理由） */
export function getFactorPortfolio(dataset: string, recompute = false): Promise<FactorPortfolioResponse> {
  const qs = new URLSearchParams({ dataset });
  if (recompute) qs.set('recompute', 'true');
  return getJson<FactorPortfolioResponse>(`/portfolio?${qs}`);
}
