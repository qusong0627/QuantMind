/**
 * 共识覆盖度的文案映射（单一来源）。
 *
 * 三个组件都要把后端的原因码翻成人话：`ModelConsensusPanel`、`ModelScoreCurveGrid`、
 * 以及未来的截面视图。早先两个面板各抄一份，改一处漏一处（`exec_failed` 就只在
 * 一边出现过），用户会看到同一个原因两种说法。
 *
 * **键必须与后端同步**（改了后端要一起改）：
 * - `SKIP_REASON_LABEL` ← `research_service.predict_single_stock` 里
 *   `consensus_skip` 的键；
 * - `EXEC_FAIL_LABEL` ← `_CONSENSUS_FAIL_LABEL`。
 */

/** 未参与共识的原因码 → 人话 */
export const SKIP_REASON_LABEL: Record<string, string> = {
  no_pred_artifact: '无推理产物',
  no_same_day_score: '该日无该股分数',
  no_storage: '注册表无路径',
  read_error: '产物读取失败',
  exec_failed: '点名模型推理失败',
};

/** 点名补算失败的原因码 → 人话 */
export const EXEC_FAIL_LABEL: Record<string, string> = {
  resolve_failed: '模型解析失败',
  no_storage_path: '注册表无路径',
  no_model_dir: '模型目录不存在',
  exec_failed: '推理执行失败',
  no_score_after_exec: '产物无该股分数',
  exception: '执行异常',
};

/** 跳过的 `skip_reasons` 条目里，不适合当作「点名失败」重复展示的键 */
export const SKIP_REASON_EXCLUDED_FROM_BANNER = new Set(['exec_failed']);
