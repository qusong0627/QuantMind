/**
 * 策略控制台「交易记录」一行的展示模型（纯函数，可单测）。
 *
 * 一行要回答机构交易台的四个问题：**成了多少**（部分成交必须一眼可见）、
 * **什么价**（委托价 → 成交均价的差就是滑点直觉）、**多少钱**（成交额 + 手续费，
 * 手续费是策略净收益的直接扣减）、**为什么是这个状态**（撤单原因在后端被追加在
 * remarks 里，不取出来用户就只看到「已撤」两个字）。
 *
 * 无名称时不编造：回落显示代码，由界面决定怎么呈现。
 */

import type { Order } from '../../../../../services/realTradingService';

export type OrderStatusTone = 'ok' | 'warn' | 'bad' | 'pending' | 'muted';

export interface OrderRowView {
  isBuy: boolean;
  /** 交易代码（原样，已含市场后缀） */
  code: string;
  /** 股票名称；后端查不到时为空串，界面回落显示 code */
  name: string;
  statusLabel: string;
  statusTone: OrderStatusTone;
  /** 「300 / 1000 股」（部分成交）或「500 股」 */
  qtyText: string;
  partial: boolean;
  /** 「18.21 → 18.18」；未成交时只有委托价 */
  priceText: string;
  amountText: string;
  amountLabel: '成交额' | '委托额';
  /** 手续费；无此字段时为空串（0 与「没有这个数」分得开） */
  feeText: string;
  timeText: string;
  timeLabel: '成交' | '委托';
  /** 备注/撤单原因（已剥掉后端加的 [CANCELLED: …] 外壳） */
  note: string;
}

const STATUS: Record<string, { label: string; tone: OrderStatusTone }> = {
  filled: { label: '已成', tone: 'ok' },
  partial_filled: { label: '部成', tone: 'warn' },
  partially_filled: { label: '部成', tone: 'warn' },
  cancelled: { label: '已撤', tone: 'muted' },
  canceled: { label: '已撤', tone: 'muted' },
  rejected: { label: '已拒绝', tone: 'bad' },
  submitted: { label: '待成交', tone: 'pending' },
  pending: { label: '待成交', tone: 'pending' },
  new: { label: '待成交', tone: 'pending' },
};

/** 备注外壳：后端撤单时把原因追加成 `[CANCELLED: xxx]` / `[Cancelled: xxx]` */
const NOTE_WRAPPER = /^\[(?:cancelled|canceled|cancel)[:：]\s*([\s\S]*?)\]$/i;

const money = (value: unknown): string => {
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

const price = (value: unknown): string => {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n.toFixed(2) : '';
};

function formatTime(value?: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value).slice(11, 16) || '—';
  const p = (x: number) => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function orderRowView(order: Order): OrderRowView {
  const rawStatus = String(order.status || '').toLowerCase();
  const status = STATUS[rawStatus] ?? { label: order.status || '-', tone: 'muted' as OrderStatusTone };

  const code = String(order.symbol || '');
  const rawName = String(order.symbol_name || '').trim();
  // 后端历史上有把 symbol 塞进 symbol_name 的写法；那不算名称，否则界面会重复显示代码
  const name = rawName && rawName !== code ? rawName : '';

  const qty = Number(order.quantity) || 0;
  const filled = Number(order.filled_quantity) || 0;
  const partial = filled > 0 && qty > 0 && filled < qty;
  const qtyText = partial ? `${filled} / ${qty} 股` : `${filled || qty} 股`;

  const orderPrice = price(order.price);
  const avgPrice = filled > 0 ? price(order.average_price) : '';
  const priceText = orderPrice && avgPrice
    ? `${orderPrice} → ${avgPrice}`
    : avgPrice || orderPrice;

  // 成交额只在真有成交时取 filled_value；否则给的是委托额，必须如实标注，
  // 否则一张被拒的单在界面上会显示成「成交 ¥9,105」。
  const hasFill = filled > 0;
  const amount = Number(hasFill ? order.filled_value : order.order_value) || 0;
  const amountLabel: OrderRowView['amountLabel'] = hasFill ? '成交额' : '委托额';

  const feeText = order.commission != null ? money(order.commission) : '';

  const showFilledTime = rawStatus === 'filled' || rawStatus.includes('partial');
  const timeLabel: OrderRowView['timeLabel'] = showFilledTime ? '成交' : '委托';
  const timeText = formatTime(showFilledTime ? order.filled_at : order.created_at);

  const note = String(order.remarks || '').trim().replace(NOTE_WRAPPER, '$1').trim();

  return {
    isBuy: String(order.side || '').toLowerCase() === 'buy',
    code,
    name,
    statusLabel: status.label,
    statusTone: status.tone,
    qtyText,
    partial,
    priceText,
    // 金额为 0 = 这张单没有金额可谈（未成交且未计价），显示 ¥0.00 只是噪音
    amountText: amount > 0 ? money(amount) : '',
    amountLabel,
    feeText,
    timeText,
    timeLabel,
    note,
  };
}
