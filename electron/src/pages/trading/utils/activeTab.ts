/**
 * 模拟交易页签解析（从 `RealTradingPage` 抽出的纯函数）。
 *
 * 抽出来只为可测：默认页签是用户可见约定，被改回去时要有测试拦住。
 */

export type ActiveTab =
  | 'desk'
  | 'signals'
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
 * `signals` 来自候选信号跳转，`position` 来自持仓预警（哨兵 `action_url`，
 * 点「去持仓」要真的落到持仓页）。
 *
 * 评估中心原为深链目标（徽章 `?tab=eval&object=…`），2026-11-18 迁后台管理后
 * 不再属于本控制台：徽章改跳 `/admin/eval`，这里同步删掉 `eval`。
 */
const DEEP_LINKABLE: readonly ActiveTab[] = ['signals', 'position'];

/**
 * 深链白名单判定。挂载（`resolveInitialTab`）与**页内 URL 变化**（`RealTradingPage`
 * 的 searchParams 监听）必须用同一把尺子：只认白名单，别的一律不跳 —— 页内 URL
 * 变化若放宽到任意 tab，就会出现「手上输 ?tab=settings 生效、刷新却不生效」的裂缝。
 */
export function isDeepLinkableTab(t: string | null | undefined): t is ActiveTab {
  return DEEP_LINKABLE.includes(t as ActiveTab);
}

/** 从 `window.location.hash` 解析初始页签；`hash` 传入而非内部读取，便于测试与 SSR。 */
export function resolveInitialTab(hash: string | null | undefined): ActiveTab {
  if (!hash) return DEFAULT_ACTIVE_TAB;
  const qi = hash.indexOf('?');
  if (qi < 0) return DEFAULT_ACTIVE_TAB;
  const t = new URLSearchParams(hash.slice(qi + 1)).get('tab');
  return isDeepLinkableTab(t) ? t : DEFAULT_ACTIVE_TAB;
}

/**
 * 从 `window.location.hash` 解析深链里的 `symbol=` 参数（哨兵「去持仓」带标的）。
 * 只在 tab=position 时消费；`hash` 传入而非内部读取，便于测试。
 */
export function resolveInitialSymbol(hash: string | null | undefined): string | null {
  if (!hash) return null;
  const qi = hash.indexOf('?');
  if (qi < 0) return null;
  const params = new URLSearchParams(hash.slice(qi + 1));
  if (params.get('tab') !== 'position') return null;
  const sym = String(params.get('symbol') || '').trim();
  return sym || null;
}
