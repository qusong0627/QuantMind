/**
 * 训练数据集页的统计数值格式（2026-10-10）。
 *
 * 铁律：缺失一律渲染「—」（`MISSING`），**绝不显示成 0**——`n_valid=0` 或
 * `IC=0` 在因子语义里是「有数据且为零」，与「没有数据」是两回事。所有
 * formatter 对 null/undefined/NaN/Infinity 一视同仁。
 */

export const MISSING = '—';

type Numeric = number | null | undefined;

const isNum = (value: Numeric): value is number =>
    typeof value === 'number' && Number.isFinite(value);

/** 带符号小数（IC/t 值：正数补 '+'，方向一眼可辨）。 */
export function fmtSigned(value: Numeric, digits = 3): string {
    if (!isNum(value)) return MISSING;
    return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`;
}

/** 普通小数（ICIR/单调性）。 */
export function fmtNum(value: Numeric, digits = 2): string {
    return isNum(value) ? value.toFixed(digits) : MISSING;
}

/** 0~1 比率 → 百分比（换手/胜率）。 */
export function fmtPct(value: Numeric, digits = 1): string {
    return isNum(value) ? `${(value * 100).toFixed(digits)}%` : MISSING;
}

/** 整数（样本量/有效天数），千分位。 */
export function fmtInt(value: Numeric): string {
    return isNum(value) ? Math.round(value).toLocaleString('en-US') : MISSING;
}

/**
 * 窗口覆盖 = 可算中性化 IC 的日数 / 评估窗口总日数。
 *
 * 派生口径：分子取统计条目的 `ic_neutral_days`，分母取 `stats_meta.window.n_dates`
 * （该库快照的评估期总天数）。表头 tooltip 必须写明这个口径——用户看到「62%」
 * 要知道它量的是什么。
 */
export function fmtWindowCoverage(days: Numeric, nDates: Numeric): string {
    if (!isNum(days) || !isNum(nDates) || nDates <= 0) return MISSING;
    const ratio = days / nDates;
    return `${(Math.min(ratio, 1) * 100).toFixed(1)}%`;
}
