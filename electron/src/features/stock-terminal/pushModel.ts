/**
 * 候选信号「多选 → 一键推送」的展示口径 · 纯逻辑（与渲染分离，便于单测盯口径）。
 *
 * 确认面板上「显示错了也不报错」的地方集中在这里，逐条单测：
 *
 * 1. **阻断的话必须说出来**。服务端逐笔给 `executable=false` + `problem`；前端若只把
 *    行灰掉，用户点不动确认按钮却不知道哪一笔、为什么。
 * 2. **镜像回执里只有 `success` 是成功**。`skipped`（压根没发）/ `queued`（还没发）/
 *    `duplicate`（早就发过了）三者与成功在界面上必须可区分 —— 这条链最后会动真钱。
 * 3. **改量是本地输入**，非法输入必须把确认按钮按住（不能等到服务端 400 才发现）。
 *    合法改量后金额、汇总都要跟着变，否则「预计金额」与逐笔数字对不上。
 * 4. **环境闸与标的闸分开**。`l0.*`（时段/急停/时钟/配置）是对全场的结论，写进某一只
 *    票的风险栏会让用户去查一只根本不该查的票。
 */

import type {
  PushBudget,
  PushChannel,
  PushExecute,
  PushLeg,
  PushLegResult,
  PushMirrorPlan,
} from '../stock-terminal-shared/types';

/** 与后端 `push_orders.MAX_BATCH_SYMBOLS` 同口径：再多就该分两批（每笔一次风控，很慢） */
export const MAX_PICK = 50;
/** 与后端 `push_orders.MAX_QUANTITY` 同口径 */
export const MAX_QUANTITY = 1_000_000;
/** 实盘硬确认的确认词（真钱路径不做「点两下就过」） */
export const REAL_CONFIRM_WORD = '确认下单';

/** 通道选项（`real` 是叠加语义：实盘 = 模拟盘建单 + 镜像真单，不存在「只实盘」） */
export const CHANNEL_OPTIONS: { value: PushChannel; label: string; hint: string }[] = [
  { value: 'sim', label: '仅模拟盘', hint: '只写模拟盘台账，不碰真钱' },
  { value: 'real', label: '模拟盘 + 实盘', hint: '模拟盘建单 + 镜像真单（受白名单/急停/日配额四道闸约束）' },
];

export function channelLabel(channels: PushChannel[]): string {
  return channels.includes('real') ? '模拟盘 + 实盘镜像' : '仅模拟盘';
}

// ---------------------------------------------------------------------------
// 逐笔阻断
// ---------------------------------------------------------------------------

/** 阻断来源的中文短标签（`blocked_by` 是服务端的机器词） */
const BLOCKED_TAG: Record<string, string> = {
  quantity: '数量',
  list: '名单',
  risk: '风控',
};

/**
 * 阻断来源短标签；未被阻断返回空串。
 *
 * 实盘配额跳过要**先判**：`_mirror_plan` 把这类腿改成 `executable=false` 时不写
 * `blocked_by`（它只补 `problem`），照 `blocked_by` 查会落进通用「阻断」，
 * 用户就看不出这一笔是「模拟盘照常成交、真单没下发」。
 */
export function blockedTag(leg: PushLeg): string {
  if (leg.executable) return '';
  if (leg.mirror_precheck?.will_skip) return '实盘配额';
  return BLOCKED_TAG[String(leg.blocked_by || '')] ?? '阻断';
}

/**
 * 这一笔为什么点不动。优先用服务端给的 `problem`（它带理由正文），
 * 没有就退回 `blocked_by` 的通用说法 —— **不留空**，空白看起来像界面坏了。
 */
export function blockedReason(leg: PushLeg): string {
  if (leg.executable) return '';
  return String(leg.problem || '').trim() || `被${blockedTag(leg)}阻断（服务端未给原因）`;
}

/** 实盘配额试算的机器词 → 中文（用户要能看懂「第 6 只起不下发」是卡在哪一条） */
const MIRROR_REASON: Record<string, string> = {
  max_daily_orders: '日委托笔数上限',
  max_daily_symbols: '日标的只数上限',
  max_daily_value: '日委托金额上限',
  max_order_value: '单笔金额上限',
};

