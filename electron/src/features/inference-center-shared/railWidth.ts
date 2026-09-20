/**
 * 推理中心「排名榜 / 个股工作台」分栏宽度。
 *
 * 抽成纯函数是为了能单测：越界处理（拖动到轨道外、localStorage 里的脏数据）
 * 是这类交互唯一会出错的地方，而它埋在组件里时只能靠人肉拖。
 *
 * 为什么要可拖：两侧对宽度都有真实诉求，固定分配必然挤到一边 ——
 * 排名榜那行「手动推理执行 + 日期 + T+N + 立即执行 + 设默认」实测最少要 ~500px
 * （给 460 会横向溢出 20px），而右栏的 K 线图越宽越好。
 */

/** 可拖范围下限：再窄排名榜的控制行就要溢出（T+N 日期已允许截断后的余量） */
export const RAIL_MIN_W = 420;
/** 可拖范围上限：再宽就把右栏压没了 */
export const RAIL_MAX_W = 760;
/** 没拖过时的响应式下限（1636px 视口以上回到 520） */
export const RAIL_DEFAULT_MIN_W = 480;
/** 没拖过时的响应式上限 */
export const RAIL_DEFAULT_MAX_W = 520;
export const RAIL_WIDTH_KEY = 'qm:inference-center:rail-width';

/**
 * 拖动一帧的落点宽。越界返回 `null` 表示「这一帧不动」——
 * 刻意不做贴边钳制：钳制会让指针已经移出轨道后宽度还一直贴在下限上，
 * 手感上像是拖拽失效。
 */
export function nextRailWidth(startWidth: number, dx: number): number | null {
  const next = Math.round(startWidth + dx);
  return next < RAIL_MIN_W || next > RAIL_MAX_W ? null : next;
}

/**
 * 解析落盘的宽度。只在合法区间内才采信 —— 脏数据（换过版本、手改过 localStorage）
 * 会让排名榜宽度变成一个荒谬的值，宁可退回响应式默认。
 */
export function parseSavedRailWidth(raw: string | null): number | null {
  if (raw == null || raw === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) && n >= RAIL_MIN_W && n <= RAIL_MAX_W ? n : null;
}

/** 渲染用的 CSS width：拖过就是定值，没拖过就是响应式 clamp */
export function railWidthStyle(px: number | null): string {
  if (px != null) return `${px}px`;
  return `clamp(${RAIL_DEFAULT_MIN_W}px, 31vw, ${RAIL_DEFAULT_MAX_W}px)`;
}
