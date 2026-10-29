/** A 股市场分析 · 因子概率板取数层（/api/v1/market-analysis/factor-board）
 *
 * 契约：后端读产物 JSON 原样返回 + available_dates；404=无产物/该决策日无文件
 * （detail 带 available_dates）；409=产物损坏或 decision_date 错位。
 * 展示纪律：判定枚举英文机读（resonant_long 等），前端映射位置词（靠前/靠后），
 * 不写买卖 —— 见 features/shared/signalVocabulary.ts 的边界。
 */

import { SERVICE_ENDPOINTS } from '../../../config/services';

const MA_API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis`;

/** 产物必需顶层键（与后端金样 factorBoardGolden.json `boardRequiredTop` 共读防漂移）。 */
export const REQUIRED_TOP_KEYS = [
  'schema_version',
  'meta',
  'factors',
  'horizon',
  'validation',
  'resonance',
  'sectors',
  'stocks',
] as const;

export interface FactorBoardFactor {
  name: string;
  lib: string;
  group: string;
  display: string;
  sign: number;
  ic60: number | null;
  win60: number | null;
  /** ICIR=mean(IC)/std(IC)（不是 t）；1.1 产物才有 */
  icir60: number | null;
  /** t=ICIR×√n（显著性；1.1 增量，旧产物缺失） */
  t60?: number | null;
  kept_ic: number | null;
  weight: number | null;
  horizon: Record<string, number | null>;
  // ── 1.1 增量（schema <1.1 的历史产物没有这些键 → 一律可选）──
  /** 净 IC：扣当日新进多头腿成本后的 RankIC（只显形，不进闸门）。 */
  net_ic60?: number | null;
  net_win60?: number | null;
  net_icir60?: number | null;
  net_t60?: number | null;
  /** 成本侵蚀：净 IC 与毛 IC 反号（净轨有效日不足时 null）。 */
  eroded?: boolean | null;
  /** 尾窗 20 个有效日的方向对齐 IC 均值（sgn×RankIC；可负）。 */
  ic20?: number | null;
  win20?: number | null;
  /** ok / weak / flipped / insufficient（见 DECAY_META）。 */
  decay?: string | null;
  /** regime 调权轨迹：原始权重 / 乘子 / 桶内对齐 IC / 桶有效日 / 注记。 */
  w_raw?: number | null;
  regime_mult?: number | null;
  regime_ic?: number | null;
  regime_n?: number | null;
  regime_note?: string | null;
}

/** coverage.mask：可交易掩码命中信息（涨跌停/停牌为当日状态，无当日行不过滤）。 */
export interface FactorBoardMaskInfo {
  mask_date: number | null;
  mask_max_dt: number | null;
  stale_days: number | null;
  missing: boolean;
}

/** 顶层 regime 块（P2-c；DB 不可用/样本不足时整块 null，权重全不动）。 */
export interface FactorBoardRegime {
  market: string;
  state: string;
  state_asof: string;
  state_lag_days: number;
  lookback_days: number;
  eval_days: number;
  bucket_n: number;
  weak_count: number;
  adjusted_count: number;
  weak_mult: number;
  by_family: Record<string, { state_ic: number | null; n_days: number; mult: number }>;
}

/** 顶层 dedup 块（P2-e；入选内二次去重：|ρ|≥reject 判重、[warn,reject) 记警）。 */
export interface FactorBoardDedup {
  n_pool: number;
  n_reserves: number;
  n_kept: number;
  reject: number;
  warn: number;
  dropped: { key: string; matched_key: string; rho: number | null }[];
  pruned: string[];
  warnings: { key: string; matched_key: string; rho: number | null }[];
}

/** validation.rolling_oos：最近一期滚动重选台账的后窗验证（前窗选、后窗验）。 */
export interface FactorBoardRollingOos {
  month: string;
  as_of: string | null;
  since?: string;
  until?: string;
  horizon?: number;
  rank_ic: number | null;
  icir?: number | null;
  t?: number | null;
  win: number | null;
  days: number;
  excluded_no_fwd?: number;
  note?: string;
  filled_by_month?: string;
  filled_at?: string;
}

export interface FactorBoardSectorItem {
  name: string;
  /** 板块成分总数 */
  n: number;
  /** 有合成分（参与判定）的成分数；1.1 增量 */
  n_scored?: number | null;
  fac: number | null;
  med: number | null;
  fbreadth: number | null;
  mbreadth: number | null;
  model_cov: number | null;
  verdict: string;
}

export interface FactorBoardSectorGroup {
  q67: number | null;
  q33: number | null;
  items: FactorBoardSectorItem[];
}

export interface FactorBoardStock {
  symbol: string;
  name: string | null;
  composite: number | null;
  model_rank: number | null;
  /** 无模型覆盖时可为 null（top/bottom 不再强制模型覆盖） */
  quadrant: string | null;
  sector_l2: string | null;
}

export interface FactorBoardMeta {
  decision_date: string;
  trade_date: string;
  generated_at: string;
  data_window?: string[];
  eval_days?: number;
  model: {
    trade_date: string;
    run_id: string;
    n: number;
    source: string;
    stale: boolean;
    by_side?: Record<string, number>;
  };
  coverage: {
    pool: number;
    evaluated: number;
    gated: number;
    gated_long: number;
    gated_short: number;
    selected: number;
    families: number;
    composite_n: number;
    model_cov_n: number;
    close_nonpos: number;
    /** 1.1 增量（可选，历史产物无此键）。 */
    mask?: FactorBoardMaskInfo | null;
  };
  /** 1.1 阈值快照（含 cost_rate / DECAY_* / REGIME_* / DEDUP_*；可选）。 */
  thresholds?: Record<string, unknown>;
  warnings: string[];
}

export interface FactorBoardValidation {
  stock?: { rank_ic: number | null; icir: number | null; t: number | null; win: number | null; days: number } | null;
  sector?: { rank_ic: number | null; icir: number | null; t: number | null; win: number | null; days: number } | null;
  in_sample?: boolean;
  note?: string;
  /** 1.1：最近一期滚动 OOS（暂无已回填台账 → 键缺失或 null）。 */
  rolling_oos?: FactorBoardRollingOos | null;
}

export interface FactorBoardResponse {
  schema_version: string;
  meta: FactorBoardMeta;
  factors: FactorBoardFactor[];
  horizon: {
    global: Record<string, number | null>;
    by_group: Record<string, Record<string, number | null>>;
  };
  validation: FactorBoardValidation;
  resonance: { spearman: number | null; n: number; quadrants: Record<string, number> };
  sectors: {
    industry_l1: FactorBoardSectorGroup | null;
    industry_l2: FactorBoardSectorGroup | null;
    concept: FactorBoardSectorGroup | null;
  };
  stocks: {
    top: FactorBoardStock[];
    bottom: FactorBoardStock[];
    resonant_long: FactorBoardStock[];
    resonant_short: FactorBoardStock[];
  };
  /** 1.1 顶层块：键必须存在（可 null）；1.0 历史产物无键 → 可选，缺则隐藏。 */
  regime?: FactorBoardRegime | null;
  dedup?: FactorBoardDedup | null;
  available_dates: string[];
}

/** 携带状态码与可用日期的取数错误（404 时前端可提示「尚未生成」并展示历史日）。 */
export class FactorBoardError extends Error {
  readonly status: number;
  readonly availableDates: string[];

  constructor(message: string, status: number, availableDates: string[] = []) {
    super(message);
    this.name = 'FactorBoardError';
    this.status = status;
    this.availableDates = availableDates;
  }
}

export async function getFactorBoard(date?: string): Promise<FactorBoardResponse> {
  const token = localStorage.getItem('access_token') || '';
  const qs = date ? `?date=${encodeURIComponent(date)}` : '';
  const res = await fetch(`${MA_API}/factor-board${qs}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    const msg =
      typeof detail === 'string'
        ? detail
        : (detail as { message?: string } | null)?.message || `因子概率板接口失败 ${res.status}`;
    const dates = (detail as { available_dates?: string[] } | null)?.available_dates || [];
    throw new FactorBoardError(msg, res.status, dates);
  }
  if (body === null || typeof body !== 'object') {
    throw new FactorBoardError('因子概率板产物结构异常（非对象）', res.status);
  }
  const missing = REQUIRED_TOP_KEYS.filter((k) => !(k in (body as Record<string, unknown>)));
  if (missing.length > 0) {
    throw new FactorBoardError(`因子概率板产物结构不完整：缺 ${missing.join('、')}`, res.status);
  }
  return body as FactorBoardResponse;
}

