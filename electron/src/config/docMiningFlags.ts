/**
 * 文档挖掘功能开关（因子挖掘「上传文档」链）
 *
 * 生产环境隐藏「上传文档」输入入口与挖掘历史的「文档解析」Tab。
 * 通过 VITE_ENABLE_DOC_MINING 控制：true 显示、false 隐藏。
 * 默认：生产环境关闭，开发环境保留。
 *
 * 与后端 `ENABLE_DOC_MINING`（backend/services/engine/alpha_agent/doc_gate.py）
 * 是**同一件事的两端**：本开关管「渲染不渲染」，后端管「端点通不通」。
 * 文档会被上传到 MinerU 云端解析（数据出网），所以恢复必须两端同时打开——
 * 只开后端会 403，只开前端会显示一个调不通的界面，两个方向都是安全的失败方向。
 *
 * 范式与 tradingFlags.ts / marketFlags.ts 完全一致（三态：显式 true / 显式 false /
 * 缺省按构建环境）。
 */

const envValue = (
  import.meta.env.VITE_ENABLE_DOC_MINING as string | undefined
)?.toLowerCase();

export const ENABLE_DOC_MINING: boolean =
  envValue === 'true' ? true
    : envValue === 'false' ? false
      : import.meta.env.PROD ? false : true;

/** UI 是否允许出现文档挖掘相关界面。**所有文档链入口都必须经此判定**。 */
export function isDocMiningEnabled(): boolean {
  return ENABLE_DOC_MINING;
}

/**
 * 后端文档闸门拒绝时的机器可读标记。
 *
 * 与 `backend/services/engine/alpha_agent/doc_gate.py::DISABLED_DETAIL` 是
 * **同一个字面量**，改一处必须改两处。前端据此把「本部署没开文档链」
 * （可预期的正常态）与真错误区分开——只看 403 分不出来。
 */
export const DOC_MINING_DISABLED_DETAIL = 'doc_mining_disabled';
