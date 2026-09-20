/** 今日交易台纯函数（展示模型；可单测，无副作用） */

import type {
  EvidenceRing,
  ExecutionBlock,
  ExecutionItem,
  HealthBlock,
  PipelineStep,
  PlanBlock,
  PlanOrder,
  PnlBlock,
  SignalItem,
  SignalsBlock,
} from './types';
import type { DrillLevelSpec } from '../shared/DrillDownDrawer';

export interface DrillEntryLike {
  label: string;
  value: string;
  source?: string;
  hint?: string;
  /** 逐层穿透（T-FE-03 v2）：该条目的下一层 */
  drill?: DrillLevelSpec;
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

/**
 * 委托块「不可归集」原因（可用时返回 null）。
 *
 * 后端对无 market 列的市场返回 `available:false` + `reason`，且**刻意不带**
 * sim_count/filled/rejected/items。这类块必须先判此函数再渲染——否则缺字段回退 0，
 * 页面会把"这个市场归集不了委托"显示成"今天没交易（正常空态）"，即假证据。
 */
export function executionUnavailableReason(
  execution: ExecutionBlock | null | undefined,
): string | null {
  if (execution?.available !== false) return null;
  return execution.reason || `${execution.market || '该市场'}委托暂不可按市场归集`;
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
      label: `${sideLabel(order.side)} ${symbolLabel(order.symbol, order.name)}`,
      value: `${order.quantity} 股 @ ${order.price}（约 ${formatMoney(order.estimated_amount)}）`,
      source: planKindLabel(order.kind),
      hint: order.reason,
    });
  }
  return entries;
}

// ── 逐层穿透（T-FE-03 v2）：条目 → 下一层（计划单→信号→原始载荷 等）────────

function sideLabel(side: string): string {
  return String(side).toUpperCase() === 'BUY' ? '买' : '卖';
}

/** 标的展示口径（名称为主、代码辅）：有名称 → 「名称（代码）」；未收录 → 代码原样（不伪造） */
export function symbolLabel(symbol: string, name?: string | null): string {
  const n = String(name || '').trim();
  return n ? `${n}（${symbol}）` : symbol;
}

/** 取价来源 → 人话（T-P2-03 取价链的降级标记如实解释） */
export function priceSourceHint(priceSource: string | null | undefined): string {
  const key = String(priceSource || '');
  if (key === 'broker_fill') return '券商真实成交回报（实盘镜像）';
  if (key === 'local_close') return '当日本地日线收盘（QuantDB）';
  if (key === 'today_bar_close') return '当日 K 线收盘（当日分区延迟时如实标注）';
  if (key === 'prev_close_bar') return '降级：最近可用日线收盘（非当日，已标 degraded）';
  if (!key) return '未标注（历史数据或旧路径）';
  return `工程口径见原始载荷（${key}）`;
}

/** 候选信号条目 → 下钻层（字段分解 → 原始条目/信号块） */
export function signalItemDrillEntries(
  item: SignalItem,
  signals: SignalsBlock | null | undefined
): DrillEntryLike[] {
  const rankText =
    item.rank_pct === null || item.rank_pct === undefined ? '—' : item.rank_pct.toFixed(3);
  return [
    { label: '标的', value: symbolLabel(item.symbol, item.name) },
    { label: '方向', value: sideLabel(item.side) },
    {
      label: 'rank_pct（当日分位）',
      value: rankText,
      hint: '0=全市场最弱、1=最强；阈值按当日分布分位自适应（不硬编码）',
    },
    { label: '模型分', value: item.score === null || item.score === undefined ? '—' : item.score.toFixed(4) },
    { label: '交易日', value: signals?.trade_date || '—', source: signals?.source },
    {
      label: '原始条目（该标的全部列）',
      value: '展开',
      drill: {
        title: `信号原始条目 · ${symbolLabel(item.symbol, item.name)}`,
        subtitle: 'engine_signal_scores 行原样（含市场/来源/时间戳）',
        entries: Object.entries(item as unknown as Record<string, unknown>).map(([k, v]) => ({
          label: k,
          value: v === null || v === undefined ? '—' : String(v),
        })),
        raw: item,
      },
    },
    {
      label: '当日全体分布',
      value: `BUY ${signals?.buy ?? '—'} / SELL ${signals?.sell ?? '—'} / HOLD ${signals?.hold ?? '—'}`,
      drill: {
        title: '信号块原始载荷',
        subtitle: '当日信号聚合（与扫描器/选股同源）',
        entries: [
          { label: '交易日', value: signals?.trade_date || '—' },
          { label: '市场', value: signals?.market || 'CN' },
          { label: '来源', value: signals?.source || '—' },
        ],
        raw: signals,
      },
    },
  ];
}

