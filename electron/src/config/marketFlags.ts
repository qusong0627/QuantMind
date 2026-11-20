/**
 * 市场功能开关
 *
 * 生产环境屏蔽区块链（CRYPTO）市场。
 * 通过 VITE_ENABLE_CRYPTO 控制：true 显示、false 隐藏。
 * 默认：生产环境关闭，开发环境保留。
 */

const envValue = (import.meta.env.VITE_ENABLE_CRYPTO as string | undefined)?.toLowerCase();

export const ENABLE_CRYPTO: boolean =
  envValue === 'true' ? true
    : envValue === 'false' ? false
      : import.meta.env.PROD ? false : true;

export function isMarketEnabled(market: string): boolean {
  if (market === 'CRYPTO') return ENABLE_CRYPTO;
  // 自定义市场为用户自传因子：无外部行情依赖，始终可用。
  if (market === 'CUSTOM') return true;
  return true;
}
