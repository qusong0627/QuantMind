/**
 * 交易功能开关
 *
 * 生产环境屏蔽实盘交易（券商通道 / QMT 真单镜像 / 实盘模式切换）。
 * 通过 VITE_ENABLE_REAL_TRADING 控制：true 显示、false 隐藏。
 * 默认：生产环境关闭，开发环境保留。
 *
 * 与后端 `ENABLE_REAL_TRADING`（backend/services/trade_shared/trade_config.py）
 * 是**同一件事的两端**：本开关管「渲染不渲染」，后端管「端点通不通」。
 * 恢复实盘必须两端同时打开——只开后端会 403，只开前端会显示一个调不通的界面，
 * 两个方向都是安全的失败方向。
 *
 * 范式与 marketFlags.ts 完全一致（三态：显式 true / 显式 false / 缺省按构建环境）。
 */

const envValue = (
  import.meta.env.VITE_ENABLE_REAL_TRADING as string | undefined
)?.toLowerCase();

export const ENABLE_REAL_TRADING: boolean =
  envValue === 'true' ? true
    : envValue === 'false' ? false
      : import.meta.env.PROD ? false : true;

/**
 * UI 是否允许出现实盘相关界面。**所有实盘入口都必须经此判定**——
 * 模式开关、券商配置页签、real 推送通道、后台订单/风控筛选。
 *
 * 与 `normalizeTradingMode` 的「未知一律判为模拟」同向：宁可少认一个实盘，
 * 不可把一个模拟部署显示成实盘（反向误判会让用户以为真金白银在下单）。
 */
export function isLiveTradingEnabled(): boolean {
  return ENABLE_REAL_TRADING;
}

/**
 * 后端实盘闸门拒绝时的机器可读标记。
 *
 * 与 `backend/shared/live_trading_gate.py::DISABLED_DETAIL` 是**同一个字面量**，
 * 改一处必须改两处。前端据此把「本部署没开实盘」（可预期的正常态）与
 * 「权限不足」（要原样抛给用户看的真错误）区分开——只看 403 分不出来。
 */
export const REAL_TRADING_DISABLED_DETAIL = 'real_trading_disabled';
