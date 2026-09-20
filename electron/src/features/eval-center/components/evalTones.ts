/**
 * 语气 → 样式映射（评估中心唯一取色入口）。
 *
 * 色表**不在本文件新建**：胶囊样式取自交易台的 `cardKit.TONES`，缺证据/异常态取自
 * `deskModel.statusStyle()`（「无证据」是独立态，虚线深黄，不等于通过）。文本色从这些
 * 类串里按 `text-*` 提取，避免再手写一份 hex/色阶。
 */

import { TONES } from '../../desk/components/cardKit';
import { statusStyle } from '../../desk/deskModel';
import { CHART_COLORS } from './evalCenterModel';
import type { InsightTone } from './evalInsightModel';

/** 语气 → 胶囊（底色 + 边框 + 文字色） */
export const TONE_CHIP: Record<InsightTone, string> = {
  pos: TONES.red,
  neg: TONES.green,
  risk: TONES.amber,
  flat: TONES.slate,
  pending: statusStyle('no_evidence').card,
};

function textOf(className: string): string {
  return className.split(' ').find((token) => token.startsWith('text-')) || 'text-slate-500';
}

/** 语气 → 纯文本色（大号数值用；来源同上，不新增色阶） */
export const TONE_TEXT: Record<InsightTone, string> = Object.fromEntries(
  Object.entries(TONE_CHIP).map(([tone, className]) => [tone, textOf(className)])
) as Record<InsightTone, string>;

/** 语气 → 图形填充色（取自 `evalCenterModel.CHART_COLORS`，A 股红涨绿跌） */
export const TONE_COLOR: Record<InsightTone, string> = {
  pos: CHART_COLORS.up,
  neg: CHART_COLORS.down,
  risk: CHART_COLORS.risk,
  flat: CHART_COLORS.neutral,
  pending: CHART_COLORS.risk,
};

/** 缺证据态（斜纹格）：与全链证据矩阵的「无证据」同款 */
export const NO_EVIDENCE_CARD = `${statusStyle('no_evidence').card} border`;
export const RED_LINE_BAR = statusStyle('fail').bar;
export const SCORED_BAR = statusStyle('ok').bar;
