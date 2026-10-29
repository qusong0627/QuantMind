/** A 股市场分析 · 资金流全景 v2 取数层（/api/v1/market-analysis/money-flow/*）
 *
 * 契约（细案 v2 §4/§5/§8）：
 * - panorama：4 层桑基（entity/size/sector/stock）+ 板块 master 榜；金额单位「元」；
 * - sector：板块明细（20 日趋势 + 成分随窗口净额/贡献占比/板块内 rank）；
 * - stock：个股 20 日五档序列（亿元 4 位小数）+ 1/5/20 日窗口（元）+ 板块归因
 *   + factor（因子板共振态；板缺失为 null）；
 * - tags（融合面）：统一搜索框的板块名候选（/tags/matches）与统计+热门（/tags/stats）。
 * 展示纪律：展示面只说位置与流量（净流入/净流出、靠前/靠后），个股层不出现预测
 * 徽章（实测否决，见 disclosures）；streak/证据一律来自离线产物，缺失为 null，
 * 绝不回落成 0。
 */

import { SERVICE_ENDPOINTS } from '../../../config/services';

const MA_API = `${SERVICE_ENDPOINTS.USER_SERVICE}/market-analysis`;

export type FlowPeriod = '1d' | '2d' | '3d' | '5d' | '10d' | '20d';
export type FlowSectorSet = 'industry1' | 'industry2' | 'concept';
export type FlowDirection = 'all' | 'inflow' | 'outflow';

/** 条件统计（fwd=探针口径 t→t+3；exec=可执行口径 T+1 收盘建仓；样本不足时 null）。 */
export interface FlowEvidenceStats {
  n: number;
  excess_bp: number | null;
  excess_daily_bp: number | null;
  hit: number | null;
  base_hit: number | null;
  yearly?: Record<
    string,
    { n: number; excess_bp: number | null; hit?: number | null; base_hit?: number | null }
  >;
}

/** P 级证据徽章（仅板块层；由后端 pick_badge 统一出口）。 */
export interface FlowEvidenceBadge {
  level: number;
  horizon: number;
  label: string;
  fwd: FlowEvidenceStats;
  exec: FlowEvidenceStats | null;
  window: string[] | null;
  min_n: number;
}

export interface FlowPanoramaNode {
  name: string;
  kind: 'entity' | 'size' | 'sector' | 'stock';
  key: string;
  layer: number;
  net: number;
  net_total?: number;
  sign: number;
  // sector 节点
  streak?: number | null;
  evidence?: FlowEvidenceBadge | null;
  members?: number;
  pct_change?: number | null;
  net_ratio?: number | null;
  // stock 节点
  sector?: string;
  inner_rank?: number;
  inner_total?: number;
  close?: number | null;
  // 速率（当前窗口日均 vs 前一窗口日均）
  net_daily?: number | null;
  prev_net_daily?: number | null;
  accel?: number | null;
  prev_net_ratio?: number | null;
  accel_ratio?: number | null;
}

export interface FlowPanoramaLink {
  source: string;
  target: string;
  value: number;
  net: number;
  sign: number;
}

export interface FlowSpeedRow {
  name?: string;
  symbol?: string;
  sector?: string | null;
  net_daily: number | null;
  prev_net_daily: number | null;
  accel: number | null;
  net_ratio?: number | null;
  prev_net_ratio?: number | null;
  accel_ratio?: number | null;
}

export interface FlowSectorRow {
  name: string;
  net_main: number;
  net_daily?: number | null;
  prev_net_daily?: number | null;
  accel?: number | null;
  accel_ratio?: number | null;
  net_total: number;
  net_ratio: number | null;
  breadth: number | null;
  pct_change: number | null;
  members: number;
  buy: number;
  sell: number;
  sign: number;
  streak: number | null;
  evidence: FlowEvidenceBadge | null;
}