// ── 板块成分钻取（/factor-board/members） ────────────────────────────────

/** 板块成分行（含 ST 与无模型覆盖标的：quadrant 缺失为 null，绝不落 0/空串）。 */
export interface FactorBoardSectorMember {
  symbol: string;
  name: string | null;
  composite: number | null;
  model_rank: number | null;
  quadrant: string | null;
}

export interface FactorBoardMembersResponse {
  schema_version: string | null;
  decision_date: string | null;
  type: string;
  name: string;
  n: number;
  items: FactorBoardSectorMember[];
}

/**
 * 板块成分明细（点击板块行时的按需取数）。
 * 404=该决策日无明细（旧版产物未重跑产板）或板块名不存在（detail.message 直接可展示）。
 */
export async function getFactorBoardMembers(
  type: string,
  name: string,
  date?: string,
): Promise<FactorBoardMembersResponse> {
  const token = localStorage.getItem('access_token') || '';
  const qs = new URLSearchParams({ type, name });
  if (date) qs.set('date', date);
  const res = await fetch(`${MA_API}/factor-board/members?${qs.toString()}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    const msg =
      typeof detail === 'string'
        ? detail
        : (detail as { message?: string } | null)?.message || `板块成分接口失败 ${res.status}`;
    const dates = (detail as { available_dates?: string[] } | null)?.available_dates || [];
    throw new FactorBoardError(msg, res.status, dates);
  }
  const items = (body as { items?: unknown } | null)?.items;
  if (!Array.isArray(items)) {
    throw new FactorBoardError('板块成分明细结构异常（items 非数组）', res.status);
  }
  return body as FactorBoardMembersResponse;
}
