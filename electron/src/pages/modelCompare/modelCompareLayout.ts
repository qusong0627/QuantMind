/**
 * 模型对比浮窗的尺寸与位置计算。
 *
 * 窗口用视口坐标（position: fixed / absolute）自由摆放：默认宽度按「指标列 + 模型列数」
 * 推算，能一屏放下 5 个模型就不横向滚动；放不下时表格自身横向滚动，而不是把列挤窄。
 */
import { METRIC_COL_WIDTH, MODEL_COL_WIDTH } from './modelCompareMetrics';

export interface WindowRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** 窗口离视口边缘的最小留白（px）。 */
export const WINDOW_MARGIN = 12;
/** 指标列 + 最多 3 个冻结模型列必须一屏放得下，否则冻结列会被挤出可视区。 */
export const MIN_WINDOW_WIDTH = 940;
/** 低于此高度时表头 + 分组行 + 一两行数据就看不全。 */
export const MIN_WINDOW_HEIGHT = 420;
/** 窗口默认至少这么宽，两个模型时不至于像个小卡片。 */
const PREFERRED_MIN_WIDTH = 960;
/** 默认占视口宽/高的比例（再与最小/最大值收敛）。 */
const DEFAULT_VIEWPORT_WIDTH_RATIO = 0.94;
const DEFAULT_VIEWPORT_HEIGHT_RATIO = 0.86;

export function viewportSize(): { width: number; height: number } {
  if (typeof window === 'undefined') return { width: 1440, height: 900 };
  return { width: window.innerWidth, height: window.innerHeight };
}

/** 把窗口收敛回视口内，并保证宽高不低于最小值。 */
export function clampWindowRect(
  rect: WindowRect,
  viewport: { width: number; height: number } = viewportSize(),
): WindowRect {
  const maxWidth = Math.max(MIN_WINDOW_WIDTH, viewport.width - WINDOW_MARGIN * 2);
  const maxHeight = Math.max(MIN_WINDOW_HEIGHT, viewport.height - WINDOW_MARGIN * 2);
  const width = Math.min(Math.max(rect.width, MIN_WINDOW_WIDTH), maxWidth);
  const height = Math.min(Math.max(rect.height, MIN_WINDOW_HEIGHT), maxHeight);
  const maxX = Math.max(WINDOW_MARGIN, viewport.width - width - WINDOW_MARGIN);
  const maxY = Math.max(WINDOW_MARGIN, viewport.height - height - WINDOW_MARGIN);
  return {
    width,
    height,
    x: Math.min(Math.max(rect.x, WINDOW_MARGIN), maxX),
    y: Math.min(Math.max(rect.y, WINDOW_MARGIN), maxY),
  };
}

/** 默认窗口：宽度能放下当前所有模型列（上限为视口的 94%），并居中。 */
export function defaultWindowRect(modelCount: number): WindowRect {
  const viewport = viewportSize();
  const idealWidth = METRIC_COL_WIDTH + Math.max(1, modelCount) * MODEL_COL_WIDTH + 32;
  const width = Math.min(
    Math.max(idealWidth, PREFERRED_MIN_WIDTH),
    Math.round(viewport.width * DEFAULT_VIEWPORT_WIDTH_RATIO),
  );
  const height = Math.min(
    Math.max(Math.round(viewport.height * DEFAULT_VIEWPORT_HEIGHT_RATIO), MIN_WINDOW_HEIGHT),
    viewport.height - WINDOW_MARGIN * 2,
  );
  return clampWindowRect(
    {
      x: Math.round((viewport.width - width) / 2),
      y: Math.max(WINDOW_MARGIN, Math.round((viewport.height - height) / 2)),
      width,
      height,
    },
    viewport,
  );
}

/** 最大化：铺满视口（四周留白 WIN_MARGIN）。 */
export function maximizedWindowRect(
  viewport: { width: number; height: number } = viewportSize(),
): WindowRect {
  return clampWindowRect(
    {
      x: WINDOW_MARGIN,
      y: WINDOW_MARGIN,
      width: viewport.width - WINDOW_MARGIN * 2,
      height: viewport.height - WINDOW_MARGIN * 2,
    },
    viewport,
  );
}
