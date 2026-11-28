/**
 * 组合选股的纯函数层（无 React、无请求）。
 *
 * 为什么单独一层：榜单/滚动的展示口径（历史徽标、分歧词、CSV 转义、计划过期）
 * 都属于「一旦错了每个页面都错」的东西，放在组件里只能靠渲染快照测，脆且慢。
 * 这里全部是 `(input) → output`，测试直接对拍。
 *
 * **单一算法源**：位置词、分歧档位、持有天数、历史标注都由后端给出，
 * 本模块只做**格式化和翻译**，不重算分位、不自己定阈值。
 */

import type {
  BlendHistory,
  BlendPlan,
  BlendPlanRow,
  BlendPurpose,
  BlendRankingRow,
  BlendRollingColumn,
  BlendRollingExit,
  BlendRollingPick,
  ComboTemplate,
  DispersionBand,
} from '../../../../services/blendTypes';
import { formatResearchScore, RESEARCH_SCORE_MISSING } from '../../../shared/researchScore';
import { ratingPositionLabel } from '../../positionLabels';

/** 分歧档位 → 中文词；unknown/缺失 → `—`（不猜成「低」） */
export function dispersionLabel(band: DispersionBand | string | null | undefined): string {
  switch (String(band ?? '').toLowerCase()) {
    case 'low':
      return '低';
    case 'medium':
      return '中';
    case 'high':
      return '高';
    default:
      return RESEARCH_SCORE_MISSING;
  }
}

/** 综合分（0–100）：缺失 → `—`，绝不显示 0 */
export function scoreLabel(row: Pick<BlendRankingRow, 'research_score'>): string {
  return formatResearchScore(row.research_score ?? null);
}

/** 位置词：统一走 `signalVocabulary`，组件里不得直接渲染 BUY/HOLD/SELL */
export function positionLabel(row: Pick<BlendRankingRow, 'position'>): string {
  return ratingPositionLabel(row.position);
}

/** 覆盖：`n/总成员数`；成员数未知时只显示 n */
export function coverageLabel(coverage: number | null | undefined, memberCount: number): string {
  const n = typeof coverage === 'number' && Number.isFinite(coverage) ? coverage : null;
  if (n === null) return RESEARCH_SCORE_MISSING;
  if (!memberCount || memberCount <= 0) return String(n);
  return `${n}/${memberCount}`;
}

/**
 * 历史徽标。**没有持仓历史返回 `—`，不显示「持有 0 天」**。
 *
 * 数据来源是后端 `history`（纸面台账 + 近 10 日 TopK），本函数只拼词：
 * - `持有 4/10 日`：已持有交易日 / 目标持有日；
 * - `连续在榜 3 日`：截至当日连续进入 Top50 的天数；
 * - `再入选 2 次`：历史上第 2 次及以后进入纸面台账的次数。
 *
 * `holdDays` 缺省时不显示分母（不编一个 10 出来）。
 */
export function historyLabel(
  history: BlendHistory | null | undefined,
  holdDays?: number | null,
): string {
  if (!history) return RESEARCH_SCORE_MISSING;
  const parts: string[] = [];
  const held = Number(history.held_days ?? 0);
  if (Number.isFinite(held) && held > 0) {
    const total = typeof holdDays === 'number' && holdDays > 0 ? `/${holdDays}` : '';
    parts.push(`持有 ${held}${total} 日`);
  }
  const streak = Number(history.consecutive_days_in_top50 ?? 0);
  if (Number.isFinite(streak) && streak > 0) parts.push(`连续在榜 ${streak} 日`);
  const reentry = Number(history.reentry_count ?? 0);
  if (Number.isFinite(reentry) && reentry > 0) parts.push(`再入选 ${reentry} 次`);
  return parts.length ? parts.join(' · ') : RESEARCH_SCORE_MISSING;
}

// ── 滚动建仓 ───────────────────────────────────────────────────────────

export type RollingStatus = 'new' | 'held';

/** 色块：**刻意不用红绿**——红绿在 A 股是涨跌，不是「新进/留存」 */
export const ROLLING_TONE: Record<RollingStatus | 'exit', string> = {
  new: 'border-blue-300 bg-blue-50 text-blue-900',
  held: 'border-slate-200 bg-white text-slate-700',
  exit: 'border-amber-300 bg-amber-50 text-amber-900',
};

