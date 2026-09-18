/** 副驾驶面板展示模型（纯函数，可单测）：事件流/建议卡/指标 → 展示结构。 */

export type Severity = 'info' | 'warn' | 'critical' | string;

export interface CopilotEvent {
  alert_id: string;
  ts: string;
  alert_type: string;
  severity: Severity;
  market: string;
  symbol: string;
  title: string;
  targets?: string[];
  pushed?: boolean;
  outcome_status?: string;
  hit?: boolean | null;
  annotation?: string | null;
}

export interface CopilotAdviceAction {
  symbol: string;
  side: string;
  quantity: number;
  order_type?: string;
  price?: number | null;
}

export interface CopilotAdvice {
  advice_id: string;
  source: string;
  title: string;
  rationale: string;
  actions: CopilotAdviceAction[];
  context_refs?: Record<string, unknown>;
  status: string;
  created_at: string;
  execution?: Array<{ symbol: string; side: string; success: boolean; message?: string; duplicate?: boolean }> | null;
  /** 兑现结果（决策日收盘→T+h 收盘，超额 vs 沪深300；次日凌晨回填） */
  outcome?: {
    base_date?: string;
    benchmark?: string;
    summary?: Record<string, { n?: number; hits?: number; avg_excess?: number | null }>;
  } | null;
  outcome_status?: string;
}

export interface AdviceStatsHorizon {
  n: number;
  hits: number;
  hit_rate: number | null;
  avg_excess: number | null;
}

export interface AdviceStats {
  available?: boolean;
  days?: number;
  total?: number;
  decided?: number;
  executed?: number;
  rejected?: number;
  decide_rate?: number | null;
  scored?: number;
  by_horizon?: Record<string, AdviceStatsHorizon>;
  source?: string;
}

export interface CopilotPanel {
  as_of?: string;
  events?: { available?: boolean; items?: CopilotEvent[]; reason?: string; source?: string };
  latency?: {
    available?: boolean;
    /** 展示档（_fresh 口径，行情到达时延） */
    display?: Record<string, number | null> | null;
    display_stage?: string;
    market_snapshot_bridge_fresh?: Record<string, number | null> | null;
    market_snapshot_fresh?: Record<string, number | null> | null;
    market_snapshot?: Record<string, number | null> | null;
    reason?: string;
  };
  budget?: { available?: boolean; detail?: Record<string, unknown> | null; reason?: string };
  miss_rate?: { available?: boolean; miss_rate?: number | null; filled?: number; hit?: number; reason?: string };
}

/** 严重度 → 色调 + 中文 */
export function severityMeta(severity: Severity): { tone: 'red' | 'amber' | 'slate'; label: string } {
  if (severity === 'critical') return { tone: 'red', label: '严重' };
  if (severity === 'warn') return { tone: 'amber', label: '关注' };
  return { tone: 'slate', label: '提示' };
}

/** 告警类型（news:risk_event 形态）→ 中文标签 */
export function alertTypeLabel(alertType: string): string {
  const map: Record<string, string> = {
    'news:risk_event': '新闻风险',
    'news:negative': '新闻利空',
    'news:positive': '新闻利好',
    'news:sentiment_spike': '情绪突变',
    'anomaly:volume_surge': '异常放量',
    'anomaly:price_limit_up': '涨停异动',
    'anomaly:price_limit_down': '跌停异动',
    'anomaly:price_surge': '大幅波动',
    'anomaly:account_cancel_ratio': '撤单异常',
    'anomaly:account_concentration': '集中度偏高',
    'anomaly:data_jump': '数据跳变',
    'anomaly:data_gap': '数据缺口',
    'anomaly:model_ic_drop': '模型 IC 异常',
    regime: '市场状态',
  };
  return map[alertType] || alertType;
}

/** T+1 结果 → 展示 */
export function outcomeMeta(event: CopilotEvent): { label: string; tone: 'green' | 'red' | 'slate' } {
  if (event.annotation === 'true_positive') return { label: '标注:真实', tone: 'green' };
  if (event.annotation === 'false_positive') return { label: '标注:误报', tone: 'red' };
  if (event.outcome_status === 'filled' && event.hit === true) return { label: 'T+1 命中', tone: 'green' };
  if (event.outcome_status === 'filled' && event.hit === false) return { label: 'T+1 未中', tone: 'red' };
  if (event.outcome_status === 'no_data') return { label: '无数据', tone: 'slate' };
  if (event.outcome_status === 'not_scorable') return { label: '不可评分', tone: 'slate' };
  return { label: '待回填', tone: 'slate' };
}

