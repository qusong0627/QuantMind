import type { StrategyFile } from '../../../types/backtest/strategy';

const STANDARD_TOPK_TYPE = 'standard_topk';

function readStrategyType(strategy: StrategyFile): string {
  const type = String(strategy.parameters?.strategy_type || '').trim().toLowerCase();
  if (type) return type;
  for (const tag of strategy.tags || []) {
    const text = String(tag);
    if (text.startsWith('template:')) {
      return text.slice('template:'.length).trim().toLowerCase();
    }
  }
  if (strategy.id.startsWith('sys_')) {
    return strategy.id.slice(4).trim().toLowerCase();
  }
  return '';
}

export function isStandardTopkStrategy(strategy: StrategyFile): boolean {
  if (readStrategyType(strategy) === STANDARD_TOPK_TYPE) return true;
  const name = String(strategy.name || '').trim();
  return name === '默认 Top-K 选股策略' || name === '标准 Top-K 选股';
}

export function tradingStrategyNumericId(strategy: StrategyFile): number {
  const numeric = Number(strategy.id);
  return Number.isFinite(numeric) ? numeric : Number.MAX_SAFE_INTEGER;
}

/** 默认 Top-K 固定为 1，其余按数据库 ID 升序。 */
export function tradingStrategySortRank(strategy: StrategyFile): number {
  if (isStandardTopkStrategy(strategy)) return 1;
  const explicit = Number(strategy.parameters?.sort);
  if (Number.isFinite(explicit) && explicit > 0) return explicit;
  return 1_000_000_000;
}

export function compareTradingStrategies(a: StrategyFile, b: StrategyFile): number {
  const rankA = tradingStrategySortRank(a);
  const rankB = tradingStrategySortRank(b);
  if (rankA !== rankB) return rankA - rankB;
  const idDiff = tradingStrategyNumericId(a) - tradingStrategyNumericId(b);
  if (idDiff !== 0) return idDiff;
  return String(a.id).localeCompare(String(b.id));
}

export function sortTradingStrategies(strategies: StrategyFile[]): StrategyFile[] {
  return [...strategies].sort(compareTradingStrategies);
}