export function mirrorReasonText(reason: string): string {
  const key = String(reason || '').trim();
  return MIRROR_REASON[key] ?? (key || '配额不足');
}

/**
 * 实盘腿的镜像状态文案。`will_skip` 必须说成「不会下发真单」——
 * 只说「配额不足」的话，用户仍会以为模拟盘那笔成了、真单也成了。
 */
export function mirrorPrecheckText(leg: PushLeg): string {
  const p = leg.mirror_precheck;
  if (!p) return '未查询镜像配额';
  if (!p.will_skip) return '将下发真单';
  return `不会下发真单（${mirrorReasonText(p.reason)}）`;
}

/** 这一笔是不是「实盘独有持仓直发」（没有模拟腿） */
export function isRealDirect(leg: PushLeg): boolean {
  return String(leg.exec_path ?? '') === 'real_direct';
}

/**
 * 实盘直发腿在「通道」列的文案。
 *
 * 不能沿用镜像腿的「将下发真单」：那句旁边总有一条模拟成交，而这一笔**没有**——
 * 用户按下去就是真钱出仓，模拟台账里不会有任何记录。两件事必须长得不一样。
 */
export function realDirectText(leg: PushLeg): string {
  const p = leg.mirror_precheck;
  if (p?.will_skip) return `实盘直发 · 不会下发（${mirrorReasonText(p.reason)}）`;
  return '实盘直发（不经模拟台账）';
}


// ---------------------------------------------------------------------------
// 实盘闸门三态 + 配额
// ---------------------------------------------------------------------------

/**
 * 实盘通道就绪性问题清单（**并列展示，不吞并**）。
 *
 * 哨兵是 `enabled`/`kill_switch`/`real_trading_ready`/`blocked_reason` 四个独立信号：
 * 只显示第一个命中的，用户就看不到「急停没开但券商通道没就绪」这种组合。
 */
export function mirrorPlanIssues(plan: PushMirrorPlan | null | undefined): string[] {
  if (!plan || !plan.requested) return [];
  const out: string[] = [];
  if (plan.available === false) {
    out.push(`镜像控制面不可读：${plan.reason || '未知原因'}`);
    return out;
  }
  if (plan.enabled === false) out.push(`真单镜像未开启${plan.blocked_reason ? `（${plan.blocked_reason}）` : ''}`);
  if (plan.kill_switch) out.push('镜像急停已置位：真单一律不下发');
  if (plan.blocked_reason && plan.enabled !== false) out.push(`镜像未启用：${plan.blocked_reason}`);
  if (plan.real_trading_ready === false) {
    out.push(`券商通道未就绪${plan.not_ready_reason ? `（${plan.not_ready_reason}）` : ''}`);
  }
  if (plan.trading_time === false && !plan.will_queue) out.push('当前非交易时段，真单不会下发');
  if (plan.will_queue) out.push('当前非交易时段：真单会**入队等开盘**，不是已成交');
  return out;
}

/** 今日配额余量一行（`null` = 该维未设上限） */
export function quotaLine(plan: PushMirrorPlan | null | undefined): string | null {
  if (!plan || !plan.requested || !plan.available) return null;
  const q = plan.quota ?? {};
  const parts: string[] = [];
  const sym = q.remaining_symbols;
  const ord = q.remaining_orders;
  const val = q.remaining_value;
  if (sym != null) parts.push(`标的 ${sym} 只`);
  if (ord != null) parts.push(`委托 ${ord} 笔`);
  if (val != null) parts.push(`金额 ¥${Number(val).toLocaleString('zh-CN')}`);
  if (!parts.length) return null;
  return `今日实盘配额剩余：${parts.join(' / ')}`;
}

// ---------------------------------------------------------------------------
// 改量
// ---------------------------------------------------------------------------

export interface QuantityParse {
  value: number | null;
  error: string;
}

/** 手填数量的解析：**只接受正整数**（小数、负数、空、超上限都各自给词） */
export function parseQuantity(raw: string): QuantityParse {
  const text = String(raw ?? '').trim();
  if (!text) return { value: null, error: '数量不能为空' };
  if (!/^\d+$/.test(text)) return { value: null, error: '数量须为正整数' };
  const n = Number(text);
  if (!Number.isFinite(n) || n <= 0) return { value: null, error: '数量须大于 0' };
  if (n > MAX_QUANTITY) return { value: null, error: `数量超出单笔上限 ${MAX_QUANTITY}` };
  return { value: n, error: '' };
}

