import { describe, expect, test } from 'vitest';
import {
  RAIL_DEFAULT_MAX_W,
  RAIL_DEFAULT_MIN_W,
  RAIL_MAX_W,
  RAIL_MIN_W,
  nextRailWidth,
  parseSavedRailWidth,
  railWidthStyle,
} from '../railWidth';

describe('nextRailWidth', () => {
  test('区间内按位移量平移', () => {
    expect(nextRailWidth(520, -100)).toBe(420);
    expect(nextRailWidth(520, 100)).toBe(620);
  });

  test('位移量带小数时四舍五入到整像素', () => {
    // 指针在 1.5 倍缩放的屏幕上会给出半像素位移，落成 480.5px 会让宽度来回抖
    expect(nextRailWidth(520, 10.4)).toBe(530);
    expect(nextRailWidth(520, 10.6)).toBe(531);
  });

  test('越过下限返回 null（这一帧不动），而不是钳到下限', () => {
    // 钳制的话，指针已经拖到很左边了宽度还贴在下限上，手感像拖拽失效
    expect(nextRailWidth(RAIL_MIN_W, -1)).toBeNull();
    expect(nextRailWidth(520, -1000)).toBeNull();
  });

  test('越过上限返回 null', () => {
    expect(nextRailWidth(RAIL_MAX_W, 1)).toBeNull();
    expect(nextRailWidth(520, 1000)).toBeNull();
  });

  test('正好落在边界上算合法', () => {
    expect(nextRailWidth(520, RAIL_MIN_W - 520)).toBe(RAIL_MIN_W);
    expect(nextRailWidth(520, RAIL_MAX_W - 520)).toBe(RAIL_MAX_W);
  });
});

describe('parseSavedRailWidth', () => {
  test('合法值原样返回', () => {
    expect(parseSavedRailWidth('500')).toBe(500);
    expect(parseSavedRailWidth(String(RAIL_MIN_W))).toBe(RAIL_MIN_W);
    expect(parseSavedRailWidth(String(RAIL_MAX_W))).toBe(RAIL_MAX_W);
  });

  test('越界的落盘值不采信 —— 退回响应式默认比还原一个荒谬宽度好', () => {
    expect(parseSavedRailWidth('100')).toBeNull();
    expect(parseSavedRailWidth('2000')).toBeNull();
    expect(parseSavedRailWidth('-500')).toBeNull();
  });

  test('空值与脏字符串不采信', () => {
    expect(parseSavedRailWidth(null)).toBeNull();
    expect(parseSavedRailWidth('')).toBeNull();
    expect(parseSavedRailWidth('abc')).toBeNull();
    expect(parseSavedRailWidth('NaN')).toBeNull();
    expect(parseSavedRailWidth('Infinity')).toBeNull();
  });
});

describe('railWidthStyle', () => {
  test('拖过就用定值', () => {
    expect(railWidthStyle(600)).toBe('600px');
    expect(railWidthStyle(420)).toBe('420px');
  });

  test('没拖过用响应式 clamp，两端取自常量', () => {
    const style = railWidthStyle(null);
    expect(style).toBe(`clamp(${RAIL_DEFAULT_MIN_W}px, 31vw, ${RAIL_DEFAULT_MAX_W}px)`);
    // 默认下限必须落在可拖区间内，否则「拖窄」会立刻被判定越界而拖不动
    expect(RAIL_DEFAULT_MIN_W).toBeGreaterThanOrEqual(RAIL_MIN_W);
    expect(RAIL_DEFAULT_MAX_W).toBeLessThanOrEqual(RAIL_MAX_W);
  });
});
