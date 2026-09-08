/**
 * minibt 脚本框架识别（前端统一口径）
 *
 * minibt 策略只能跑在 AI-IDE 的 minibt 运行时（quantmind-minibt-runner 镜像），
 * qlib 回测引擎没装这个库——直接提交必然抛 ModuleNotFoundError。
 * 所有 qlib 回测入口都要用它把 minibt 挡在外面，识别口径与后端
 * strategy_builder._validate_strategy_content 的 AST 拦截保持一致。
 */

/** 元数据标记：parameters.strategy_type = 'minibt_xxx'（minibt 模板同步到策略库时的标记） */
export const isMinibtStrategyType = (strategyType?: string | null): boolean =>
  String(strategyType || '').toLowerCase().startsWith('minibt_');

/**
 * 代码特征：真实 import minibt。
 * 行首锚定 + \b 词界：注释/字符串里的提及不算，minibt_qdb 这类同前缀模块也不算。
 */
const MINIBT_IMPORT_RE = /^\s*(?:import|from)\s+minibt\b/m;

export const isMinibtStrategyCode = (code?: string | null): boolean =>
  MINIBT_IMPORT_RE.test(code || '');

export interface MinibtDetectable {
  code?: string;
  parameters?: Record<string, unknown> | null;
}

/** 元数据或代码任一命中即视为 minibt 策略 */
export const isMinibtStrategy = (strategy?: MinibtDetectable | null): boolean => {
  if (!strategy) return false;
  const rawType = strategy.parameters?.strategy_type;
  const strategyType = rawType == null ? undefined : String(rawType);
  return isMinibtStrategyType(strategyType) || isMinibtStrategyCode(strategy.code);
};
