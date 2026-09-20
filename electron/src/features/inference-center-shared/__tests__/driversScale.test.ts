import { describe, expect, test } from 'vitest';
import { barWidthPct, formatImpact, maxAbsImpact } from '../driversScale';

describe('formatImpact', () => {
  test('印的是 SHAP 原值，不乘 100 也不加百分号', () => {
    // 实测港股 lightgbm 头号因子；旧实现 (.impact*100).toFixed(2) 会印成「0.45%」
    expect(formatImpact(0.00453)).toBe('0.00453');
  });

  test('大值收到 3 位小数，不铺一串无用精度', () => {
    expect(formatImpact(0.123456)).toBe('0.123');
    expect(formatImpact(Math.abs(-0.987654))).toBe('0.988');
  });

  test('小到 1e-5 量级仍能看出非零 —— 旧实现印 +0.00%', () => {
    expect(formatImpact(0.00007)).toBe('0.00007');
    expect(formatImpact(0.00007)).not.toContain('0.00%');
  });

  test('三位有效数字，尾零去掉（不出现 0.0000700 / 1.000）', () => {
    expect(formatImpact(0.0099)).toBe('0.0099');
    expect(formatImpact(0.000001)).toBe('0.000001');
    expect(formatImpact(1)).toBe('1');
  });

  test('再小转科学计数，不塌成一串 0', () => {
    expect(formatImpact(0.00000031)).toBe('3.1e-7');
    expect(formatImpact(0)).toBe('0');
  });
});

describe('maxAbsImpact', () => {
  test('取绝对值最大者', () => {
    expect(maxAbsImpact([0.0045, -0.013, 0.0001])).toBeCloseTo(0.013, 10);
  });

  test('空组与全非有限值是 0，调用方据此除零保护', () => {
    expect(maxAbsImpact([])).toBe(0);
    expect(maxAbsImpact([NaN, Infinity])).toBe(0);
  });
});

describe('barWidthPct', () => {
  test('组内最大者撑满 100%，其余按比例', () => {
    // 旧实现 |impact|*2000：0.0045 只有 9% 宽度，整排看着像没渲染
    expect(barWidthPct(0.0045, 0.0045)).toBe(100);
    expect(barWidthPct(0.00225, 0.0045)).toBe(50);
    expect(barWidthPct(0.00045, 0.0045)).toBeCloseTo(10, 10);
  });

  test('负向因子按绝对值给宽度（方向由左右分列表达）', () => {
    expect(barWidthPct(-0.0045, 0.0045)).toBe(100);
  });

  test('组内全零 / 非法输入时不给宽度', () => {
    expect(barWidthPct(0, 0)).toBe(0);
    expect(barWidthPct(0.0045, 0)).toBe(0);
    expect(barWidthPct(NaN, 0.0045)).toBe(0);
  });

  test('不超过 100%（防上游给出超过本组最大值的数）', () => {
    expect(barWidthPct(0.01, 0.0045)).toBe(100);
  });
});
