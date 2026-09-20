/**
 * 模拟交易页签解析（从 `RealTradingPage` 抽出的纯函数）。
 *
 * 抽出来只为可测：默认页签是用户可见约定，被改回去时要有测试拦住。
 */

export type ActiveTab =
  | 'desk'
  | 'signals'
  | 'eval'
  | 'manage'
  | 'manual-task'
  | 'personal'
  | 'position'
  | 'history'
  | 'settings'
  | 'replay';

/**
 * 无深链时的落地页签 = **系统健康**。
 *
 * 进页第一眼先看平台能不能用：健康 / 行情 / 推理链有红的时候，
 * 后面几栏的数据本来也不可信。需要看策略再手动切。
 */
export const DEFAULT_ACTIVE_TAB: ActiveTab = 'desk';

/**
 * 只有这几条支持深链直达——别的一律回落默认，不盲信 URL。
 * `eval` 来自评估徽章跳转，`signals` 来自候选信号跳转。
 */
const DEEP_LINKABLE: readonly ActiveTab[] = ['eval', 'signals'];

/** 从 `window.location.hash` 解析初始页签；`hash` 传入而非内部读取，便于测试与 SSR。 */
export function resolveInitialTab(hash: string | null | undefined): ActiveTab {
  if (!hash) return DEFAULT_ACTIVE_TAB;
  const qi = hash.indexOf('?');
  if (qi < 0) return DEFAULT_ACTIVE_TAB;
  const t = new URLSearchParams(hash.slice(qi + 1)).get('tab');
  return DEEP_LINKABLE.includes(t as ActiveTab) ? (t as ActiveTab) : DEFAULT_ACTIVE_TAB;
}