/** 逐笔生效数量：手填优先，否则用服务端算的 */
export function effectiveQuantity(leg: PushLeg, edits: Map<string, number>): number {
  const edited = edits.get(leg.symbol);
  return edited != null ? edited : Number(leg.quantity || 0);
}

/** 逐笔生效金额（改量后金额必须跟着变，否则汇总与逐笔对不上） */
export function legAmount(leg: PushLeg, edits: Map<string, number>): number {
  const px = Number(leg.price || 0);
  return Math.round(px * effectiveQuantity(leg, edits) * 100) / 100;
}

export interface PushPanelSummary {
  /** 面板里的总行数 */
  total: number;
  /** 用户勾掉的行数 */
  deselected: number;
  /** 真的会发出去的行数（可执行 且 未被勾除） */
  willRun: number;
  /** 服务端阻断的行数（不因勾除而变） */
  blocked: number;
  /** 改量非法的行数 */
  invalid: number;
  /** 预计金额（只算会发出去的行，且按改量后的数量） */
  estAmount: number;
  /** 实盘腿里会被镜像跳过的行数 */
  mirrorSkips: number;
}

/**
 * 底部那行「本次将执行 N 笔（勾除 M）· 预计金额 ¥X」。
 *
 * 金额只累计**会发出去的**腿：把被阻断/被勾除的也算进去，用户会按一个偏大的数
 * 去做资金判断，而这笔钱实际不会动。
 */
export function panelSummary(
  legs: PushLeg[],
  deselected: Set<string>,
  edits: Map<string, number>,
  invalid: Set<string> = new Set<string>(),
): PushPanelSummary {
  let willRun = 0;
  let blocked = 0;
  let estAmount = 0;
  let mirrorSkips = 0;
  for (const leg of legs) {
    // 被镜像配额跳过的腿在服务端也是 `executable=false`（那一笔的**真单**不会下发），
    // 所以它同时进 blocked 与 mirrorSkips —— 两个数说的是两件事，都要看得见。
    if (leg.mirror_precheck?.will_skip) mirrorSkips += 1;
    if (!leg.executable) {
      blocked += 1;
      continue;
    }
    if (deselected.has(leg.symbol)) continue;
    willRun += 1;
    estAmount += legAmount(leg, edits);
  }
  return {
    total: legs.length,
    deselected: deselected.size,
    willRun,
    blocked,
    invalid: invalid.size,
    estAmount: Math.round(estAmount * 100) / 100,
    mirrorSkips,
  };
}

/**
 * 批次缩量提示（`meta.budget`）→ 一行给用户看的话；没缩量就是空串。
 *
 * 不缩量时**什么都不显示**：平时挂一句「本批未超资金」只会稀释真正的警告。
 * `applied=true` 但服务端没给 note 时自己拼一句兜底（宁可说得粗糙，也不能缩了量
 * 却让用户以为数量是原样算出来的）。
 */
export function budgetBanner(budget: PushBudget | null | undefined): string {
  if (!budget?.applied) return '';
  const note = String(budget.note || '').trim();
  if (note) return note;
  const factor = Number(budget.factor);
  const shown = Number.isFinite(factor) && factor > 0 ? `×${factor.toFixed(4)}` : '等比例';
  const cash = Number(budget.available_cash ?? 0);
  const planned = Number(budget.planned_amount ?? 0);
  if (cash > 0 && planned > 0) {
    return `本批自动算量合计 ¥${planned.toLocaleString('zh-CN')} 超出可用资金 ¥${cash.toLocaleString('zh-CN')}，已按 ${shown} 缩量`;
  }
  return `已按批次资金约束缩量 ${shown}`;
}

export interface PushGate {
  ok: boolean;
  why: string;
}

