/**
 * 策略编辑（回测中心「策略编辑」）参数 → 代码 的唯一改写实现。
 *
 * 为什么要在前端改代码：
 * - minibt 的标的/区间/股数/佣金/均线参数都是脚本顶层常量，平台没有请求级覆盖；
 * - 模块型 STRATEGY_CONFIG 的 topk / n_drop / rebalance_days 在代码 kwargs 里，
 *   AI-IDE 模块回测按代码执行，请求里的 strategy_params 不会覆盖代码。
 *
 * 因此这里做**最小的文本替换**：只改已经存在的键/常量，不注入新结构；
 * 找不到就原样返回，避免把用户代码改坏。
 */

export interface StrategyEditorBacktestParams {
  symbol: string;
  startDate: string;
  endDate: string;
  initialCapital: number;
  universe: string;
  commission: number;
  topk: number;
  nDrop: number;
  rebalanceDays: number;
  size: number;
  fast: number;
  slow: number;
}

export const DEFAULT_STRATEGY_EDITOR_PARAMS: StrategyEditorBacktestParams = {
  symbol: '600036',
  startDate: '2024-01-01',
  endDate: '2026-10-26',
  initialCapital: 1_000_000,
  universe: 'all',
  commission: 0.00025,
  topk: 50,
  nDrop: 10,
  rebalanceDays: 3,
  size: 1000,
  fast: 10,
  slow: 20,
};

const MINIBT_IMPORT_RE = /^\s*(?:import|from)\s+minibt\b/m;

export function isMinibtStrategyCode(code: string): boolean {
  return MINIBT_IMPORT_RE.test(String(code || ''));
}

function replaceStringAssignment(code: string, name: string, value: string): string {
  if (!value) return code;
  const re = new RegExp(`^(${name}\\s*=\\s*)(['"])(.*?)\\2`, 'm');
  return code.replace(re, (_m, prefix: string, quote: string) => `${prefix}${quote}${value}${quote}`);
}

function replaceNumberAssignment(code: string, name: string, value: number): string {
  if (!Number.isFinite(value)) return code;
  const re = new RegExp(`^(${name}\\s*=\\s*)(-?\\d+(?:\\.\\d+)?)`, 'm');
  return code.replace(re, `$1${value}`);
}

function replaceDictParam(code: string, name: string, value: number): string {
  if (!Number.isFinite(value)) return code;
  // params = dict(fast=10, slow=20)
  const dictRe = /params(\s*=\s*)dict\(([^)]*)\)/;
  code = code.replace(dictRe, (_match, eq: string, args: string) => {
    const nextArgs = args.replace(
      new RegExp(`(${name}\\s*=\\s*)(-?\\d+(?:\\.\\d+)?)`),
      `$1${value}`,
    );
    return `params${eq}dict(${nextArgs})`;
  });
  // params = {"fast": 10, "slow": 20}
  const literalRe = /params(\s*=\s*)\{([^}]*)\}/;
  code = code.replace(literalRe, (_match, eq: string, body: string) => {
    const nextBody = body.replace(
      new RegExp(`(['"]${name}['"]\\s*:\\s*)(-?\\d+(?:\\.\\d+)?)`),
      `$1${value}`,
    );
    return `params${eq}{${nextBody}}`;
  });
  return code;
}

function replaceJsonNumericKey(code: string, key: string, value: number): string {
  if (!Number.isFinite(value)) return code;
  const re = new RegExp(`(['"]${key}['"]\\s*:\\s*)(-?\\d+(?:\\.\\d+)?)`, 'g');
  return code.replace(re, `$1${value}`);
}

/**
 * 把参数应用到代码：minibt 改写顶层常量与 params；STRATEGY_CONFIG 改写 kwargs 中
 * 已存在的 topk / n_drop / rebalance_days。其他代码原样返回。
 */
export function applyBacktestParamsToCode(
  code: string,
  params: StrategyEditorBacktestParams,
): string {
  const source = String(code || '');
  if (!source.trim()) return source;

  if (isMinibtStrategyCode(source)) {
    let next = source;
    next = replaceStringAssignment(next, 'SYMBOL', String(params.symbol || '').trim());
    next = replaceStringAssignment(
      next,
      'START',
      String(params.startDate || '').replace(/-/g, ''),
    );
    next = replaceStringAssignment(
      next,
      'END',
      String(params.endDate || '').replace(/-/g, ''),
    );
    next = replaceNumberAssignment(next, 'SIZE', Number(params.size));
    next = replaceNumberAssignment(next, 'COMMISSION', Number(params.commission));
    next = replaceDictParam(next, 'fast', Number(params.fast));
    next = replaceDictParam(next, 'slow', Number(params.slow));
    return next;
  }

  if (source.includes('STRATEGY_CONFIG')) {
    let next = source;
    next = replaceJsonNumericKey(next, 'topk', Number(params.topk));
    next = replaceJsonNumericKey(next, 'n_drop', Number(params.nDrop));
    next = replaceJsonNumericKey(next, 'rebalance_days', Number(params.rebalanceDays));
    return next;
  }

  return source;
}
