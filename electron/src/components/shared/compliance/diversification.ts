/**
 * 强制分散提示（T-FE-18）：按调仓计划估算单票占比与标的数（纯函数，可单测）。
 *
 * 口径（如实）：只按**本次计划**的买入金额估算，非全账户持仓口径——用于执行前提示，
 * 不阻断提交（平台建议值：单票 ≤15%、最少 3 只，见产品化 §五）。
 */

export interface DiversificationOrderLike {
  symbol: string;
  side: string;
  estimated_amount: number;
}

export interface DiversificationOptions {
  /** 单票占比上限（默认 0.15，平台建议） */
  maxSingleWeight?: number;
  /** 建议最少标的数（默认 3） */
  minNames?: number;
}

export interface DiversificationReport {
  ok: boolean;
  buyCount: number;
  totalBuyAmount: number;
  /** 单票最大占比（0~1）；无买入计划时为 null */
  maxWeight: number | null;
  maxWeightSymbol: string | null;
  warnings: string[];
}

const DEFAULT_MAX_SINGLE_WEIGHT = 0.15;
const DEFAULT_MIN_NAMES = 3;

function isBuy(side: string): boolean {
  const s = String(side || '').toUpperCase();
  return s === 'BUY' || s === '买' || s === '买入';
}

export function checkDiversification(
  orders: readonly DiversificationOrderLike[] | null | undefined,
  opts: DiversificationOptions = {}
): DiversificationReport {
  const maxSingleWeight = opts.maxSingleWeight ?? DEFAULT_MAX_SINGLE_WEIGHT;
  const minNames = opts.minNames ?? DEFAULT_MIN_NAMES;

  const bySymbol = new Map<string, number>();
  for (const o of orders || []) {
    if (!o || !isBuy(o.side)) continue;
    const amount = Math.abs(Number(o.estimated_amount) || 0);
    if (amount <= 0) continue;
    bySymbol.set(o.symbol, (bySymbol.get(o.symbol) || 0) + amount);
  }
  const totalBuyAmount = Array.from(bySymbol.values()).reduce((a, b) => a + b, 0);
  const buyCount = bySymbol.size;

  if (buyCount === 0 || totalBuyAmount <= 0) {
    return {
      ok: true,
      buyCount: 0,
      totalBuyAmount: 0,
      maxWeight: null,
      maxWeightSymbol: null,
      warnings: [],
    };
  }

  let maxWeightSymbol: string | null = null;
  let maxWeight = 0;
  for (const [symbol, amount] of bySymbol) {
    const w = amount / totalBuyAmount;
    if (w > maxWeight) {
      maxWeight = w;
      maxWeightSymbol = symbol;
    }
  }

  const warnings: string[] = [];
  if (maxWeight > maxSingleWeight) {
    warnings.push(
      `单票占比 ${(maxWeight * 100).toFixed(1)}%（${maxWeightSymbol}）超过建议上限 ${(maxSingleWeight * 100).toFixed(0)}%`
    );
  }
  if (buyCount < minNames) {
    warnings.push(`本次计划仅买入 ${buyCount} 只，建议至少 ${minNames} 只以分散风险`);
  }

  return {
    ok: warnings.length === 0,
    buyCount,
    totalBuyAmount,
    maxWeight,
    maxWeightSymbol,
    warnings,
  };
}