/**
 * 确认按钮能不能点（不能点时必须给出**为什么**，否则就是个死按钮）。
 *
 * `realAck` = 实盘硬确认词已正确输入。模拟盘不需要它：给模拟盘也套一层确认词，
 * 用户会养成闭眼打字的习惯，等真到实盘那一次也照打。
 */
export function pushGate(
  summary: PushPanelSummary,
  opts: { channels: PushChannel[]; realAck: boolean; loading?: boolean },
): PushGate {
  if (opts.loading) return { ok: false, why: '正在预检…' };
  if (summary.invalid > 0) return { ok: false, why: `有 ${summary.invalid} 笔数量不合法，修正后才能推送` };
  if (summary.willRun === 0) {
    return {
      ok: false,
      why: summary.blocked > 0
        ? `选中的 ${summary.blocked} 笔全部被阻断，没有可执行的腿`
        : '至少勾选 1 笔才能推送',
    };
  }
  if (opts.channels.includes('real') && !opts.realAck) {
    return { ok: false, why: `实盘通道需要输入确认词「${REAL_CONFIRM_WORD}」` };
  }
  return { ok: true, why: '' };
}

// ---------------------------------------------------------------------------
// 提交后的逐笔回执
// ---------------------------------------------------------------------------

export interface LegResultView {
  tone: 'ok' | 'queued' | 'dup' | 'skipped' | 'fail';
  label: string;
  detail: string;
}

/**
 * 一笔的回执。四种状态各占一个词：**`skipped`/`fail` 绝不渲染成成功**。
 *
 * `duplicate` 单列：它表示「这笔早就提交过了」（幂等键命中），钱只动了一次，
 * 但对用户来说「我点了两次」这件事必须看得见。
 */
export function legResultView(r: PushLegResult): LegResultView {
  if (String(r.exec_path ?? '') === 'real_direct') return realDirectView(r);
  if (!r.executed) {
    return { tone: 'skipped', label: '未提交', detail: String(r.skipped_reason || '预检阻断') };
  }
  if (r.duplicate) {
    return { tone: 'dup', label: '重复（未重复下单）', detail: String(r.message || '幂等键命中，未产生新委托') };
  }
  if (r.success) {
    const fill = r.fill_price != null ? `成交价 ${Number(r.fill_price).toFixed(2)}` : '';
    const qty = r.filled_quantity != null ? `${Number(r.filled_quantity)} 股` : '';
    return {
      tone: 'ok',
      label: '已成交',
      detail: [qty, fill, r.commission ? `手续费 ¥${Number(r.commission).toFixed(2)}` : ''].filter(Boolean).join(' · ') || String(r.message || ''),
    };
  }
  return { tone: 'fail', label: '失败', detail: String(r.message || '未说明原因') };
}

/**
 * 实盘直发腿的回执（`exec_path === 'real_direct'`）。
 *
 * 与模拟腿分开渲染是因为**同一个 `success` 在两个语境里不是一件事**：模拟腿的
 * `success=true` 配着一条模拟成交（「已成交」），而直发腿的 `success=true` 只说明
 * 真单**已提交到券商**，成交与否要等回报。把后者渲染成「已成交」就是对着真钱撒谎。
 */
export function realDirectView(r: PushLegResult): LegResultView {
  const d = r.real_direct;
  const cls = String(d?.class || '');
  if (!r.executed) {
    return {
      tone: 'skipped',
      label: '未提交',
      detail: String(r.skipped_reason || d?.reason || '实盘闸门未放行'),
    };
  }
  if (cls === 'success') {
    const limit = d?.limit_price != null ? `限价 ${Number(d.limit_price).toFixed(2)}` : '';
    return {
      tone: 'ok',
      label: '真单已提交',
      detail: [limit, String(r.message || '')].filter(Boolean).join(' · '),
    };
  }
  if (cls === 'queued') {
    return { tone: 'queued', label: '真单排队中（未发出）', detail: String(r.message || '非交易时段入队，开盘后自动下发') };
  }
  if (cls === 'duplicate' || (!cls && r.duplicate)) {
    return { tone: 'dup', label: '重复（未重复下单）', detail: String(r.message || '幂等键命中，未产生新委托') };
  }
  // 无 `class` / 未知 class 一律归失败：真钱路径宁可让人多看一眼
  return {
    tone: 'fail',
    label: cls ? '真单失败' : '真单状态未知',
    detail: String(r.message || d?.reason || '无回执'),
  };
}

