/** 因子研究 API 服务层（/api/v1/factor-research，引擎服务经网关转发） */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import type {
  CatalogResponse,
  CompareItemReq,
  CompareResponse,
  ComposeRequest,
  ComposeResponse,
  FactorDetail,
  LeaderboardResponse,
  OptimalResponse,
} from '../types/factorResearch';

const BASE = `${SERVICE_ENDPOINTS.USER_SERVICE}/factor-research`;

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** 带状态码的接口错误（页面据此区分「快照未生成」与其它失败） */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function requestJson<T>(path: string, init: RequestInit = {}, timeoutMs = 60000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init.headers || {}) },
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new ApiError(res.status, `因子研究接口失败 ${res.status}: ${detail.slice(0, 220)}`);
    }
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

/** 区间参数（start/end 为 YYYY-MM-DD 或 YYYY-MM，null=开放端） */
export interface RangeParams {
  start?: string | null;
  end?: string | null;
}

/** 数据集：classic=demo 复刻 82 因子；private=筛选保留 327 私人因子库 */
export type FactorDataset = 'classic' | 'private';

/** 因子目录（分类顺序 + 基准 + 快照元信息） */
export function getCatalog(dataset: FactorDataset = 'classic'): Promise<CatalogResponse> {
  return requestJson<CatalogResponse>(`/catalog?dataset=${dataset}`);
}

/** 排行榜（区间内重算 KPI/超额/IC + 持仓画像 + 标签 + 综合分；n=持仓数） */
export function getLeaderboard(range?: RangeParams, n = 30, dataset: FactorDataset = 'classic'): Promise<LeaderboardResponse> {
  const qs = new URLSearchParams();
  if (range?.start) qs.set('start', range.start);
  if (range?.end) qs.set('end', range.end);
  if (n && n !== 30) qs.set('n', String(n));
  qs.set('dataset', dataset);
  return requestJson<LeaderboardResponse>(`/leaderboard?${qs.toString()}`, {}, 120000);
}

/** 单因子详情（ns=持仓数档位列表，如 [5,10,30]） */
export function getFactorDetail(
  code: string,
  ns: number[],
  range?: RangeParams,
  stocksN = 30,
  dataset: FactorDataset = 'classic',
): Promise<FactorDetail> {
  const qs = new URLSearchParams();
  qs.set('ns', ns.join(','));
  qs.set('stocks_n', String(stocksN));
  qs.set('dataset', dataset);
  if (range?.start) qs.set('start', range.start);
  if (range?.end) qs.set('end', range.end);
  return requestJson<FactorDetail>(`/factor/${encodeURIComponent(code)}?${qs.toString()}`, {}, 90000);
}

/** 多因子对比（每因子可单独设持仓数） */
export function postCompare(items: CompareItemReq[], range?: RangeParams, dataset: FactorDataset = 'classic'): Promise<CompareResponse> {
  const body: Record<string, unknown> = { items, dataset };
  if (range?.start) body.start = range.start;
  if (range?.end) body.end = range.end;
  return requestJson<CompareResponse>('/compare', { method: 'POST', body: JSON.stringify(body) }, 120000);
}

/** 多因子合成回测 */
export function postCompose(req: ComposeRequest): Promise<ComposeResponse> {
  return requestJson<ComposeResponse>('/compose', { method: 'POST', body: JSON.stringify(req) }, 120000);
}

/** 网格搜索最优权重（夏普/年化/超额各一组） */
export function postOptimalWeights(
  codes: string[],
  topN: number,
  range?: RangeParams,
  dataset: FactorDataset = 'classic',
): Promise<OptimalResponse> {
  const body: Record<string, unknown> = { codes, top_n: topN, dataset };
  if (range?.start) body.start = range.start;
  if (range?.end) body.end = range.end;
  return requestJson<OptimalResponse>('/optimal-weights', { method: 'POST', body: JSON.stringify(body) }, 180000);
}

/** 因子筛选结果（质量门槛 + 同源去重） */
export function getScreening(): Promise<import('../types/factorResearch').ScreeningResponse> {
  return requestJson('/screening');
}

// ---------------------------------------------------------------------------
// 快照管理（一键计算；全部本地计算，不上传任何数据）
// ---------------------------------------------------------------------------
export interface SnapshotStatus {
  exists: boolean;
  running: boolean;
  built_at: string | null;
  window: [string, string] | null;
  n_factors: number | null;
  n_dates: number | null;
  step: string;
  log_tail: string[];
}

export function getSnapshotStatus(dataset: FactorDataset = 'classic'): Promise<SnapshotStatus> {
  return requestJson<SnapshotStatus>(`/snapshot-status?dataset=${dataset}`);
}

export function postBuildSnapshot(dataset: FactorDataset = 'classic'): Promise<{ started: boolean; running: boolean; pid?: number }> {
  return requestJson(`/build?dataset=${dataset}`, { method: 'POST' }, 30000);
}

// ---------------------------------------------------------------------------
// 来源扫描（只读：列出 quantdb 里有哪些因子是快照目录还没有的）
// ---------------------------------------------------------------------------
/** 一条差异：因子代码 + 它来自哪个库（消失项可能取不到库名，故可为空串） */
export interface ScanDiffItem {
  code: string;
  library: string;
  library_label: string;
}

/** 「盘上扫到什么」× 「快照目录记了什么」的差异（后端 /scan，只读） */
export interface ScanDiff {
  /** 盘上有、快照目录没有（重算会收进来） */
  new: ScanDiffItem[];
  /** 快照目录有、盘上已找不到（重算会移出去） */
  missing: ScanDiffItem[];
  /** 新增数按来源库（分组标题用；顺序与 new 一致） */
  new_by_library: Record<string, number>;
  unchanged_count: number;
  /** 盘上扫到的因子数（已跨库去重） */
  discovered_count: number;
  /** 快照目录里记的因子数 */
  catalog_count: number;
  dataset: string;
  /** 当前快照的构建时间 */
  snapshot_at: string | null;
  /** 当前快照按哪个来源建的（auto/l1l2/kept）。非 auto 时「新增」会偏多，因为重算走 auto 全量 */
  snapshot_source: string | null;
}

/** 扫描 quantdb 的因子来源，列出与快照目录的差异（只读，不触发重算） */
export function getScanSources(dataset: FactorDataset = 'private'): Promise<ScanDiff> {
  return requestJson<ScanDiff>(`/scan?dataset=${dataset}`, {}, 30000);
}