/**
 * 证据产物状态（meta.evidence，契约见后端 flow_panorama._evidence_context）：
 * 正常态 reason=null 且 available=true；降级态 reason ∈ not_applicable | not_built |
 * stale 且 available=false。后端从不下发 'ok'——判可用性一律用 available。
 */
export interface FlowEvidenceMeta {
  applicable: boolean;
  available: boolean;
  trade_date: string | null;
  window: string[] | null;
  reason: 'not_applicable' | 'not_built' | 'stale' | null;
  built_at?: string | null;
}

export interface FlowPanoramaMeta {
  unit: string;
  sector_set: FlowSectorSet;
  sector_set_label: string;
  sector_type: string;
  period: FlowPeriod;
  window_days: [string, string];
  direction: FlowDirection;
  coverage: { flow_symbols: number; covered_symbols: number; covered_pct: number | null };
  evidence: FlowEvidenceMeta;
  disclosure: string[];
  generated_at: string;
}

export interface FlowPanoramaResponse {
  available: boolean;
  trade_date: string | null;
  trade_date_raw: string | null;
  period: FlowPeriod;
  sector_set: FlowSectorSet;
  direction: FlowDirection;
  stocks_per_sector: number;
  top_sectors: number;
  nodes: FlowPanoramaNode[];
  links: FlowPanoramaLink[];
  sectors: FlowSectorRow[];
  /** 资金流速率榜：当前窗口日均 vs 前一窗口日均 */
  speed?: {
    sectors_up: FlowSpeedRow[];
    sectors_down: FlowSpeedRow[];
    stocks_up: FlowSpeedRow[];
    stocks_down: FlowSpeedRow[];
  };
  meta: FlowPanoramaMeta & { reason?: string };
}

export interface FlowSectorMember {
  symbol: string;
  name: string;
  net_main: number;
  net_total: number;
  super: number;
  large: number;
  medium: number;
  small: number;
  share: number | null;
  inner_rank: number;
  inner_total: number;
  /** 近 ≤5 个 L2 分区日逐日涨跌幅（升序统一日期组；缺价 pct=null 显 —）。 */
  recent?: { date: string; pct: number | null }[];
  /** 近 20 交易日累计涨幅（%）：官方逐日涨跌幅复利；有效日 <10（停牌/次新）为 null 显 —，绝非 0。 */
  pct_20d: number | null;
}

export interface FlowSectorDetailResponse {
  available: boolean;
  name: string;
  sector_set: FlowSectorSet;
  sector_set_label: string;
  period: FlowPeriod;
  trade_date: string;
  trade_date_raw: string;
  summary: {
    net_main: number;
    net_total: number;
    net_ratio: number | null;
    super: number;
    large: number;
    medium: number;
    small: number;
    buy: number;
    sell: number;
    breadth: number | null;
    pct_change: number | null;
    members: number;
    data_symbols: number;
    streak: number | null;
    evidence: FlowEvidenceBadge | null;
    net_daily?: number | null;
    prev_net_daily?: number | null;
    accel?: number | null;
    accel_ratio?: number | null;
  };
  speed?: {
    window: [string, string];
    prev_window: [string, string] | null;
    net_daily: number | null;
    prev_net_daily: number | null;
    accel: number | null;
    net_ratio?: number | null;
    prev_net_ratio?: number | null;
    accel_ratio?: number | null;
  };
  trend: { date: string; net_main: number; net_ratio: number | null }[];
  members: FlowSectorMember[];
  meta: { unit: string; window_days: [string, string]; evidence: FlowEvidenceMeta; disclosure: string[] };
}

export interface FlowStockSeriesPoint {
  date: string;
  super: number | null;
  large: number | null;
  medium: number | null;
  small: number | null;
  main: number | null;
  retail: number | null;
  net_ratio: number | null;
  close: number | null;
  pct_change: number | null;
  /** 事后 5/10/20 日涨跌（%，close(t)→close(t+h)）；仅历史回看，最新几天为 null */
  fwd_5d?: number | null;
  fwd_10d?: number | null;
  fwd_20d?: number | null;
}

