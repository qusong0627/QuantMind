/**
 * 内部信号词汇（BUY/SELL/HOLD）的**中性译法**——展示面唯一来源。
 *
 * 为什么需要这一层：`signal_side` 是推理链路的内部判定（筛选、撮合、推送都在消费它），
 * 直接渲染成「买入信号 / 卖出信号 / 强烈看多」就是在向用户输出**投资建议**的形态。
 * 展示面一律改说「靠前 / 靠后 / 居中」——只描述该标的在当日截面里的位置。
 *
 * **边界（不可越界）**：执行上下文必须继续说「买入/卖出」——模拟盘下单表单、
 * 委托列表、成交台账、手动任务页。用户在那些地方要按的是真按钮，含糊化会让人
 * 下错单，比措辞风险严重得多。
 *
 * 为什么不做成「前 20% / 后 20%」这种更精确的说法：`signal_side` 有两条判定来源——
 * 推理链路按百分位（`_resolve_signal_sides` 的 buy_pct=0.20）+ 共识/置信闸门，
 * 而 pred.parquet 回退路线按**绝对阈值**（`fusion > 0.2`）。两条来源下 BUY 的含义
 * 并不相同，写死百分比会在其中一条上变成假话。所以这里只给定性位置。
 */

/** 唯一译法表。改这里就是改全站展示口径。 */
export const SIGNAL_POSITION_LABELS: Record<string, string> = {
    BUY: '靠前',
    SELL: '靠后',
    HOLD: '居中',
};

/** 位置说明（唯一一份）。UI 的 tooltip / 副标题引用这里。 */
export const SIGNAL_POSITION_HINT =
    '模型在当日截面中的相对位置分类（靠前 / 靠后 / 居中）。它描述位置，不构成任何买卖建议；不同来源的判定阈值不同，仅同源可比。';

/**
 * `signal_side` → 中性位置词。未知值或缺失一律 `—`。
 *
 * **不猜**：把 `STRONG_BUY` 硬套成「靠前」看着无害，但那是在替一个我们不认识的
 * 判定编一个含义出来；显示 `—` 才能让上游多做的那件事显形。
 */
export function signalPositionLabel(side: string | null | undefined): string {
    if (side === null || side === undefined) return '—';
    const key = String(side).trim().toUpperCase();
    return SIGNAL_POSITION_LABELS[key] ?? '—';
}
