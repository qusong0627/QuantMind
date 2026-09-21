/** 个股终端跨市场共用的小工具：代码格式归一与数值格式化 */

import { SIGNAL_POSITION_HINT, signalPositionLabel } from '../shared/signalVocabulary';

/** suffix(600519.SH) -> prefix(SH600519)，自选表用 prefix 格式；无后缀时原样返回（美股 ticker 即此情形） */
export function toPrefix(symbol: string): string {
  const [code, ex] = symbol.split('.');
  return ex && code ? `${ex}${code}` : symbol;
}

/** 涨跌幅格式化：+1.23% */
export function fmtPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

/** 市值格式化：亿元口径（A 股/港股）；美股请用 fmtCapUsd */
export function fmtMv(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  if (v >= 10000) return `${(v / 10000).toFixed(1)}万亿`;
  return `${v.toFixed(0)}亿`;
}

/** 市值格式化：美元（美股 f10 的 market_cap 是原始美元值） */
export function fmtCapUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--';
  const abs = Math.abs(v);
  if (abs >= 1e12) return `$${(v / 1e12).toFixed(2)}万亿`;
  if (abs >= 1e8) return `$${(v / 1e8).toFixed(0)}亿`;
  if (abs >= 1e4) return `$${(v / 1e4).toFixed(1)}万`;
  return `$${v.toFixed(0)}`;
}

/** 交易标记 → 下钻条目（T-FE-08：买卖点点击的来源链；缺失字段如实「—」，不推断） */
export function tradeMarkerDrillEntries(marker: {
  date: string;
  side: 'buy' | 'sell';
  price: number;
  shares: number;
  reason?: string;
  order_id?: string;
  amount?: number;
  fee?: number;
}): Array<{ label: string; value: string; source?: string; hint?: string }> {
  const entries = [
    { label: '方向', value: marker.side === 'buy' ? '买入' : '卖出' },
    { label: '日期', value: marker.date || '—' },
    { label: '成交价', value: Number(marker.price || 0).toFixed(2) },
    { label: '数量（股）', value: String(marker.shares ?? '—') },
    {
      label: '成交金额',
      value: marker.amount !== undefined ? Number(marker.amount).toFixed(2) : '—',
    },
    {
      label: '费用',
      value: marker.fee !== undefined ? Number(marker.fee).toFixed(2) : '—',
      source: 'sim_trades.total_fee（佣金+印花税+过户费）',
    },
    {
      label: '理由（下单备注）',
      value: marker.reason || '—',
      hint: marker.reason ? undefined : '该笔成交未带备注（历史单或人工单）',
    },
    {
      label: '订单号',
      value: marker.order_id || '—',
      source: 'sim_trades.order_id ⋈ sim_orders',
    },
  ];
  return entries;
}

/**
 * 信号标记 → 下钻条目（T-FE-08：信号三角点击；与分数副图同一模型/同一来源）。
 *
 * 「方向」而不是「买入/卖出信号」：模型判定的是该标的在当日截面里的位置，
 * 说成买卖信号就成了替用户做决定，见 `features/shared/signalVocabulary.ts`。
 */
export function signalPointDrillEntries(signal: {
  date: string;
  side: string;
  fusion: number | null;
}): Array<{ label: string; value: string; source?: string; hint?: string }> {
  return [
    {
      label: '相对位置',
      value: signalPositionLabel(signal.side),
      source: 'engine_signal_scores.signal_side',
      hint: SIGNAL_POSITION_HINT,
    },
    { label: '日期', value: signal.date || '—' },
    {
      label: '推理分数',
      value: signal.fusion === null || signal.fusion === undefined ? '—' : Number(signal.fusion).toFixed(6),
      source: 'engine_signal_scores.fusion_score（与分数副图同源）',
      hint: '分数仅同日内排序有效；跨模型不可比，阈值按 rank_pct 分位口径',
    },
  ];
}

// ── 详情页签的简单/专业降升维（T-FE-02 收尾）──────────────────────────

export interface DetailTabDef<T extends string = string> {
  id: T;
  label: string;
  /** 机构级页签：简单模式收起（专业模式全部展示） */
  proOnly?: boolean;
}

/** 按模式过滤页签（纯函数）：简单模式收起 proOnly；专业模式全量 */
export function visibleDetailTabs<T extends string>(
  tabs: DetailTabDef<T>[],
  isSimple: boolean
): DetailTabDef<T>[] {
  return isSimple ? tabs.filter((t) => !t.proOnly) : tabs;
}

/** 当前页签被收起时的回落目标（第一个可见页签；无可见则保持原值） */
export function fallbackDetailTab<T extends string>(
  current: T,
  tabs: DetailTabDef<T>[],
  isSimple: boolean
): T {
  const visible = visibleDetailTabs(tabs, isSimple);
  if (visible.some((t) => t.id === current)) return current;
  return visible[0]?.id ?? current;
}
