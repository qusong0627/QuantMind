/** 今日交易台纯函数（展示模型；可单测，无副作用） */

import type {
  ExecutionBlock,
  HealthBlock,
  PipelineStep,
  PlanBlock,
  PlanOrder,
  PnlBlock,
} from './types';

export interface DrillEntryLike {
  label: string;
  value: string;
  source?: string;
  hint?: string;
}

export interface StatusStyle {
  label: string;
  dot: string;
  text: string;
}

const STATUS_STYLES: Record<string, StatusStyle> = {
  ok: { label: '正常', dot: 'bg-red-500', text: 'text-red-600' },
  warn: { label: '警告', dot: 'bg-amber-500', text: 'text-amber-600' },
  fail: { label: '异常', dot: 'bg-rose-600', text: 'text-rose-700' },
  unknown: { label: '未运行', dot: 'bg-slate-300', text: 'text-slate-500' },
  // T-FE-16：无证据 ≠ 绿——独立灰态（验收要求任一环节"无证据"可见）
  no_evidence: { label: '无证据', dot: 'bg-slate-200 border border-dashed border-slate-400', text: 'text-slate-400' },
};

export function statusStyle(status: string | undefined): StatusStyle {
  return STATUS_STYLES[String(status || 'unknown')] || STATUS_STYLES.unknown;
}

export interface PipelineSummary {
  ok: number;
  warn: number;
  fail: number;
  unknown: number;
  worst: 'ok' | 'warn' | 'fail' | 'unknown';
}

const _SEVERITY: Array<PipelineSummary['worst']> = ['fail', 'warn', 'unknown', 'ok'];

/** 管线汇总：计数 + 最差状态（决定头部徽章颜色） */
export function pipelineSummary(pipeline: PipelineStep[] | null | undefined): PipelineSummary {
  const steps = pipeline || [];
  const counts = { ok: 0, warn: 0, fail: 0, unknown: 0 };
  for (const step of steps) {
    const key = (step?.status || 'unknown') as keyof typeof counts;
    counts[key in counts ? key : 'unknown'] += 1;
  }
  const worst = _SEVERITY.find((level) => counts[level] > 0) || 'unknown';
  return { ...counts, worst };
}

export interface PlanSummary {
  buys: PlanOrder[];
  sells: PlanOrder[];
  exits: number;
  buyAmount: number;
  sellAmount: number;
}

/** 计划汇总：买/卖分组（涨红跌绿口径：买=红、卖=绿）、金额合计、退出规则单数 */
export function planSummary(plan: PlanBlock | null | undefined): PlanSummary {
  const orders = plan?.orders || [];
  const buys = orders.filter((o) => String(o.side).toUpperCase() === 'BUY');
  const sells = orders.filter((o) => String(o.side).toUpperCase() === 'SELL');
  const sum = (list: PlanOrder[]) =>
    Math.round(list.reduce((acc, o) => acc + Number(o.estimated_amount || 0), 0) * 100) / 100;
  return {
    buys,
    sells,
    exits: orders.filter((o) => o.kind === 'exit').length,
    buyAmount: sum(buys),
    sellAmount: sum(sells),
  };
}

export function planKindLabel(kind: string | undefined): string {
  if (kind === 'exit') return '退出规则';
  if (kind === 'rebalance') return '定期调仓';
  return String(kind || '—');
}

export interface PnlSummary {
  available: boolean;
  totalPnl: number;
  todayPnl: number;
  returnPct: number | null;
}

/** 盈亏汇总：总盈亏/今日盈亏/累计收益率（除初始本金；无本金 → null 不硬算） */
export function pnlSummary(pnl: PnlBlock | null | undefined): PnlSummary {
  if (!pnl || !pnl.available) {
    return { available: false, totalPnl: 0, todayPnl: 0, returnPct: null };
  }
  const initial = Number(pnl.initial_capital || 0);
  return {
    available: true,
    totalPnl: Number(pnl.total_pnl || 0),
    todayPnl: Number(pnl.today_pnl || 0),
    returnPct: initial > 0 ? Number(pnl.total_pnl || 0) / initial : null,
  };
}

/** 执行卡汇总：成交/拒单（缺字段回退 0，不推断） */
export function executionSummary(execution: ExecutionBlock | null | undefined): {
  simCount: number;
  realCount: number;
  filled: number;
  rejected: number;
} {
  return {
    simCount: Number(execution?.sim_count || 0),
    realCount: Number(execution?.real_count || 0),
    filled: Number(execution?.filled || 0),
    rejected: Number(execution?.rejected || 0),
  };
}

export interface HealthItemView {
  id: string;
  name: string;
  level: string;
  style: StatusStyle;
  detail: string;
  suggestion: string;
}

/** 健康卡：12 项断言 → 视图行（红黄绿 + 下钻 detail/suggestion） */
export function healthItemViews(health: HealthBlock | null | undefined): HealthItemView[] {
  return (health?.items || []).map((item) => ({
    id: String(item.id || ''),
    name: String(item.name || item.id || ''),
    level: String(item.level || 'unknown'),
    style: statusStyle(item.level),
    detail: String(item.detail || ''),
    suggestion: String(item.suggestion || ''),
  }));
}

export function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