export interface FlowStockAttribution {
  name: string;
  type: string;
  sector_set: FlowSectorSet | null;
  net_main: number;
  share: number | null;
  inner_rank: number;
  inner_total: number;
  streak: number | null;
  evidence: FlowEvidenceBadge | null;
  stock_net_main: number;
}

/** 因子板共振态（/money-flow/stock 的 factor 块，P3 对接）。
 *
 * 后端读因子概率板 members 伴生件的**行业二级**成员表：一只票恰属一个二级行业，
 * 概念多对多不适合做「所属行业」。`available=false` = 板在但该股未收录；
 * 整块 null = 板产物缺失（不渲染，不编造）。`stale` 由后端按 OVERLAY_STALE_DAYS
 * 判定（`stale_days` = 资金流交易日 − 板决策日）；陈旧时展示层只许显形不许冒充当日。
 */
export interface FlowStockFactor {
  available: boolean;
  decision_date: string | null;
  trade_date: string | null;
  stale_days: number | null;
  stale: boolean;
  sector_l2: string | null;
  quadrant: string | null;
  composite: number | null;
  model_rank: number | null;
}

export interface FlowStockDetailResponse {
  available: boolean;
  symbol: string;
  suffix: string;
  name: string;
  trade_date: string;
  trade_date_raw: string;
  close: number | null;
  pct_change: number | null;
  series: FlowStockSeriesPoint[];
  windows: Record<
    '1d' | '2d' | '3d' | '5d' | '10d' | '20d',
    { net_main: number; net_total: number; net_ratio: number | null; days: number } | null
  >;
  speed?: {
    window: [string, string];
    prev_window: [string, string] | null;
    net_daily: number | null;
    prev_net_daily: number | null;
    accel: number | null;
  };
  attribution: FlowStockAttribution[];
  factor: FlowStockFactor | null;
  meta: { unit: string; series_unit: string; window_days: [string, string]; disclosure: string[] };
}

// ── 证据台（/money-flow/evidence；信号档案 + 命中榜） ──────────────────────

/** 闸门五项判定（gate.checks 键为英文判据名，criteria 为整段判据文本）。 */
export interface FlowEvidenceGate {
  pass: boolean;
  evaluated_horizon: number;
  checks: Record<string, boolean>;
  criteria: string;
}

/** 环境分层（市场 20 日环境 up/down 两档的条件超额）。 */
export interface FlowEvidenceEnvSplit {
  n: number;
  excess_bp: number | null;
  hit: number | null;
}

/** 信号档案的一档条件（streak_2 / streak_3 / streak_5）。 */
export interface FlowEvidenceCondition {
  level: number;
  label: string;
  years: number;
  gate: FlowEvidenceGate;
  fwd: Record<string, FlowEvidenceStats>;
  exec: Record<string, FlowEvidenceStats>;
  env: Record<string, FlowEvidenceEnvSplit>;
  sub_splits?: {
    low_mom: FlowEvidenceStats;
    high_mom: FlowEvidenceStats;
    note: string;
  } | null;
}

export type FlowRejectedVerdict = 'reject' | 'background' | 'control';

/** 否决清单条目（「已实测、不具预测力、不参与展示」节）。 */
export interface FlowEvidenceRejected {
  key: string;
  scope: 'stock' | 'sector' | string;
  label: string;
  verdict: FlowRejectedVerdict | string;
  note: string;
  metrics: {
    n?: number;
    n_days?: number;
    excess_bp?: number | null;
    excess_daily_bp?: number | null;
    hit?: number | null;
    base_hit?: number | null;
    ic_mean?: number;
    icir?: number | null;
    t_stat?: number | null;
    pct_pos?: number;
    yearly?: Record<string, unknown>;
  };
}

