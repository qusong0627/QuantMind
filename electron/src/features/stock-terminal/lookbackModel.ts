/**
 * 信号准确率回看（T-N 分数/排名 → 至今涨跌）· 展示层纯函数。
 *
 * 口径由后端 `backend/services/api/stock_lookback.py` 单方面定义，这里只做**呈现**，
 * 不重算、不补值。两条铁律：
 *
 * 1. **缺失一律破折号，绝不显示成 0。** 0 涨幅是「没涨没跌」这一事实主张；
 *    没数据不是事实主张。混同之后用户会看到一张「模型毫无区分度」的表。
 * 2. **缺的回看点整个不出现**，不拿相邻档位顶替。三个档位量的是同一天的话，
 *    `T-5` 一栏的数字就不是 T-5 了。
 */
import type { LookbackPoint, LookbackSummaryRow, PriceKind } from '../stock-terminal-shared/types';

// 契约类型在共享层，这里原样转发，调用方只需认这一个入口
export type {
  LookbackDetailItem,
  LookbackPoint,
  LookbackSummaryRow,
  PriceKind,
  PriceSourceKind,
  SignalLookbackData,
} from '../stock-terminal-shared/types';

const DASH = '—';

/** 回看点数 → `T-3` / `T-10`。非法输入给空串（不产生 `T-NaN`）。 */
export function lookbackLabel(n: number): string {
  return Number.isFinite(n) ? `T-${n}` : '';
}

/** 分数显示。三位小数与左侧候选列表一致；缺失 → `—`（**不是 0.000**）。 */
export function formatScore(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return DASH;
  return v.toFixed(3);
}

/** 名次显示 `#1127 / 3271`。总样本缺失时不编分母。 */
export function formatRank(rank: number | null | undefined, dayN?: number | null): string {
  if (rank === null || rank === undefined || Number.isNaN(rank)) return DASH;
  if (dayN === null || dayN === undefined || Number.isNaN(dayN)) return `#${rank}`;
  return `#${rank} / ${dayN}`;
}

/**
 * 小数收益 → 百分数（`PctText` 收百分数）。
 * `null` **原样返回 null**，让 `PctText` 走它自己的 `--` 分支。
 */
export function toPct(v: number | null | undefined): number | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  return v * 100;
}

/**
 * 按请求的 `lookbacks` 排布（降序：T-10 → T-3）。
 * 后端没返回的档位**直接不出现**——绝不拿别的档位顶替。
 */
export function orderPoints(points: LookbackPoint[], lookbacks: number[]): LookbackPoint[] {
  const wanted = [...lookbacks].sort((a, b) => b - a);
  return wanted
    .map((n) => points.find((p) => p.lookback === n))
    .filter((p): p is LookbackPoint => p !== undefined);
}

/** 表头价格徽章。来源未知给空串，让调用方决定要不要渲染。 */
export function priceSourceLabel(
  kind: PriceKind | undefined,
  liveCount = 0,
  total = 0,
): string {
  if (kind === 'live') return `实时 ${liveCount} 只`;
  if (kind === 'close') return `收盘价 · ${total} 只`;
  if (kind === 'mixed') return `实时 ${liveCount} / 收盘 ${total - liveCount} 只`;
  return '';
}

/** 只留算得出价差的汇总行（`spread=0` 是真实结论，保留）。 */
export function usableSummaryRows(rows: LookbackSummaryRow[]): LookbackSummaryRow[] {
  return rows.filter((r) => r.spread !== null && r.spread !== undefined);
}

/**
 * 这张表能不能下结论。
 *
 * **零项参与不算通过**：所有回看点都缺价时，界面必须说「算不出来」，
 * 而不是渲染一张全是 `—` 的表——后者会被读成「模型没有区分度」。
 */
export function isVerdictUsable(rows: LookbackSummaryRow[]): boolean {
  return usableSummaryRows(rows).length > 0;
}
