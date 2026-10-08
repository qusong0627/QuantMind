import type { ReactNode } from 'react';

/** 分析回合的动作标签：优先按成交事实（fills）判定本轮实际动作，无成交
 *  回退文本关键词。跟在实际发生的交易后面，不要在条件句上打架。
 *
 *  标签体系：卖系 清仓>减仓>卖出；买系 加仓>新增股票>买入；
 *  止盈/止损（文本/条件意图）；文本同时含买卖意向且无成交 → 单一「调仓」；
 *  无动作 → 持仓；空仓 → 空仓（灰虚线）。最多 3 个。 */
export interface ActionTag {
  label: string;
  cls: string;
}

export interface FillLike {
  ts?: string;
  agent?: string | null;
  side?: string | null;
  volume?: number;
  code?: string;
}

export function actionTagsOf(
  content?: string | null,
  ctx?: { fills?: FillLike[]; model?: string; tsMs?: number | null; heldCodes?: Set<string> },
): ActionTag[] {
  const t = content ?? '';
  const tags: ActionTag[] = [];

  // 1) 成交事实优先：本轮时间窗（±15 分钟）内该模型有真实成交回报
  const fills = ctx?.fills ?? [];
  if (fills.length && ctx?.model) {
    const win = ctx.tsMs != null ? [ctx.tsMs - 15 * 60000, ctx.tsMs + 15 * 60000] : null;
    const mine = fills.filter((f) => {
      if (f.agent !== ctx.model) return false;
      const ts = f.ts ? new Date(f.ts).getTime() : NaN;
      if (win && Number.isFinite(ts) && (ts < win[0] || ts > win[1])) return false;
      return true;
    });
    const sells = mine.filter((f) => String(f.side).toUpperCase() === 'SELL');
    const buys = mine.filter((f) => String(f.side).toUpperCase() === 'BUY');
    if (sells.length) {
      const fullyClosed = sells.every((f) => !ctx.heldCodes?.has(f.code ?? ''));
      tags.push({ label: fullyClosed ? '清仓' : '减仓', cls: 'sell' });
    }
    if (buys.length) {
      const isNew = buys.some((f) => !ctx.heldCodes?.has(f.code ?? ''));
      tags.push({ label: isNew ? '新增股票' : '加仓', cls: 'buy' });
    }
    if (tags.length) return tags.slice(0, 3);
  }

  // 2) 无成交 → 文本兜底（条件句只取强势动词）
  if (/止盈|止损/.test(t)) tags.push({ label: /止损/.test(t) ? '止损' : '止盈', cls: 'tp' });

  const sellHit = /清仓|减仓|卖出/.test(t);
  const buyHit = /加仓|新增|建仓|新买入|买入/.test(t);
  if (sellHit && buyHit) {
    tags.push({ label: '调仓', cls: 'tune' }); // 文字含双向意向且无成交：单标签不打架
  } else if (sellHit) {
    if (/清仓/.test(t)) tags.push({ label: '清仓', cls: 'sell' });
    else if (/减仓/.test(t)) tags.push({ label: '减仓', cls: 'sell' });
    else tags.push({ label: '卖出', cls: 'sell' });
  } else if (buyHit) {
    if (/加仓/.test(t)) tags.push({ label: '加仓', cls: 'buy' });
    else if (/新增|建仓|新买入/.test(t)) tags.push({ label: '新增股票', cls: 'buy' });
    else tags.push({ label: '买入', cls: 'buy' });
  }

  if (!tags.length) {
    if (/空仓|候选池复盘/.test(t)) tags.push({ label: '空仓', cls: 'empty' });
    else tags.push({ label: '持仓', cls: 'hold' });
  }
  return tags.slice(0, 3);
}

/** 渲染为标签组（ReactNode），供对话卡头部直接内联 */
export function renderActionTags(
  content?: string | null,
  ctx?: { fills?: FillLike[]; model?: string; tsMs?: number | null; heldCodes?: Set<string> },
): ReactNode {
  return actionTagsOf(content, ctx).map((tag) => (
    <span key={tag.label} className={`mc-mode-chip action ${tag.cls}`}>
      {tag.label}
    </span>
  ));
}