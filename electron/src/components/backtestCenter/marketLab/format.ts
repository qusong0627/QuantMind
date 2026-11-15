/** 行情回测页共用的格式化与错误文案。
 *
 * 本模块自带一套数字格式化（原先借用 arena 的 `utils/format`）：行情回测已从
 * 「实盘交易」栏迁到回测中心，不再跨模块取工具函数。
 *
 * ⚠️ `fmtPct` 的入参是**百分数**（TradingView 口径，如 62.35 表示 62.35%），
 *    与全站 `utils/format` 的百分比工具（入参是小数 0.6235）不同——这里的实现
 *    已经把这层换算收在内部，调用方不要自己再除以 100。
 */

export const fmt = (v: number | null | undefined, digits = 2): string => {
  if (v == null || !Number.isFinite(v)) return '—';
  return v.toFixed(digits);
};

export const fmtPct = (v: number | null | undefined, digits = 2, signed = true): string => {
  if (v == null || !Number.isFinite(v)) return '—';
  const sign = signed && v > 0 ? '+' : '';
  return `${sign}${v.toFixed(digits)}%`;
};

export const fmtMoney = (v: number | null | undefined, unit = '¥'): string => {
  if (v == null || !Number.isFinite(v)) return '—';
  return `${unit}${v.toLocaleString('en-US', { maximumFractionDigits: 0, minimumFractionDigits: 0 })}`;
};

/** axios 错误 → 人话（后端 400 的 detail 优先） */
export const errText = (e: unknown): string => {
  const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  return detail || (e instanceof Error ? e.message : String(e));
};

/** ISO 时间戳 → 本地「MM-DD HH:MM」（解析不了返回空串，别让界面出现 Invalid Date） */
export const fmtWhen = (iso?: string): string => {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

/** 语料来源：edited > recrawl > desktop */
export const KIND_LABEL: Record<string, string> = {
  desktop: '原始语料',
  recrawl: '重爬（缩进完整）',
  edited: '已编辑',
  custom: '我的策略',
  missing: '缺失',
};

/** 展示面合规：策略标题里的「买入/卖出」改成「入场/离场」。
 *  只改显示，不改 id/文件名/源码；原始语料标题多为外部策略名，保留其可检索性靠 id。 */
export const sanitizeStrategyTitle = (title: string): string =>
  String(title || '').replace(/买入/g, '入场').replace(/卖出/g, '离场');

/** 对比卡的一格：pct 是百分数口径（62.35），没有 pct 就显示 value（成交笔数这类） */
export interface CompareCellLike {
  label?: string;
  value?: number | null;
  pct?: number | null;
}

const cellValue = (c?: CompareCellLike): number | null => c?.pct ?? c?.value ?? null;

export const compareCellText = (c?: CompareCellLike): string => {
  const v = cellValue(c);
  if (v == null) return '—';
  // 值本身不带符号（回撤 18% 写成 +18% 会让人以为在涨），只有「变化」列才带符号
  return c?.pct != null ? fmtPct(v, 1, false) : fmt(v, 0);
};

export const compareDeltaText = (b?: CompareCellLike, a?: CompareCellLike): string => {
  const bv = cellValue(b);
  const av = cellValue(a);
  if (bv == null || av == null) return '—';
  const d = av - bv;
  const sign = d > 0 ? '+' : '';
  return b?.pct != null || a?.pct != null ? `${sign}${d.toFixed(1)}pp` : `${sign}${d.toFixed(0)}`;
};

/** 变好/变差 → 上色用的 class（数据缺失不猜） */
export const compareDeltaClass = (b?: CompareCellLike, a?: CompareCellLike): string => {
  const bv = cellValue(b);
  const av = cellValue(a);
  if (bv == null || av == null) return '';
  return av > bv ? 'up' : av < bv ? 'down' : '';
};

/** 转写 / 对话任务的阶段文案（两套阶段的 key 不重叠，共用一张表） */
export const STAGE_LABEL: Record<string, string> = {
  queued: '已入队，等宿主 worker 取（每分钟一轮）',
  transpile: '转写中：调模型翻成 Pyne-Python…',
  static: '静态检查未通过',
  backtest: '沙箱回测中（bwrap：只读根 + 断网）…',
  repair: '回测报错，让模型修一版再重跑…',
  context: '读取策略上下文…',
  llm: '模型思考中…',
  staged: '候选已跑通沙箱回测，正在整理前后对比…',
  mode: '该模式还没上线',
  done: '完成',
  worker: 'worker 异常',
};