/** 今日执行条目 → 下钻层（订单字段 → 取价来源说明 → 原始条目） */
export function executionItemDrillEntries(
  item: ExecutionItem,
  execution: ExecutionBlock | null | undefined
): DrillEntryLike[] {
  return [
    { label: '模式', value: item.mode === 'REAL' ? '实盘（镜像）' : '模拟盘' },
    { label: '标的/方向', value: `${sideLabel(item.side)} ${symbolLabel(item.symbol, item.name)}` },
    { label: '数量', value: `${item.quantity} 股` },
    { label: '状态', value: item.status },
    {
      label: '取价来源',
      value: item.price_source || '—',
      hint: priceSourceHint(item.price_source),
      drill: {
        title: '取价来源 · 工程口径',
        subtitle: 'T-P2-03 取价链唯一实现（L0/L1 新鲜价优先，降级如实标注）',
        entries: [
          { label: '本单标记', value: item.price_source || '—' },
          { label: '含义', value: priceSourceHint(item.price_source) },
          { label: '降级规则', value: '[RULE:PRICE-STALE]（陈旧价拒单/标注）' },
        ],
        raw: { price_source: item.price_source, symbol: item.symbol, client_order_id: item.client_order_id },
      },
    },
    { label: '订单 ID', value: item.client_order_id || '—', source: execution?.source },
    {
      label: '原始条目',
      value: '展开',
      drill: {
        title: `执行原始条目 · ${symbolLabel(item.symbol, item.name)}`,
        subtitle: 'sim_orders/trades 投影行（含 reason/时间）',
        entries: Object.entries(item as unknown as Record<string, unknown>).map(([k, v]) => ({
          label: k,
          value: v === null || v === undefined ? '—' : String(v),
        })),
        raw: item,
      },
    },
  ];
}

/** 计划单 → 下钻层（字段分解 → 触发类别说明 / 对应当日信号 / 原始条目） */
export function planOrderDrillEntries(
  order: PlanOrder,
  plan: PlanBlock | null | undefined,
  signals?: SignalsBlock | null
): DrillEntryLike[] {
  const isExit = order.kind === 'exit';
  const matchedSignal = (signals?.top_buy || []).find((s) => s.symbol === order.symbol);
  const entries: DrillEntryLike[] = [
    { label: '标的/方向', value: `${sideLabel(order.side)} ${symbolLabel(order.symbol, order.name)}` },
    { label: '计划数量', value: `${order.quantity} 股` },
    { label: '计划价格', value: String(order.price), hint: '预演取价；真实成交以执行时行情为准' },
    {
      label: '预估金额',
      value: formatMoney(order.estimated_amount),
      hint: `计算式：${order.price} × ${order.quantity}`,
    },
    {
      label: '触发类别',
      value: isExit ? '退出规则（风控）' : '定期调仓',
      source: plan?.source,
      drill: {
        title: isExit ? '退出规则 · 为什么风控不可绕过' : '定期调仓 · 人工可调范围',
        subtitle: '与执行同一 RebalanceCalculator（T-P2-04 退出规则单实现）',
        entries: isExit
          ? [
              { label: '触发', value: '止损/止盈等退出规则命中持仓', hint: order.reason },
              { label: '人工权限', value: '不可排除、不可改量（风控动作不绕过）' },
              { label: '规则来源', value: 'shared/exit_rules.py（唯一实现）' },
            ]
          : [
              { label: '来源', value: 'TopK 偏离目标权重触发调仓', hint: order.reason },
              { label: '人工权限', value: '可勾选排除、可改量（按市场申报规则归一）' },
              { label: '计算器', value: 'RebalanceCalculator（与执行/预演同源）' },
            ],
        raw: { kind: order.kind, reason: order.reason, symbol: order.symbol, side: order.side },
      },
    },
    { label: '理由原文', value: order.reason || '—' },
    {
      label: '涨跌停/停牌',
      value: order.is_suspended ? '停牌' : order.is_limit_up ? '涨停' : order.is_limit_down ? '跌停' : '正常',
    },
    {
      label: '计划单原始条目',
      value: '展开',
      drill: {
        title: `计划单原始条目 · ${symbolLabel(order.symbol, order.name)}`,
        subtitle: 'dry-run 引擎输出（未执行，字段原样）',
        entries: Object.entries(order as unknown as Record<string, unknown>).map(([k, v]) => ({
          label: k,
          value: v === null || v === undefined ? '—' : String(v),
        })),
        raw: order,
      },
    },
  ];
  if (matchedSignal) {
    entries.push({
      label: '对应当日信号',
      value: `rank_pct ${matchedSignal.rank_pct?.toFixed(3) ?? '—'} · 分 ${matchedSignal.score?.toFixed(4) ?? '—'}`,
      hint: '该标的同时在当日候选信号内——可继续下钻信号来源',
      drill: {
        title: `信号 · ${symbolLabel(order.symbol, order.name)}`,
        subtitle: '当日候选信号条目（engine_signal_scores）',
        entries: signalItemDrillEntries(matchedSignal, signals),
        raw: matchedSignal,
      },
    });
  }
  return entries;
}