export function rollingStatus(pick: Pick<BlendRollingPick, 'is_new'>): RollingStatus {
  return pick.is_new ? 'new' : 'held';
}

/** 单元格摘要：`持有 3 日` / `新进`；缺失为 `—` */
export function rollingHeldLabel(pick: Pick<BlendRollingPick, 'is_new' | 'held_days'>): string {
  if (pick.is_new) return '新进';
  const held = Number(pick.held_days);
  return Number.isFinite(held) && held > 0 ? `持有 ${held} 日` : RESEARCH_SCORE_MISSING;
}

/**
 * 按代码把某列标的折叠成 Map，供跨列对照（如点击标的查看历史）。
 * 同列内代码重复属于上游异常，保留首个并忽略后续（不静默求和）。
 */
export function columnBySymbol(column: BlendRollingColumn | null | undefined): Map<string, BlendRollingPick> {
  const map = new Map<string, BlendRollingPick>();
  for (const pick of column?.picks ?? []) {
    if (!map.has(pick.symbol)) map.set(pick.symbol, pick);
  }
  return map;
}

/** 退出标注：只保留代码（后端已限定「昨列 → 今日」一次） */
export function exitLabel(column: BlendRollingColumn | null | undefined): string {
  const exits = column?.exits ?? [];
  return exits.length ? exits.join('、') : '';
}

/**
 * 收盘价损益（%）：带符号两位小数。**缺失 → `—`，绝不显示成 0**。
 * 0 是合法收益（平盘），与「没有行情」不是一回事。
 */