/** 盈亏块 → 下钻条目（T-FE-03：数字 → 来源链；金额原样展示，不重算） */
export function pnlDrillEntries(pnl: PnlBlock | null | undefined): DrillEntryLike[] {
  if (!pnl || !pnl.available) {
    return [{ label: '状态', value: '无资金快照', source: pnl?.source, hint: pnl?.detail }];
  }
  const summary = pnlSummary(pnl);
  return [
    { label: '总资产', value: formatMoney(pnl.total_asset), source: pnl.source },
    { label: '初始本金', value: formatMoney(pnl.initial_capital), source: pnl.source },
    { label: '累计收益', value: formatMoney(pnl.total_pnl), source: pnl.source, hint: summary.returnPct !== null ? `收益率 ${formatPct(summary.returnPct)}（累计收益 ÷ 初始本金）` : '本金缺失，不计算收益率' },
    { label: '今日盈亏', value: formatMoney(pnl.today_pnl), source: pnl.source },
    { label: '持仓市值', value: formatMoney(pnl.market_value), source: pnl.source },
    { label: '快照日期', value: pnl.snapshot_date || '—', source: pnl.source },
  ];
}

/** 计划块 → 下钻条目（顶部摘要 + 前 N 笔明细） */
export function planDrillEntries(plan: PlanBlock | null | undefined, topN = 10): DrillEntryLike[] {
  if (!plan?.available) {
    return [{ label: '状态', value: plan?.reason || '不可用', source: plan?.source }];
  }
  const summary = planSummary(plan);
  const entries: DrillEntryLike[] = [
    { label: '策略', value: `${plan.strategy_name || plan.strategy_id || '—'}（${plan.mode || 'SIMULATION'}）`, source: plan.source },
    { label: '信号数', value: String(plan.signal_count ?? '—'), source: 'db:engine_signal_scores（同执行路径）' },
    { label: '计划笔数', value: `${plan.order_count ?? summary.buys.length + summary.sells.length}（买 ${summary.buys.length} / 卖 ${summary.sells.length}${summary.exits ? ` · 退出规则 ${summary.exits}` : ''}）`, source: plan.source },
    { label: '买入金额（预估）', value: formatMoney(summary.buyAmount), source: plan.source },
    { label: '卖出金额（预估）', value: formatMoney(summary.sellAmount), source: plan.source },
    { label: '预演错误', value: plan.error || '无', source: plan.source },
  ];
  for (const order of (plan.orders || []).slice(0, topN)) {
    entries.push({
      label: `${order.side === 'BUY' ? '买' : '卖'} ${order.symbol}`,
      value: `${order.quantity} 股 @ ${order.price}（约 ${formatMoney(order.estimated_amount)}）`,
      source: planKindLabel(order.kind),
      hint: order.reason,
    });
  }
  return entries;
}

export interface ExecuteSelection {
  /** 可勾选排除的调仓单（index 为在 orders 中的下标） */
  selectable: Array<{ index: number; order: PlanOrder }>;
  /** 锁定不可排除的退出规则单（风控退出不可被人工绕过） */
  locked: Array<{ index: number; order: PlanOrder }>;
  /** 当前将被执行的单数 */
  executableCount: number;
}

/** 一键执行选择模型（纯函数）：退出规则单锁定；其余可逐笔勾选排除 */
export function buildExecuteSelection(
  plan: PlanBlock | null | undefined,
  excludedIndexes: Set<number>
): ExecuteSelection {
  const orders = plan?.orders || [];
  const selectable: ExecuteSelection['selectable'] = [];
  const locked: ExecuteSelection['locked'] = [];
  orders.forEach((order, index) => {
    if (order.kind === 'exit') {
      locked.push({ index, order });
    } else {
      selectable.push({ index, order });
    }
  });
  const executableCount = orders.filter(
    (order, index) => order.kind === 'exit' || !excludedIndexes.has(index)
  ).length;
  return { selectable, locked, executableCount };
}

/** 勾选下标 → 排除标的（服务端按 symbol 归一匹配裸码/后缀） */
export function excludedSymbolsFromPlan(
  plan: PlanBlock | null | undefined,
  excludedIndexes: Set<number>
): string[] {
  const orders = plan?.orders || [];
  return orders
    .filter((order, index) => excludedIndexes.has(index) && order.kind !== 'exit')
    .map((order) => order.symbol);
}

export interface EvidenceSummary {
  ok: number;
  warn: number;
  fail: number;
  noEvidence: number;
  gapLabels: string[];
}

/** 证据矩阵汇总（纯函数）：各态计数 + 无证据环名单（前端首行横幅用） */
export function evidenceSummary(rings: import('./types').EvidenceRing[] | null | undefined): EvidenceSummary {
  const list = rings || [];
  const out: EvidenceSummary = { ok: 0, warn: 0, fail: 0, noEvidence: 0, gapLabels: [] };
  for (const ring of list) {
    if (ring.level === 'ok') out.ok += 1;
    else if (ring.level === 'warn') out.warn += 1;
    else if (ring.level === 'fail') out.fail += 1;
    else {
      out.noEvidence += 1;
      out.gapLabels.push(ring.label);
    }
  }
  return out;
}

/** 证据环 → 下钻条目（T-FE-16：每格下钻至证据项原文/来源） */
export function evidenceRingDrillEntries(
  ring: import('./types').EvidenceRing | null | undefined
): Array<{ label: string; value: string; source?: string; hint?: string }> {
  if (!ring) return [];
  const head = [
    { label: '环节', value: ring.label },
    { label: '状态', value: statusStyle(ring.level).label },
    { label: '证据产物', value: `${ring.artifact}（${ring.frequency}）` },
  ];
  const items = (ring.items || []).map((item) => ({
    label: `${item.name}（${item.id}）`,
    value: statusStyle(item.level).label,
    source: item.source,
    hint: [item.detail, item.suggestion ? `建议：${item.suggestion}` : ''].filter(Boolean).join(' — '),
  }));
  return [...head, ...items];
}