interface MirrorReceiptView {
  tone: 'ok' | 'queued' | 'dup' | 'skipped' | 'fail';
  label: string;
}

const MIRROR_CLASS_VIEW: Record<string, MirrorReceiptView> = {
  success: { tone: 'ok', label: '真单已下发' },
  queued: { tone: 'queued', label: '真单排队中（未发出）' },
  duplicate: { tone: 'dup', label: '真单重复（未重复发送）' },
  skipped: { tone: 'skipped', label: '真单未下发' },
  failed: { tone: 'fail', label: '真单失败' },
};

/**
 * 镜像回执的展示态。**未知 `class` 归失败**（与后端 `mirror_outcome_class` 同口径）：
 * 真钱路径上宁可显示成失败让用户自己看一眼，也不能默认落进「成功」。
 */
export function mirrorReceiptView(r: PushLegResult): MirrorReceiptView | null {
  const m = r.mirror;
  if (!m) return null;
  const view = MIRROR_CLASS_VIEW[String(m.class || '')];
  if (!view) return { tone: 'fail', label: `真单状态未知（${m.status || '无回执'}）` };
  return view;
}

export interface ExecuteHeadline {
  tone: 'ok' | 'warn' | 'fail';
  text: string;
}

/** 提交后的顶栏结论（`status` 是服务端词：executed/partial/failed/blocked/preview） */
export function executeHeadline(data: PushExecute): ExecuteHeadline {
  const s = data.summary;
  switch (String(data.status || '')) {
    case 'executed':
      return { tone: 'ok', text: `全部提交完成：${s.succeeded}/${s.attempted} 笔成交` };
    case 'partial':
      return { tone: 'warn', text: `部分成功：${s.succeeded} 笔成交 / ${s.failed} 笔失败（未提交 ${s.skipped} 笔）` };
    case 'failed':
      return { tone: 'fail', text: `全部失败：${s.failed} 笔未成交` };
    case 'blocked':
      return { tone: 'fail', text: `全部被阻断：${s.skipped} 笔未提交，一笔都没发出去` };
    case 'preview':
      return { tone: 'warn', text: '这是预检结果（未下单）' };
    default:
      return { tone: 'warn', text: `未知结果状态：${data.status || '无'}` };
  }
}

/** 风控裁定的展示态（`verdict` 是服务端的原始词） */
export function riskVerdictView(leg: PushLeg): { txt: string; cls: string; title: string } {
  const title = (leg.subject ?? [])
    .map(d => `${d.rule_id}：${d.reason || ''}`)
    .concat((leg.environment ?? []).map(d => `[环境] ${d.rule_id}：${d.reason || ''}`))
    .join('\n');
  switch (String(leg.risk_verdict || '')) {
    case 'pass':
      return { txt: '通过', cls: 'bg-emerald-50 text-emerald-600 border-emerald-200', title: title || '风控通过' };
    case 'warn':
      return { txt: '告警', cls: 'bg-amber-50 text-amber-700 border-amber-200', title: title || '风控告警（放行）' };
    case 'reject':
      return leg.risk_enforced === false
        ? { txt: '影子拒单', cls: 'bg-slate-100 text-slate-500 border-slate-200', title: `影子期：判定拒单但**不拦单**\n${title}` }
        : { txt: `拒单[${leg.risk_rule_id || 'risk'}]`, cls: 'bg-rose-50 text-rose-600 border-rose-200', title: title || '风控拒单' };
    case 'halt':
      return { txt: '全停', cls: 'bg-rose-600 text-white border-rose-700', title: title || '风控全停：全局状态机已迁移' };
    case 'error':
      return { txt: '判定失败', cls: 'bg-rose-50 text-rose-600 border-rose-200', title: title || '风控判定过程中出错（fail-closed）' };
    case 'unavailable':
      return { txt: '未判定', cls: 'bg-slate-100 text-slate-400 border-slate-200', title: '风控链路不可用，逐笔未做判定' };
    default:
      return { txt: '未判定', cls: 'bg-slate-100 text-slate-400 border-slate-200', title: title };
  }
}