export function formatReturnPct(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return RESEARCH_SCORE_MISSING;
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}%`;
}

/**
 * A 股口径：**红 = 盈、绿 = 亏**；0 持平走中性色，缺失走灰。
 * 与 `ROLLING_TONE`（新进/留存，刻意不用红绿）分属两套语义，不要合并。
 */
export function returnToneClass(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'text-slate-400';
  if (value > 0) return 'text-red-600';
  if (value < 0) return 'text-green-600';
  return 'text-slate-500';
}

/** 价格两位小数；缺失/非正数 → `—`（价格 0 不可能是有效收盘价） */
export function formatPrice(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    return RESEARCH_SCORE_MISSING;
  }
  return value.toFixed(2);
}

/**
 * 退出弹窗：`入选 … 收盘 x → 退出 … 收盘 y · 持有 n 日 [· 按窗口起点计]`。
 *
 * 展示面合规：不写「买入/卖出」（研究视图只说名单与位置），口径用
 * 「入选日/退出日收盘价」表达；两个价格都是当日真实收盘价。
 */
export function exitTradeLabel(row: BlendRollingExit): string {
  const trade =
    `入选 ${row.entry_date} 收盘 ${formatPrice(row.entry_price)}` +
    ` → 退出 ${row.exit_date} 收盘 ${formatPrice(row.exit_price)}`;
  const parts = [trade, `持有 ${row.held_days} 日`];
  if (row.entry_from_window_start) parts.push('按窗口起点计');
  return parts.join(' · ');
}

// ── 横向滚动条 ─────────────────────────────────────────────────────────

export interface ScrollGeometry {
  visible: boolean;
  /** 滑块宽度（px） */
  thumbWidth: number;
  /** 滑块左偏移（px） */
  thumbLeft: number;
  /** 滚动进度 0–1（无障碍 `aria-valuenow` 用） */
  progress: number;
}

/** 滑块最小宽度（px）：窗口再长也留得住，不至于只剩一个点 */
const MIN_SCROLL_THUMB_PX = 28;

/**
 * 横向滚动条几何：由**视口/内容/滚动位置**三个实测值算出滑块的位置与宽度。
 *
 * 为什么自己画：Electron/Chrome 在 Linux 默认用**覆盖式滚动条**，不滚动时
 * 完全不可见（1920 实测 `offsetHeight - clientHeight === 0`），`::-webkit-scrollbar`
 * 与 `scrollbar-width` 都拿不到常驻轨道。「滚动建仓」是 11 列的长条看板，
 * 没有常驻滑块用户不知道还能往右拖，所以轨道与滑块由组件渲染。
 *
 * 没有横向溢出（内容不多 1px）→ `visible: false`，不占版面。
 */
export function scrollGeometry(input: {
  clientWidth: number;
  scrollWidth: number;
  scrollLeft: number;
  trackWidth: number;
}): ScrollGeometry {
  const { clientWidth, scrollWidth, scrollLeft, trackWidth } = input;
  const maxScroll = scrollWidth - clientWidth;
  if (
    !Number.isFinite(maxScroll) ||
    maxScroll <= 1 ||
    !Number.isFinite(trackWidth) ||
    trackWidth <= 0 ||
    !Number.isFinite(scrollLeft)
  ) {
    return { visible: false, thumbWidth: 0, thumbLeft: 0, progress: 0 };
  }
  const ratio = Math.min(1, Math.max(0, clientWidth / scrollWidth));
  const thumbWidth = Math.min(trackWidth, Math.max(MIN_SCROLL_THUMB_PX, ratio * trackWidth));
  const travel = trackWidth - thumbWidth;
  const progress = Math.min(1, Math.max(0, scrollLeft / maxScroll));
  return { visible: true, thumbWidth, thumbLeft: progress * travel, progress };
}

// ── 计划 ───────────────────────────────────────────────────────────────

export type PlanCategory = '新进' | '留存' | '退出';

export interface PlanDisplayRow extends BlendPlanRow {
  category: PlanCategory;
}

/** 计划三类行按「新进 → 留存 → 退出」固定顺序摊平，类别不靠字段猜 */
export function planDisplayRows(plan: BlendPlan | null | undefined): PlanDisplayRow[] {
  if (!plan) return [];
  return [
    ...(plan.entries ?? []).map((row) => ({ ...row, category: '新进' as const })),
    ...(plan.holds ?? []).map((row) => ({ ...row, category: '留存' as const })),
    ...(plan.exits ?? []).map((row) => ({ ...row, category: '退出' as const })),
  ];
}

/**
 * 计划过期判定：后端只允许确认**最近 2 个交易日**的计划。
 * `dates` 是 `/dates` 返回的降序可用日；计划日不在前两位 → 过期。
 * 日期缺失/列表为空 → 判定为过期（宁可多点一次生成，也不放行陈旧台账）。
 */
export function isPlanStale(
  dataTradeDate: string | null | undefined,
  dates: readonly string[],
): boolean {
  if (!dataTradeDate || dates.length === 0) return true;
  const index = dates.indexOf(dataTradeDate);
  return index === -1 || index >= 2;
}

/** 金额（元）→ 万元/亿元缩写，保留 1 位；负值不隐藏符号 */
export function formatAmount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return RESEARCH_SCORE_MISSING;
  }
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(1)} 亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)} 万`;
  return value.toFixed(0);
}

// ── CSV ────────────────────────────────────────────────────────────────

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/**
 * 榜单 CSV。表头与设计稿 §6.4 一致；缺失值留空（**不填 0**）。
 * 文件名由调用方拼 `组合选股_<blend>_<date>.csv`，带 BOM 供 Excel 识别 UTF-8。
 */
export function buildRankingCsv(rows: readonly BlendRankingRow[], memberCount: number): string {
  const header = ['排名', '代码', '名称', '综合分', '位置', '覆盖', '分歧', '行业', '市值(亿)'];
  const lines = [header.join(',')];
  for (const row of rows) {
    lines.push(
      [
        csvCell(row.rank),
        csvCell(row.code),
        csvCell(row.name),
        // 与屏幕同口径（一位小数）；缺失留空单元格，不写 0 也不写 —
        csvCell(row.research_score == null ? '' : formatResearchScore(row.research_score)),
        csvCell(positionLabel(row)),
        csvCell(coverageLabel(row.coverage, memberCount)),
        csvCell(dispersionLabel(row.dispersion_band)),
        csvCell(row.industry ?? ''),
        csvCell(row.market_cap_yi ?? ''),
      ].join(','),
    );
  }
  return `﻿${lines.join('\n')}`;
}

