/**
 * 色块标签排布契约：每一档的文本盒高都必须 ≥ 行高，
 * 否则 zrender 的 lineOverflow:'truncate' 会整块不渲染（长尾小块空白的根因）。
 */
import { describe, expect, it } from 'vitest';
import { fitBlockLabel, LABEL_TWO_LINE_HEIGHT } from '../heatmapLabelFit';

describe('fitBlockLabel · 由宽到窄的排布阶梯', () => {
  it('大块走两行（沿用系列级 rich formatter）', () => {
    const fit = fitBlockLabel('半导体', 60, 50);
    expect(fit.show).toBe(true);
    expect(fit.style).toEqual({});
    expect(fit.textH).toBe(LABEL_TWO_LINE_HEIGHT);
  });

  it('高度不足两行内容高度时退单行，不会出现「只有名称没有数值」', () => {
    // boxH 26 < 两行内容高度 27
    const fit = fitBlockLabel('半导体', 60, 26);
    expect(fit.show).toBe(true);
    expect(fit.textH).toBeGreaterThanOrEqual(11);
    expect(typeof fit.style.formatter).toBe('function');
  });

  it('窄块单行 9px → 7px 逐档收敛', () => {
    const nine = fitBlockLabel('装修装饰', 27, 17);
    expect(nine.show).toBe(true);
    expect(nine.style.fontSize).toBe(9);
    expect(nine.textH).toBeGreaterThanOrEqual(11);

    const seven = fitBlockLabel('装修装饰', 17, 10);
    expect(seven.show).toBe(true);
    expect(seven.style.fontSize).toBe(7);
    expect(seven.textH).toBeGreaterThanOrEqual(9);
  });

  it('窄高块（长尾常是窄长条）中文名竖排保底', () => {
    const fit = fitBlockLabel('教育培训', 9, 37);
    expect(fit.show).toBe(true);
    expect(fit.style.fontSize).toBe(8);
    expect(fit.textH).toBeGreaterThanOrEqual(9);
    // 每个字一行
    const formatter = fit.style.formatter as () => string;
    expect(formatter()).toBe('教\n育\n培\n训');
  });

  it('非中文名不做竖排（会变成 A/P/P/L）', () => {
    expect(fitBlockLabel('AAPL', 9, 37).show).toBe(false);
  });

  it('确实放不下的极小块才隐藏', () => {
    expect(fitBlockLabel('半导体', 7, 9).show).toBe(false);
    expect(fitBlockLabel('半导体', 20, 8).show).toBe(false);
  });

  it('任何 show=true 的档位都满足 盒高 ≥ 行高', () => {
    const cases: Array<[number, number]> = [
      [60, 50], [60, 26], [27, 17], [24, 12], [17, 10], [14, 10], [9, 37], [8, 20],
    ];
    for (const [w, h] of cases) {
      const fit = fitBlockLabel('中文名称测试', w, h);
      if (!fit.show) continue;
      const lineHeight = Number(fit.style.lineHeight ?? 15);
      expect(fit.textH).toBeGreaterThanOrEqual(lineHeight);
    }
  });
});
