/**
 * 归因面板（单股因子贡献透视）的展示口径：数字格式 + 强度条归一。
 *
 * 抽出来是因为原实现把这两件事**写死在组件里**，而且都写错了方向：
 *
 * 1) 数字：`|impact| * 100` 再钉两位小数，把 SHAP 原值当百分比印。实测港股
 *    lightgbm 头号因子 impact=0.00453 → 印成 `+0.45%`，最小的一条 7e-05 →
 *    `+0.01%`，再小就是 `+0.00%`（看着像零贡献）。SHAP 值在模型输出量纲上
 *    （收益率回归是千分位、概率是百分位），跨模型不存在统一的「百分比」含义。
 *
 * 2) 强度条：`|impact| * 2000` 假设 |impact| 落在 0.05 附近。实测同一批因子的
 *    条宽只有 4.3 / 1.5 / 0.6 / 0.5 / 0.4 / 0.1 px（轨道 48px），一整排看着
 *    像没渲染出来 —— 这正是「归因面板没有用」的观感来源。归因要回答的是
 *    「谁在推、谁在压、相对强弱多少」，按本组最大值归一的**比例**才是信息。
 */

/** 去掉定点小数多余的尾零：「0.0000700」→「0.00007」，「1.000」→「1」。 */
function trimZeros(fixed: string): string {
  if (!fixed.includes('.')) return fixed;
  return fixed.replace(/0+$/, '').replace(/\.$/, '');
}

/** SHAP 原值展示：三位有效数字、不用百分号、尽量不用科学计数法。 */
export function formatImpact(value: number): string {
  const abs = Math.abs(value);
  if (abs === 0) return '0';
  if (abs >= 0.01) return trimZeros(value.toFixed(3));
  if (abs >= 1e-6) {
    // 0.01 以下逐档加密小数位，保证三位有效数字；小于 1e-6 才转科学计数法
    // （再往下铺零，「像零贡献」的老问题会以另一种形式回来）。
    const digits = Math.min(10, 3 - Math.floor(Math.log10(abs)) - 1);
    return trimZeros(value.toFixed(digits));
  }
  return value.toExponential(1);
}

/** 本组贡献值的最大绝对值；空组返回 0（调用方据此避免除零）。 */
export function maxAbsImpact(impacts: readonly number[]): number {
  return impacts.reduce((m, v) => (Number.isFinite(v) ? Math.max(m, Math.abs(v)) : m), 0);
}

/** 强度条宽度百分比：组内最大值撑满，其余按比例；组内全 0 时一律 0。 */
export function barWidthPct(impact: number, maxAbs: number): number {
  if (!(maxAbs > 0) || !Number.isFinite(impact)) return 0;
  return Math.min(100, (Math.abs(impact) / maxAbs) * 100);
}
