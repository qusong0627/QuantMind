import { afterEach, describe, expect, test } from 'vitest';
import {
  MIN_WINDOW_HEIGHT,
  MIN_WINDOW_WIDTH,
  WINDOW_MARGIN,
  clampWindowRect,
  defaultWindowRect,
  maximizedWindowRect,
} from '../modelCompare/modelCompareLayout';

const originalWidth = window.innerWidth;
const originalHeight = window.innerHeight;

function setViewport(width: number, height: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height });
}

afterEach(() => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalHeight });
});

describe('modelCompareLayout 浮动窗口尺寸', () => {
  test('默认宽度按模型列数算，5 个模型在 1920 屏上能一屏放下', () => {
    setViewport(1920, 1080);
    // 208 指标列 + 5 × 236 模型列 + 32 缓冲
    const rect = defaultWindowRect(5);
    expect(rect.width).toBe(1420);
    expect(rect.width).toBeGreaterThan(208 + 5 * 236);
    expect(rect.x).toBe(Math.round((1920 - rect.width) / 2));
  });

  test('视口太窄时宽度收在 94%，不会超出屏幕', () => {
    setViewport(1280, 800);
    const rect = defaultWindowRect(8);
    expect(rect.width).toBe(Math.round(1280 * 0.94));
    expect(rect.width + rect.x + WINDOW_MARGIN).toBeLessThanOrEqual(1280);
  });

  test('clamp 把窗口拉回视口并守住最小宽高', () => {
    setViewport(1600, 900);
    const rect = clampWindowRect({ x: -500, y: 5000, width: 200, height: 100 });
    expect(rect.width).toBe(MIN_WINDOW_WIDTH);
    expect(rect.height).toBe(MIN_WINDOW_HEIGHT);
    expect(rect.x).toBe(WINDOW_MARGIN);
    expect(rect.y).toBe(900 - MIN_WINDOW_HEIGHT - WINDOW_MARGIN);
  });

  test('最大化铺满视口，四周留 WINDOW_MARGIN', () => {
    setViewport(1920, 1080);
    expect(maximizedWindowRect()).toEqual({
      x: WINDOW_MARGIN,
      y: WINDOW_MARGIN,
      width: 1920 - WINDOW_MARGIN * 2,
      height: 1080 - WINDOW_MARGIN * 2,
    });
  });
});
