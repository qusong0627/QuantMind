/**
 * 热力矩形图 · 色块标签排布（跨市场共享的纯函数）
 *
 * 色块从大到小分四档，保证「只要放得下一行字就一定有字」：
 *   两行（名称 11px + 数值 10px）→ 单行 9px → 单行 7px → 竖排单字 8px → 隐藏。
 *
 * 硬约束（实测踩过两次）：
 * - 文本盒高 = 色块内框高 − 上下 padding，zrender 的 lineOverflow:'truncate'
 *   按盒高判定能放几行；盒高**小于行高时整块一个字都不渲染**，不是截断。
 *   所以每一档的 textH 都必须 ≥ 该档 lineHeight。
 * - 内框 = 布局尺寸 − 边框×2（series borderWidth=1.5 → 扣 3px），
 *   padding 要在内框上算，否则差 3px 又会踩到上面那条。
 */

/** 两行排布（名称 15px + 数值约 12px）的盒高校准值（离屏 SVG 实测）。 */
export const LABEL_TWO_LINE_HEIGHT = 31;

/** 两行排布的内容高度：名称行高 15 + 数值行 ~12。 */
const TWO_LINE_CONTENT_HEIGHT = 27;

export interface BlockLabelFit {
  show: boolean;
  /** 文本盒高（px）；show=false 时为 0 */
  textH: number;
  /** 覆盖 label 的样式/格式化（两行档为空对象：沿用系列级 rich formatter） */
  style: Record<string, unknown>;
}

/**
 * @param name 色块名称
 * @param boxW 色块内框宽（已扣边框）
 * @param boxH 色块内框高（已扣边框）
 */
export function fitBlockLabel(name: string, boxW: number, boxH: number): BlockLabelFit {
  // 两行：名称行 + 数值行都要放得下
  if (boxW >= 37 && boxH >= TWO_LINE_CONTENT_HEIGHT) {
    return { show: true, textH: Math.min(LABEL_TWO_LINE_HEIGHT, boxH), style: {} };
  }
  // 单行 9px（lineHeight 11）
  if (boxW >= 24 && boxH >= 12) {
    return {
      show: true,
      textH: Math.min(13, boxH),
      style: { formatter: () => name, fontSize: 9, lineHeight: 11 },
    };
  }
  // 单行 7px（lineHeight 9）；盒高留 10 以上，避免小数块高把行高挤掉
  if (boxW >= 14 && boxH >= 10) {
    return {
      show: true,
      textH: Math.min(11, boxH),
      style: { formatter: () => name, fontSize: 7, lineHeight: 9 },
    };
  }
  // 竖排单字：只对纯中文名生效，窄高块（长尾常是窄长条）最后一档保底
  const isCjkName = /^[一-鿿]{2,}$/.test(name);
  if (isCjkName && boxW >= 8 && boxH >= 20) {
    return {
      show: true,
      textH: Math.min(boxH, name.length * 9),
      style: {
        formatter: () => name.split('').join('\n'),
        fontSize: 8,
        lineHeight: 9,
      },
    };
  }
  return { show: false, textH: 0, style: {} };
}