/** 管线步骤 → 下钻层（步骤详情 → 同源体检断言证据环 → 逐证据项） */
export function pipelineStepDrillEntries(
  step: PipelineStep,
  evidence?: { rings?: EvidenceRing[] } | null
): DrillEntryLike[] {
  const entries: DrillEntryLike[] = [
    { label: '步骤', value: step.label },
    { label: '状态', value: statusStyle(step.status).label },
    { label: '明细', value: step.detail || '—' },
    { label: '来源', value: step.source },
  ];
  // 步骤 ← 体检断言的同源映射（C08/C02/C01/C05）；找到含该断言的证据环即挂下一层
  const checkId = String(step.source || '').split(':').pop() || '';
  const ring = (evidence?.rings || []).find((r) => (r.items || []).some((i) => i.id === checkId));
  if (ring) {
    entries.push({
      label: '对应证据环',
      value: `${ring.label}（${statusStyle(ring.level).label}）`,
      source: ring.artifact,
      drill: {
        title: `证据环 · ${ring.label}`,
        subtitle: `${ring.artifact} · ${ring.frequency}——每项可核对来源`,
        entries: evidenceRingDrillEntries(ring),
        raw: ring,
      },
    });
  }
  return entries;
}

export interface ExecuteSelection {  /** 可勾选排除的调仓单（index 为在 orders 中的下标） */
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

/** 人工改量（T-FE-05 v2）：改量输入与计划数量不同时收录为服务端载荷（纯函数） */
export interface QuantityOverride {
  symbol: string;
  side: string;
  quantity: number;
}

/** 改量输入（下标 → 数量）是否合法：正整数 */
export function isValidQuantityInput(value: number | null | undefined): boolean {
  const n = Number(value);
  return Number.isFinite(n) && Number.isInteger(n) && n > 0;
}

/**
 * 改量下标 → 服务端载荷（纯函数）：
 * 仅收录「非退出规则单 + 与计划数量不同 + 合法正整数」的条目；退出单恒不收（风控不绕过）。
 */
export function quantityOverridesFromPlan(
  plan: PlanBlock | null | undefined,
  quantityEdits: Map<number, number>
): QuantityOverride[] {
  const orders = plan?.orders || [];
  const out: QuantityOverride[] = [];
  orders.forEach((order, index) => {
    if (order.kind === 'exit') return;
    if (!quantityEdits.has(index)) return;
    const qty = quantityEdits.get(index) as number;
    if (!isValidQuantityInput(qty)) return;
    if (qty === order.quantity) return;
    out.push({ symbol: order.symbol, side: order.side, quantity: Math.floor(qty) });
  });
  return out;
}

/** 是否存在非法改量输入（用于确认按钮门禁：资金相关调整不做静默降级） */
export function hasInvalidQuantityEdit(
  plan: PlanBlock | null | undefined,
  quantityEdits: Map<number, number>
): boolean {
  const orders = plan?.orders || [];
  for (const [index, qty] of quantityEdits) {
    if (index < 0 || index >= orders.length) continue;
    if (orders[index].kind === 'exit') continue;
    if (!isValidQuantityInput(qty)) return true;
  }
  return false;
}

/**
 * 执行报告的改量裁定 → 人话摘要（纯函数）：applied/ignored 逐条如实，未应用附原因。
 */
export function quantityAdjustmentSummary(report: unknown): {
  applied: string[];
  ignored: string[];
} {
  const adjustments = (report as { quantity_adjustments?: unknown })?.quantity_adjustments;
  const applied: string[] = [];
  const ignored: string[] = [];
  if (!Array.isArray(adjustments)) return { applied, ignored };
  for (const item of adjustments) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const label = `${String(rec.symbol ?? '')} ${String(rec.side ?? '') === 'SELL' ? '卖' : '买'}`;
    if (rec.applied === true) {
      applied.push(`${label} ${String(rec.from ?? '?')} → ${String(rec.to ?? '?')}`);
    } else {
      ignored.push(`${label}（申请 ${String(rec.requested ?? '?')}）：${String(rec.reason ?? '未应用')}`);
    }
  }
  return { applied, ignored };
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
