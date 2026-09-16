/** 今日交易台纯函数（展示模型；可单测，无副作用） */

import type {
  ExecutionBlock,
  HealthBlock,
  PipelineStep,
  PlanBlock,
  PlanOrder,
  PnlBlock,
} from './types';

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