export function rankingCsvFilename(blendName: string, dataTradeDate: string): string {
  const safe = blendName.replace(/[\\/:*?"<>|]/g, '_').trim() || 'combo';
  return `组合排名_${safe}_${dataTradeDate}.csv`;
}

// ── 模板 ───────────────────────────────────────────────────────────────

export interface TemplateRollingDefaults {
  purpose: 'rebalance';
  entry_per_day: number;
  rolling_window: number;
  target_positions: number;
}

/**
 * 滚动建仓默认值随预测周期派生（与后端 `combo_auto._template` 同口径）：
 * T+10 → 5 只/日 × 10 日 = 50；T+5 → 25；T+20 → 100（上限 100）。
 */
export function templateRollingDefaults(horizonDays?: number | null): TemplateRollingDefaults {
  const horizon = Number(horizonDays);
  const window =
    Number.isFinite(horizon) && horizon >= 1 ? Math.min(60, Math.round(horizon)) : 10;
  const entryPerDay = 5;
  return {
    purpose: 'rebalance',
    entry_per_day: entryPerDay,
    rolling_window: window,
    target_positions: Math.min(100, entryPerDay * window),
  };
}

/** 模板 → 保存请求（成员按模板顺序，权重原样；source 固定 system） */
export function templateToCreateRequest(
  template: ComboTemplate,
  name?: string,
): {
  name: string;
  market: string;
  model_ids: string[];
  purpose: BlendPurpose;
  entry_per_day: number;
  rolling_window: number;
  target_positions: number;
  method: ComboTemplate['method'];
  trim_count: number;
  min_coverage: number;
  consensus_min_count: number;
  weights: Record<string, number> | null;
  source: 'system';
  template_id: string;
} {
  const weights = Object.fromEntries(
    template.members.map((member) => [member.model_id, Number(member.weight) || 1]),
  );
  const equal = template.members.every((member) => Math.abs((Number(member.weight) || 1) - 1) < 1e-9);
  const defaults = templateRollingDefaults(template.horizon_days);
  const pick = (value: number | undefined, fallback: number): number =>
    typeof value === 'number' && Number.isFinite(value) && value >= 1 ? value : fallback;
  return {
    name: (name ?? template.name).trim(),
    market: template.market || 'CN',
    model_ids: template.members.map((member) => member.model_id),
    purpose: template.purpose ?? defaults.purpose,
    // 与服务端 BlendCreateBody 的 le 上限对齐：超上限的模板值不得原样下发
    entry_per_day: Math.min(50, pick(template.entry_per_day, defaults.entry_per_day)),
    rolling_window: Math.min(
      60,
      pick(template.rolling_window, defaults.rolling_window),
    ),
    target_positions: Math.min(
      500,
      pick(template.target_positions, defaults.target_positions),
    ),
    method: template.method,
    trim_count: template.trim_count,
    min_coverage: template.min_coverage,
    consensus_min_count: template.consensus_min_count,
    weights: equal ? null : weights,
    source: 'system',
    template_id: template.template_id,
  };
}

/** 组合期限文案：`T+10`；缺失为 `—` */
export function blendHorizonLabel(horizonDays: number | null | undefined): string {
  return typeof horizonDays === 'number' && Number.isFinite(horizonDays) && horizonDays > 0
    ? `T+${horizonDays}`
    : RESEARCH_SCORE_MISSING;
}

/** 合成方法 → 中文词；未知原样返回（不猜成均值） */
export function blendMethodLabel(method: string | null | undefined): string {
  switch (String(method ?? '').toLowerCase()) {
    case 'mean':
      return '等权均值';
    case 'median':
      return '中位数';
    case 'trimmed':
      return '截尾均值';
    default:
      return method || RESEARCH_SCORE_MISSING;
  }
}

/**
 * 模板证据等级。P1 研究的实际结论是**全部 not_recommended**（K-R4 触发），
 * 所以这里只做等级翻译，不造「推荐」话术；`recommended` 当前不会出现。
 */
export function templateGradeLabel(grade: string | null | undefined): string {
  switch (String(grade ?? '').toLowerCase()) {
    case 'recommended':
      return '推荐';
    case 'watch':
      return '观察';
    case 'unverified':
      return '未验证';
    default:
      return '待验证';
  }
}