/** 建议动作 → 一行文案（买入 600036.SH ×900 / 限价 10.50） */
export function actionLine(action: CopilotAdviceAction): string {
  const side = action.side === 'buy' ? '买入' : action.side === 'sell' ? '卖出' : action.side;
  const price = action.order_type === 'limit' && action.price ? `  限价 ${action.price}` : '  市价';
  return `${side} ${action.symbol} × ${action.quantity}${price}`;
}

export function adviceStatusMeta(status: string): { label: string; tone: 'blue' | 'green' | 'red' | 'amber' | 'slate' } {
  const map: Record<string, { label: string; tone: 'blue' | 'green' | 'red' | 'amber' | 'slate' }> = {
    pending: { label: '待决', tone: 'blue' },
    executed: { label: '已执行', tone: 'green' },
    partial: { label: '部分执行', tone: 'amber' },
    failed: { label: '执行失败', tone: 'red' },
    rejected: { label: '已拒绝', tone: 'slate' },
  };
  return map[status] || { label: status, tone: 'slate' };
}

/** 时延自适应单位：<1s 毫秒 / <60s 秒 / 以上分钟（15min 级数值是水印陈旧告警而非格式化问题） */
function formatLatency(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}min`;
}

/** 面板指标摘要（缺失如实 —） */
export function panelMetrics(panel: CopilotPanel | null): {
  latencyP95: string;
  latencyP95Ms: number | null;
  missRate: string;
  events: number;
  budgetText: string;
} {
  if (!panel) return { latencyP95: '—', latencyP95Ms: null, missRate: '—', events: 0, budgetText: '—' };
  const p95 =
    panel.latency?.display?.p95_ms ?? panel.latency?.market_snapshot?.p95_ms;
  const miss = panel.miss_rate?.miss_rate;
  const detail = panel.budget?.detail as Record<string, unknown> | null | undefined;
  const budgetText = detail
    ? `tdx ${String(detail.tdx_rss_mb ?? '-')}MB · 引擎 ${String(detail.engine_rss_mb ?? '-')}MB`
    : '—';
  return {
    latencyP95: typeof p95 === 'number' ? formatLatency(p95) : '—',
    latencyP95Ms: typeof p95 === 'number' ? p95 : null,
    missRate: typeof miss === 'number' ? `${(miss * 100).toFixed(1)}%` : '—',
    events: panel.events?.items?.length ?? 0,
    budgetText,
  };
}

function _signedPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
  const p = Number(v) * 100;
  return `${p >= 0 ? '+' : ''}${p.toFixed(digits)}%`;
}

/** 建议战绩一行（近 N 天）：发出/已决（执行/拒绝）+ T+1 胜率与平均超额；无兑现如实标注。 */
export function adviceStatsLine(stats: AdviceStats | null): string {
  if (!stats || stats.available === false) return '建议战绩：暂不可用';
  const total = stats.total ?? 0;
  if (!total) return '暂无建议卡';
  const head = `近 ${stats.days ?? 90} 天：发出 ${total} · 已决 ${stats.decided ?? 0}（执行 ${stats.executed ?? 0} / 拒绝 ${stats.rejected ?? 0}）`;
  const t1 = stats.by_horizon?.['1'];
  if (!stats.scored || !t1 || !t1.n) {
    return `${head} · 兑现回填次日凌晨产出`;
  }
  const rate =
    t1.hit_rate === null || t1.hit_rate === undefined
      ? '—'
      : `${Math.round(t1.hit_rate * 100)}%`;
  return `${head} · T+1 胜率 ${rate}（n=${t1.n}） · 平均超额 ${_signedPct(t1.avg_excess)}`;
}

/** 单卡兑现一行（决策日收盘口径）；无 outcome → 空串。 */
export function adviceOutcomeText(item: CopilotAdvice): string {
  const summary = item.outcome?.summary;
  if (!summary) return '';
  const parts: string[] = [];
  for (const h of ['1', '3', '5']) {
    const s = summary[h];
    if (!s || !s.n) continue;
    parts.push(`T+${h} ${_signedPct(s.avg_excess)}（${s.hits ?? 0}/${s.n} 命中）`);
  }
  return parts.join(' · ');
}

/** 单卡兑现色调：T+1 平均超额 >0 → good，<0 → bad，无数据 → flat。 */
export function adviceOutcomeTone(item: CopilotAdvice): 'good' | 'bad' | 'flat' {
  const ex = item.outcome?.summary?.['1']?.avg_excess;
  if (ex === null || ex === undefined || !Number.isFinite(Number(ex))) return 'flat';
  return Number(ex) > 0 ? 'good' : 'bad';
}
