/** 今日交易台类型（FE-B：对齐 /api/v1/desk/today 契约，每块带 source 下钻字段） */

export interface PipelineStep {
  key: string;
  label: string;
  status: 'ok' | 'warn' | 'fail' | 'unknown';
  detail: string;
  source: string;
}

export interface SignalItem {
  symbol: string;
  /** 中文名（后端 enrichment；未收录为空串，前端回退显示代码） */
  name?: string;
  side: string;
  rank_pct: number | null;
  score: number | null;
}

export interface SignalsBlock {
  trade_date: string | null;
  market?: string;
  buy?: number;
  sell?: number;
  hold?: number;
  top_buy?: SignalItem[];
  source: string;
}

export interface PlanOrder {
  symbol: string;
  /** 中文名（后端 enrichment；未收录为空串） */
  name?: string;
  side: string;
  quantity: number;
  price: number;
  estimated_amount: number;
  reason: string;
  kind: 'exit' | 'rebalance' | string;
  is_limit_up: boolean;
  is_limit_down: boolean;
  is_suspended: boolean;
}

export interface PlanBlock {
  available: boolean;
  reason?: string;
  source: string;
  dry_run?: boolean;
  order_count?: number;
  signal_count?: number;
  orders?: PlanOrder[];
  strategy_id?: string;
  strategy_name?: string;
  mode?: string;
  error?: string | null;
}

export interface ExecutionItem {
  mode: 'SIM' | 'REAL';
  symbol: string;
  /** 中文名（后端 enrichment；未收录为空串） */
  name?: string;
  side: string;
  quantity: number;
  status: string;
  price_source?: string | null;
  client_order_id?: string | null;
  origin?: string | null;
  reason?: string | null;
  created_at?: string | null;
}

export interface ExecutionBlock {
  /**
   * false = 该市场**无法归集**委托（`sim_orders` 无 market 列）。
   * 此时 `sim_count/filled/rejected/items` 一律缺失——**必须渲染 `reason`**，
   * 不能按缺字段回退 0，否则"归集不了"会被显示成"今天没交易"（假证据）。
   */
  available?: boolean;
  market?: string;
  reason?: string;
  sim_count?: number;
  real_count?: number;
  filled?: number;
  rejected?: number;
  items?: ExecutionItem[];
  source: string;
}

export interface PnlBlock {
  available: boolean;
  detail?: string;
  snapshot_date?: string;
  total_asset?: number;
  initial_capital?: number;
  total_pnl?: number;
  today_pnl?: number;
  market_value?: number;
  updated_at?: string | null;
  source: string;
}

export interface HealthItem {
  id: string;
  name: string;
  level: 'ok' | 'warn' | 'fail' | string;
  detail: string;
  suggestion?: string;
}

export interface HealthBlock {
  ok: number;
  warn: number;
  fail: number;
  items: HealthItem[];
  source: string;
}

export interface ShadowBlock {
  available: boolean;
  reason?: string;
  date?: string;
  ok?: boolean;
  fill?: Record<string, unknown> | null;
  slippage?: Record<string, unknown> | null;
  price_deviation?: Record<string, unknown> | null;
  source: string;
}

export interface EvidenceItem {
  id: string;
  name: string;
  level: string;
  detail: string;
  suggestion?: string;
  source?: string;
}

export interface EvidenceRing {
  key: string;
  label: string;
  artifact: string;
  frequency: string;
  level: 'ok' | 'warn' | 'fail' | 'no_evidence' | string;
  summary: string;
  items: EvidenceItem[];
}

export interface EvidenceBlock {
  rings: EvidenceRing[];
  no_evidence: string[];
  source: string;
}

export interface DeskToday {
  as_of: string;
  tenant_id: string;
  user_id: string;
  sim_user_id: string;
  /** 服务端实际取数的市场（未声明时回落 CN）；用于丢弃过期响应 */
  market?: string;
  pipeline: PipelineStep[];
  signals: SignalsBlock;
  plan: PlanBlock;
  execution: ExecutionBlock;
  pnl: PnlBlock;
  shadow: ShadowBlock;
  health: HealthBlock;
  evidence?: EvidenceBlock;
}

export interface DeskTodayResponse {
  success: boolean;
  data: DeskToday;
}