/** 命中榜行（板块级；evidence 同徽章契约）。 */
export interface FlowEvidenceHit {
  name: string;
  streak: number;
  net_main: number | null;
  net_ratio: number | null;
  breadth: number | null;
  members: number;
  evidence: FlowEvidenceBadge | null;
}

export interface FlowEvidenceResponse {
  available: boolean;
  reason: 'not_built' | null;
  requested_date: string | null;
  trade_date: string | null;
  trade_date_raw: string | null;
  built_at: string | null;
  window: string[] | null;
  min_cond_n: number;
  badge_levels: number[] | null;
  badge_horizon: number | null;
  conventions: Record<string, string>;
  gate_criteria: string | null;
  coverage: { days: number; sectors: number; panel_rows: number } | null;
  conditions: Record<string, FlowEvidenceCondition>;
  rejected: FlowEvidenceRejected[];
  hits: FlowEvidenceHit[];
  hits_trade_date: string | null;
  hits_state: 'ok' | 'stale' | 'no_rows' | 'unbuilt';
  meta: {
    evidence_trade_date: string | null;
    evidence_window: string[] | null;
    disclosure: string[];
  };
}

export class MoneyFlowError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'MoneyFlowError';
    this.status = status;
  }
}

async function flowGet<T>(path: string): Promise<T> {
  const token = localStorage.getItem('access_token') || '';
  const res = await fetch(`${MA_API}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    const msg = typeof detail === 'string' ? detail : `资金流接口失败 ${res.status}`;
    throw new MoneyFlowError(msg, res.status);
  }
  if (body === null || typeof body !== 'object') {
    throw new MoneyFlowError('资金流接口结构异常（非对象）', res.status);
  }
  return body as T;
}

export interface FlowPanoramaParams {
  period?: FlowPeriod;
  sectorSet?: FlowSectorSet;
  topSectors?: number;
  stocksPerSector?: number;
  direction?: FlowDirection;
  date?: string;
}

export async function getFlowPanorama(params: FlowPanoramaParams = {}): Promise<FlowPanoramaResponse> {
  const qs = new URLSearchParams();
  qs.set('period', params.period || '1d');
  qs.set('sectors', params.sectorSet || 'industry2');
  if (params.topSectors) qs.set('top_sectors', String(params.topSectors));
  if (params.stocksPerSector) qs.set('stocks_per_sector', String(params.stocksPerSector));
  qs.set('direction', params.direction || 'all');
  if (params.date) qs.set('date', params.date);
  return flowGet<FlowPanoramaResponse>(`/money-flow/panorama?${qs.toString()}`);
}

export async function getFlowSector(
  name: string,
  params: { sectorSet?: FlowSectorSet; period?: FlowPeriod; top?: number; date?: string } = {},
): Promise<FlowSectorDetailResponse> {
  const qs = new URLSearchParams({ name });
  qs.set('sectors', params.sectorSet || 'industry2');
  qs.set('period', params.period || '1d');
  if (params.top) qs.set('top', String(params.top));
  if (params.date) qs.set('date', params.date);
  return flowGet<FlowSectorDetailResponse>(`/money-flow/sector?${qs.toString()}`);
}

export async function getFlowStock(
  symbol: string,
  params: { days?: number; date?: string } = {},
): Promise<FlowStockDetailResponse> {
  const qs = new URLSearchParams({ symbol });
  if (params.days) qs.set('days', String(params.days));
  if (params.date) qs.set('date', params.date);
  return flowGet<FlowStockDetailResponse>(`/money-flow/stock?${qs.toString()}`);
}

/** 热力矩形图下钻分类（与后端 /heatmap 的 category 一致；shenwan=通达信二级行业）。 */
export type HeatmapDrillCategory = 'shenwan' | 'industry1' | 'concept';

export interface HeatmapDrillItem {
  symbol: string;
  name: string;
  /** 矩形面积规模（亿）：优先总市值，无估值时回落成交额 */
  value: number;
  pct_change: number;
  /** 主力净额（亿）：flow_super_net + flow_large_net 当日值 */
  flow_net: number;
  is_leader: boolean;
}

export interface HeatmapDrillResponse {
  trade_date: string;
  category: HeatmapDrillCategory;
  category_label: string;
  sector_name: string;
  items: HeatmapDrillItem[];
  summary: {
    total: number;
    shown: number;
    truncated: boolean;
    advance: number;
    decline: number;
    flat: number;
    avg_pct: number;
    total_flow_yi: number;
    total_mv_yi: number;
  };
}

/** 板块 → 成分个股热力矩形图（下钻）；板块不存在时抛 MoneyFlowError(404)。 */
export async function getHeatmapDrill(
  params: { category?: HeatmapDrillCategory; name: string; date?: string },
): Promise<HeatmapDrillResponse> {
  const qs = new URLSearchParams({ name: params.name });
  qs.set('category', params.category || 'shenwan');
  if (params.date) qs.set('date', params.date);
  return flowGet<HeatmapDrillResponse>(`/heatmap/drill?${qs.toString()}`);
}

export interface HeatmapDirectionCell {
  /** P(板块绝对上涨) = 大盘先验 + (P(板块跑赢) − 0.5)，已截断到 5%~95% */
  prob: number;
  label: 'up' | 'down' | 'neutral';
  /** 大盘先验（情绪模型 T+N prob_up） */
  market_prob: number;
  /** 历史同状态 P(板块等权超额 > 0)，50%=无相对优势 */
  rel_prob: number;
  mean: number | null;
  n: number | null;
  date_win: number | null;
  n_dates: number | null;
  /** 全板块截面分位（0~1，1=相对概率最高）；前端计算，不来自后端 */
  rank?: number | null;
  state: { stress: string; flow: string; price: string };
}

export interface HeatmapDirectionMarket {
  trade_date?: string | null;
  stress_score?: number | null;
  stress_zone?: string | null;
  stress_label?: string | null;
  stress_note?: string | null;
  phase?: string | null;
  win20?: number | null;
  median20?: number | null;
  p10_20?: number | null;
  p90_20?: number | null;
  n_days?: number | null;
  market_prob?: Record<string, number | null>;
  indices?: Array<{
    key: string;
    name: string;
    change_pct: number | null;
    prob_up: Record<string, number | null>;
  }>;
  available?: boolean;
  neutral_band?: [number, number];
  disclosure?: string;
  note?: string;
}

export interface HeatmapTopItem {
  name: string;
  value: number;
  pct_change: number;
  /** 周期等权涨幅（%）；1 日与 pct_change 相同 */
  pct_period?: number | null;
  /** 周期主力净额（亿）：flow_super_net + flow_large_net 窗口和 */
  flow_net?: number | null;
  /** 当前周期日均（亿/日） */
  flow_daily?: number | null;
  /** 前一窗口日均（亿/日） */
  prev_flow_daily?: number | null;
  /** 加速度 = 当前日均 − 前一窗口日均（亿/日） */
  flow_accel?: number | null;
  /** 周期净额比（主力净额/成交额） */
  flow_ratio?: number | null;
  /** 当前窗口「成分主力净流入为正」的日均占比（资金广度，0~1） */
  breadth_flow?: number | null;
  /** 前一窗口资金广度 */
  breadth_flow_prev?: number | null;
  /** 资金广度变化（当前 − 前一窗口，百分点/100） */
  breadth_flow_chg?: number | null;
  /** 板块净额比加速（当前窗口日均 − 前一窗口日均） */
  flow_ratio_accel?: number | null;
  /** 成分收盘价在 MA20 上方的占比（价格广度，0~1） */
  pct_above_ma20?: number | null;
  /** 5 个交易日前的价格广度 */
  pct_above_ma20_prev?: number | null;
  /** 价格广度变化（当前 − 5 日前） */
  pct_above_ma20_chg?: number | null;
  /** 情绪热度（0~1，前端按截面分位合成；不是概率） */
  sentiment_score?: number | null;
  period?: string;
  period_days?: number;
  leader?: string;
  leader_pct?: number;
  /** 板块方向概率（仅 shenwan 最新快照；键为 5/10/20） */
  direction?: Record<string, HeatmapDirectionCell> | null;
}

export interface HeatmapTopResponse {
  items: HeatmapTopItem[];
  direction: HeatmapDirectionMarket | null;
}

/** 板块层热力图（顶层）。realtime=true 跳过快照、直接读 QuantDB 全量
 *  —— 快照库既无主力净额、又只存概念板块前 50，故顶层一律走实时全量。 */
export async function getHeatmapTop(
  params: {
    category?: HeatmapDrillCategory;
    date?: string;
    realtime?: boolean;
    period?: string;
  } = {},
): Promise<HeatmapTopResponse> {
  const qs = new URLSearchParams({ category: params.category || 'shenwan' });
  if (params.period) qs.set('period', params.period);
  if (params.date) qs.set('date', params.date);
  if (params.realtime) qs.set('realtime', '1');
  const body = await flowGet<{ items?: HeatmapTopItem[]; direction?: HeatmapDirectionMarket | null }>(
    `/heatmap?${qs.toString()}`,
  );
  return {
    items: Array.isArray(body.items) ? body.items : [],
    direction: body.direction ?? null,
  };
}

/** 证据台：信号档案（过闸条件统计 + 否决清单）+ 命中榜（默认最新，可回溯快照日）。 */
export async function getFlowEvidence(
  params: { date?: string } = {},
): Promise<FlowEvidenceResponse> {
  const qs = new URLSearchParams();
  if (params.date) qs.set('date', params.date);
  const suffix = qs.toString();
  return flowGet<FlowEvidenceResponse>(`/money-flow/evidence${suffix ? `?${suffix}` : ''}`);
}

// ── 板块标签融合（统一搜索框 / 热门 chips；数据面 /tags/*） ───────────────

/** 标签体系统计（/tags/stats；hot_tags 的 type 为板块体系字符串）。 */
export interface FlowTagStats {
  trade_date: string | null;
  total_sectors: number;
  total_stocks: number;
  avg_tags_per_stock: number;
  max_tags_per_stock: number;
  total_relations: number;
  hot_tags: { name: string; type: string; count: number }[];
}

/** 板块名候选（/tags/matches；子串匹配、精确 > 前缀 > 子串排序）。 */
export interface FlowTagMatch {
  name: string;
  sector_type: string;
  count: number;
}

/**
 * 标签统计 + 热门板块。hotType（后端板块体系字符串，如 `行业板块(二级)`）按体系过滤
 * 热门：全局混合 top-N 会被成分数大的体系（概念/地区）挤空——行业一级在全局
 * top-100 里一条都没有；单体系过滤才凑得齐热门列表。缺省仍为全局混合。
 */
export async function getTagStats(limit = 100, hotType?: string): Promise<FlowTagStats> {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (hotType) qs.set('hot_type', hotType);
  return flowGet<FlowTagStats>(`/tags/stats?${qs.toString()}`);
}

export async function getTagMatches(q: string): Promise<FlowTagMatch[]> {
  const data = await flowGet<{ q: string; matches: FlowTagMatch[] }>(
    `/tags/matches?q=${encodeURIComponent(q)}`,
  );
  return data.matches;
}

// ── 展示格式化（元/亿元口径统一出口；缺失一律 —，绝不显 0） ─────────────

export const YI = 1e8;

/** 元 → 亿元带符号文本（如 +12.34 亿 / -3.05 亿）。 */
export function fmtYi(yuan: number | null | undefined, digits = 2): string {
  if (yuan === null || yuan === undefined || Number.isNaN(yuan)) return '—';
  const v = yuan / YI;
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)} 亿`;
}

/** 「已是亿元」的序列值 → 带符号文本。与 fmtYi（入参「元」）严格区分：
 *  flow 逐日序列（个股五档 / 板块 trend）后端已按 1e8 折算，再走 fmtYi 会双重
 *  缩放，任何真实值都会显示成 +0.00 亿（个股卡 tooltip 曾因此全为 0）。 */
export function fmtSeriesYi(yi: number | null | undefined, digits = 2): string {
  if (yi === null || yi === undefined || Number.isNaN(yi)) return '—';
  return `${yi >= 0 ? '+' : ''}${yi.toFixed(digits)} 亿`;
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`;
}

/** 净额比（主力净额/成交额，已为小数比例）→ 百分比文本。 */
export function fmtRatio(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

/** A 股口径涨红跌绿（与全站一致；金额正负同此）。 */
export function flowTextClass(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return 'text-slate-500';
  return v > 0 ? 'text-rose-600' : 'text-emerald-600';
}

/** 全景空态原因 → 用户文案（后端 meta.reason）。 */
export function panoramaEmptyHint(reason: string | undefined): string {
  switch (reason) {
    case 'quantdb_unavailable':
      return '本地 QuantDB 数据不可用，请先在「数据管理」完成行情同步';
    case 'date_not_found':
      return '所选快照日期没有资金流分区数据，请换一个日期';
    case 'no_l2_data':
      return '尚无 L2 资金流分区数据（6_ml_datasets/l2_factors），请先执行数据同步';
    case 'no_flow_data':
      return '窗口内没有可聚合的资金流数据';
    case 'sector_set_empty':
      return '该板块体系暂无成分数据';
    default:
      return '资金流全景数据不可用';
  }
}

/* ── 资金流预测力（/money-flow/predictive）──────────────────────────────
 * 研究产物（离线）+ 当日质量分/形态匹配；缺失一律显式 available=false，
 * 前端隐藏统计区，不拿 0 或旧数据冒充。 */
export interface FlowPredictiveStatRow {
  q: number;
  n: number;
  mean: number | null;
  stock_win: number | null;
  date_win: number | null;
  date_mean: number | null;
}

export interface FlowPredictivePattern {
  pattern: string;
  h: number;
  n: number;
  date_win: number | null;
  date_mean: number | null;
  stock_win: number | null;
  yearly?: Record<string, number | null>;
}

export interface FlowPredictiveHeatmap {
  mean: (number | null)[][];
  win: (number | null)[][];
  n: number[][];
}

export interface FlowPredictiveArtifact {
  schema?: string;
  built_at?: string;
  trade_date?: string;
  window?: [string, string];
  tables?: Record<string, FlowPredictiveStatRow[]>;
  patterns?: Record<string, FlowPredictivePattern>;
  [key: `heatmap_exec_h${number}`]: FlowPredictiveHeatmap | undefined;
  conventions?: Record<string, string>;
  disclosure?: string;
}

export interface FlowScoreRow {
  symbol: string;
  name: string;
  industry: string | null;
  score: number;
  components?: Record<string, number | null>;
  net_ratio?: number | null;
  z20?: number | null;
  r5?: number | null;
  close?: number | null;
  pct_change?: number | null;
}

export interface FlowSectorScore {
  name: string;
  score: number | null;
  n: number;
}

export interface FlowPredictiveCurrent {
  available: boolean;
  trade_date?: string;
  reason?: string;
  coverage?: { scored: number };
  top?: FlowScoreRow[];
  bottom?: FlowScoreRow[];
  patterns?: Record<string, { count: number; items: FlowScoreRow[] }>;
  sectors_top?: FlowSectorScore[];
  sectors_bottom?: FlowSectorScore[];
}

export interface FlowPredictiveResponse {
  available: boolean;
  artifact: FlowPredictiveArtifact | null;
  current: FlowPredictiveCurrent;
  meta: {
    score_definition: string;
    components: { key: string; sign: number; label: string; note: string }[];
    universe: string;
    disclosure: string;
  };
}

export async function getFlowPredictive(date?: string): Promise<FlowPredictiveResponse> {
  const qs = date ? `?date=${encodeURIComponent(date)}` : '';
  return flowGet<FlowPredictiveResponse>(`/money-flow/predictive${qs}`);
}

/* ── 资金流概率模型（/money-flow/probability）──────────────────────────── */
export interface FlowProbabilityStock {
  symbol: string;
  name: string;
  industry: string | null;
  net_ratio: number | null;
  z20: number | null;
  r5: number | null;
  amount: number | null;
  p3?: number | null;
  p5?: number | null;
  p10?: number | null;
  p20?: number | null;
  /** 未校准排序分（用于展示/着色；p 为 isotonic 校准后的概率） */
  raw3?: number | null;
  raw5?: number | null;
  raw10?: number | null;
  raw20?: number | null;
}

export interface FlowProbabilitySector {
  name: string;
  n: number;
  net_ratio: number | null;
  p3?: number | null;
  p5?: number | null;
  p10?: number | null;
  p20?: number | null;
  /** 未校准排序分（用于展示/着色；p 为 isotonic 校准后的概率） */
  raw3?: number | null;
  raw5?: number | null;
  raw10?: number | null;
  raw20?: number | null;
}

export interface FlowProbabilityStocks {
  prob_map: Record<string, Record<string, number | null>>;
  top_3?: FlowProbabilityStock[];
  top_5?: FlowProbabilityStock[];
  top_10?: FlowProbabilityStock[];
  top_20?: FlowProbabilityStock[];
  bottom_3?: FlowProbabilityStock[];
  bottom_5?: FlowProbabilityStock[];
  bottom_10?: FlowProbabilityStock[];
  bottom_20?: FlowProbabilityStock[];
  outflow_strong_3?: FlowProbabilityStock[];
  outflow_strong_5?: FlowProbabilityStock[];
  outflow_strong_10?: FlowProbabilityStock[];
  outflow_strong_20?: FlowProbabilityStock[];
  inflow_weak_3?: FlowProbabilityStock[];
  inflow_weak_5?: FlowProbabilityStock[];
  inflow_weak_10?: FlowProbabilityStock[];
  inflow_weak_20?: FlowProbabilityStock[];
}

export interface FlowProbabilityMetrics {
  auc?: number;
  top_mean?: number;
  bot_mean?: number;
  top_hit?: number;
  base_hit?: number;
  n_days?: number;
}

export interface FlowProbabilityArtifact {
  schema?: string;
  built_at?: string;
  trade_date: string;
  window?: [string, string];
  features?: string[];
  conventions?: Record<string, string>;
  metrics?: Record<string, FlowProbabilityMetrics>;
  thresholds?: Record<string, { hi: number | null; lo: number | null }>;
  latest: {
    stocks: FlowProbabilityStocks;
    prob_map?: Record<string, Record<string, number | null>>;
    sectors: FlowProbabilitySector[];
  };
}

export interface FlowProbabilityIntentStock {
  news_score?: number | null;
  news_n?: number;
  news_pos?: number;
  news_neg?: number;
  lhb_net?: number;
  lhb_inst_net?: number;
  block_amount?: number;
  block_premium?: number | null;
}

export interface FlowProbabilityIntent {
  trade_date: string;
  stocks: Record<string, FlowProbabilityIntentStock>;
  news_coverage?: string;
  smart_coverage?: string;
}

export interface FlowProbabilityResponse {
  available: boolean;
  reason?: string;
  hint?: string;
  trade_date?: string;
  requested_date?: string | null;
  as_of_latest?: boolean;
  artifact?: FlowProbabilityArtifact;
  intent?: FlowProbabilityIntent;
}

export async function getFlowProbability(date?: string): Promise<FlowProbabilityResponse> {
  const qs = date ? `?date=${encodeURIComponent(date)}` : '';
  return flowGet<FlowProbabilityResponse>(`/money-flow/probability${qs}`);
}
