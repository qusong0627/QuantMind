/**
 * 因子研究 API 服务层（/api/v1/factor-research，引擎服务经网关转发）。
 *
 * 市场分层（2026-10）：所有调用带 `market`（缺省 'CN' 与后端默认一致）。
 * market 由**页面参数传入**（不在这里读全局 store，避免服务层与 Redux 耦合；
 * 测试也不必挂 Provider）。非法市场由后端 4xx 拒绝，前端不做第二套校验。
 */

import { SERVICE_ENDPOINTS } from '../../../config/services';
import { apiClient } from '../../../services/aiStrategyClients';
import type {
  CatalogResponse,
  CompareItemReq,
  CompareResponse,
  ComposeRequest,
  ComposeResponse,
  FactorDetail,
  LeaderboardResponse,
  OptimalResponse,
  ResearchMarket,
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
export function getCatalog(dataset: FactorDataset = 'classic', market: ResearchMarket = 'CN'): Promise<CatalogResponse> {
  return requestJson<CatalogResponse>(`/catalog?dataset=${dataset}&market=${market}`);
}

/** 排行榜（区间内重算 KPI/超额/IC + 持仓画像 + 标签 + 综合分；n=持仓数；dedup>0 = 去重，口径同筛选页） */
export function getLeaderboard(range?: RangeParams, n = 30, dataset: FactorDataset = 'classic', dedup = 0, market: ResearchMarket = 'CN'): Promise<LeaderboardResponse> {
  const qs = new URLSearchParams();
  if (range?.start) qs.set('start', range.start);
  if (range?.end) qs.set('end', range.end);
  if (n && n !== 30) qs.set('n', String(n));
  qs.set('dataset', dataset);
  qs.set('market', market);
  if (dedup > 0) qs.set('dedup', String(dedup));
  return requestJson<LeaderboardResponse>(`/leaderboard?${qs.toString()}`, {}, 120000);
}

/** 单因子详情（ns=持仓数档位列表，如 [5,10,30]） */
export function getFactorDetail(
  code: string,
  ns: number[],
  range?: RangeParams,
  stocksN = 30,
  dataset: FactorDataset = 'classic',
  market: ResearchMarket = 'CN',
): Promise<FactorDetail> {
  const qs = new URLSearchParams();
  qs.set('ns', ns.join(','));
  qs.set('stocks_n', String(stocksN));
  qs.set('dataset', dataset);
  qs.set('market', market);
  if (range?.start) qs.set('start', range.start);
  if (range?.end) qs.set('end', range.end);
  return requestJson<FactorDetail>(`/factor/${encodeURIComponent(code)}?${qs.toString()}`, {}, 90000);
}

/** 多因子对比（每因子可单独设持仓数） */
export function postCompare(items: CompareItemReq[], range?: RangeParams, dataset: FactorDataset = 'classic', market: ResearchMarket = 'CN'): Promise<CompareResponse> {
  const body: Record<string, unknown> = { items, dataset, market };
  if (range?.start) body.start = range.start;
  if (range?.end) body.end = range.end;
  return requestJson<CompareResponse>('/compare', { method: 'POST', body: JSON.stringify(body) }, 120000);
}

/** 多因子合成回测（market 在 req 里，调用方组装） */
export function postCompose(req: ComposeRequest): Promise<ComposeResponse> {
  return requestJson<ComposeResponse>('/compose', { method: 'POST', body: JSON.stringify(req) }, 120000);
}

/** 网格搜索最优权重（夏普/年化/超额各一组） */
export function postOptimalWeights(
  codes: string[],
  topN: number,
  range?: RangeParams,
  dataset: FactorDataset = 'classic',
  market: ResearchMarket = 'CN',
): Promise<OptimalResponse> {
  const body: Record<string, unknown> = { codes, top_n: topN, dataset, market };
  if (range?.start) body.start = range.start;
  if (range?.end) body.end = range.end;
  return requestJson<OptimalResponse>('/optimal-weights', { method: 'POST', body: JSON.stringify(body) }, 180000);
}

/**
 * 因子筛选结果（质量门槛 + 同源去重）。
 * 非 CN 市场后端诚实缺席（`available:false` + reason，该产物是 A 股七库口径），
 * 前端据 reason 渲染阻断面 —— 不是「暂未筛选」。
 */
export function getScreening(market: ResearchMarket = 'CN'): Promise<import('../types/factorResearch').ScreeningResponse> {
  return requestJson(`/screening?market=${market}`);
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

export function getSnapshotStatus(dataset: FactorDataset = 'classic', market: ResearchMarket = 'CN'): Promise<SnapshotStatus> {
  return requestJson<SnapshotStatus>(`/snapshot-status?dataset=${dataset}&market=${market}`);
}

export function postBuildSnapshot(dataset: FactorDataset = 'classic', market: ResearchMarket = 'CN'): Promise<{ started: boolean; running: boolean; pid?: number }> {
  return requestJson(`/build?dataset=${dataset}&market=${market}`, { method: 'POST' }, 30000);
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
export interface ScanLibraryInfo {
  library: string;
  label: string;
  /** 该库最新分区日期 YYYYMMDD；读不到为 null */
  latest_dt: string | null;
  factor_count: number;
  new_count: number;
  /** 最新分区侧车记录的缺列数（0/null = 无标记） */
  missing_count?: number;
}

export interface ScanDiff {
  /** 盘上有、快照目录没有（重算会收进来） */
  new: ScanDiffItem[];
  /** 快照目录有、盘上已找不到（重算会移出去） */
  missing: ScanDiffItem[];
  /** 新增数按来源库（分组标题用；顺序与 new 一致） */
  new_by_library: Record<string, number>;
  /** 本次扫描到的库：最新数据日 / 因子数 / 新增数（「最新时间」列） */
  libraries?: ScanLibraryInfo[];
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
  /** 毕业管道状态（CUSTOM 挖掘库 → CN 盘面）；脚本不可用的部署为 null，前端隐藏该段 */
  pipeline?: PipelineState | null;
}

/**
 * 毕业管道：新挖到的因子从 CUSTOM 走到 CN 盘面的这一站。
 *
 * 判据与毕业桥脚本共用实现（后端 pipeline_state ↔ promote_rd_mined.plan_sync），
 * 前端只做展示 —— 不在这里重算任何「待毕业数」。
 */
export interface PipelineState {
  /** empty=挖掘库还没产出；pending=有新的卡在桥前；synced=已全部毕业 */
  state: 'empty' | 'pending' | 'synced';
  custom_n_factors: number | null;
  cn_n_factors: number | null;
  pending_factors: number;
  pending_factor_names: string[];
  pending_partitions: number;
  custom_last_dt: string | null;
  cn_last_dt: string | null;
}

/** 扫描 quantdb 的因子来源，列出与快照目录的差异（只读，不触发重算） */
export function getScanSources(dataset: FactorDataset = 'private', market: ResearchMarket = 'CN'): Promise<ScanDiff> {
  return requestJson<ScanDiff>(`/scan?dataset=${dataset}&market=${market}`, {}, 30000);
}

/**
 * 把当前用户名下「待物化」的挖掘因子一次性送进 rd_mined（2026-11-28）。
 *
 * 每个因子执行的是挖掘时生成的 factor_code（每个人不同），服务端只筛
 * 有代码、a_share、未定终态的条目；与 Alpha 研究的「物化选中」共用同一把
 * flock，运行中再点会 409。
 */
export interface MaterializePendingResult {
  started: boolean;
  running: boolean;
  requested: number;
  materializable: string[];
  skipped: Record<string, string>;
  remaining: number;
  message: string;
}

export async function materializeMyMinedFactors(): Promise<MaterializePendingResult> {
  const res = await apiClient.post('/alpha-agent/factors/materialize-pending');
  const data = res.data?.data ?? {};
  const skipped: Record<string, string> = {};
  if (data.skipped && typeof data.skipped === 'object') {
    for (const [key, value] of Object.entries(data.skipped)) {
      skipped[key] = String(value);
    }
  }
  return {
    started: Boolean(data.started),
    running: Boolean(data.running),
    requested: typeof data.requested === 'number' ? data.requested : 0,
    materializable: Array.isArray(data.materializable) ? data.materializable.map(String) : [],
    skipped,
    remaining: typeof data.remaining === 'number' ? data.remaining : 0,
    message: typeof data.message === 'string' ? data.message : '',
  };
}

// ---------------------------------------------------------------------------
// 一键毕业（CUSTOM 挖掘库 → CN：镜像 + 刷新字段注册；不发布训练目录）
// ---------------------------------------------------------------------------
export interface PromoteStatus {
  running: boolean;
  pid: number | null;
  step: string;
  log_tail: string[];
}

export function getPromoteStatus(): Promise<PromoteStatus> {
  return requestJson<PromoteStatus>('/promote-status');
}

export function postPromote(): Promise<{ started: boolean; running: boolean; pid?: number }> {
  return requestJson('/promote', { method: 'POST' }, 30000);
}
