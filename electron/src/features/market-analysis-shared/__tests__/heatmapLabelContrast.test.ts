/**
 * 热力图标签字色契约：浅色块必须切深字，否则白字对比度不足 2:1，肉眼等同「方块没有文字」。
 */
import { describe, expect, it } from 'vitest';
import {
  heatmapLabelColor,
  parseHexColor,
  relativeLuminance,
  LABEL_TEXT_DARK,
  LABEL_TEXT_WHITE,
} from '../heatmapLabelContrast';

describe('heatmapLabelColor · 按底色选字色', () => {
  it('深色底保持白字（涨跌停/强信号档）', () => {
    // Arrange / Act / Assert
    expect(heatmapLabelColor('#dc2626')).toBe(LABEL_TEXT_WHITE);
    expect(heatmapLabelColor('#ef4444')).toBe(LABEL_TEXT_WHITE);
    expect(heatmapLabelColor('#059669')).toBe(LABEL_TEXT_WHITE);
    expect(heatmapLabelColor('#7c3aed')).toBe(LABEL_TEXT_WHITE);
  });

  it('浅色底切深字（中性灰/浅绿/浅红/浅紫/琥珀）', () => {
    expect(heatmapLabelColor('#cbd5e1')).toBe(LABEL_TEXT_DARK);
    expect(heatmapLabelColor('#e2e8f0')).toBe(LABEL_TEXT_DARK);
    expect(heatmapLabelColor('#94a3b8')).toBe(LABEL_TEXT_DARK);
    expect(heatmapLabelColor('#34d399')).toBe(LABEL_TEXT_DARK);
    expect(heatmapLabelColor('#10b981')).toBe(LABEL_TEXT_DARK);
    expect(heatmapLabelColor('#f87171')).toBe(LABEL_TEXT_DARK);
    expect(heatmapLabelColor('#ddd6fe')).toBe(LABEL_TEXT_DARK);
    expect(heatmapLabelColor('#a78bfa')).toBe(LABEL_TEXT_DARK);
    expect(heatmapLabelColor('#f59e0b')).toBe(LABEL_TEXT_DARK);
  });

  it('无法解析的底色按白字兜底，保持旧行为', () => {
    expect(heatmapLabelColor(undefined)).toBe(LABEL_TEXT_WHITE);
    expect(heatmapLabelColor('')).toBe(LABEL_TEXT_WHITE);
    expect(heatmapLabelColor('rgb(203, 213, 225)')).toBe(LABEL_TEXT_WHITE);
  });

  it('支持三位短写十六进制', () => {
    expect(parseHexColor('#fff')).toEqual([255, 255, 255]);
    expect(parseHexColor('#000')).toEqual([0, 0, 0]);
    expect(heatmapLabelColor('#fff')).toBe(LABEL_TEXT_DARK);
  });

  it('相对亮度单调：纯白 1、纯黑 0', () => {
    expect(relativeLuminance([255, 255, 255])).toBeCloseTo(1, 5);
    expect(relativeLuminance([0, 0, 0])).toBeCloseTo(0, 5);
  });
});
