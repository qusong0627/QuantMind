/**
 * 热力矩形图谱着色纯函数测试
 *
 * A 股口径：涨红跌绿。阈值语义要与「涨/跌停=±10%」对齐，
 * 且 `flow_net` / 面积为**亿元**（与后端 /heatmap/drill 契约一致）。
 */
import { describe, it, expect } from 'vitest';
import {
  pctToColor,
  flowToColor,
  directionToColor,
  sentimentToColor,
  vpToColor,
  vpLabel,
  computeSentimentScores,
  heatmapCellColor,
  heatmapDirectionLabel,
  heatmapLabelValue,
  HEATMAP_COLOR_LABEL,
} from '../components/heatmapColor';

describe('pctToColor', () => {
  it('涨用红、跌用绿（A 股口径）', () => {
    expect(pctToColor(3)).toMatch(/^#(dc2626|ef4444|f87171)$/);
    expect(pctToColor(-3)).toMatch(/^#(059669|10b981|34d399)$/);
  });

  it('涨停/跌停档位用最深色', () => {
    expect(pctToColor(10)).toBe('#dc2626');
    expect(pctToColor(11)).toBe('#dc2626');
    expect(pctToColor(-10)).toBe('#059669');
  });

  it('平盘用中性灰', () => {
    expect(pctToColor(0)).toBe('#94a3b8');
  });

  it('缺失/非法值按 0 处理（中性灰，不崩）', () => {
    expect(pctToColor(null)).toBe('#94a3b8');
    expect(pctToColor(undefined)).toBe('#94a3b8');
    expect(pctToColor(NaN)).toBe('#94a3b8');
  });
});

describe('flowToColor', () => {
  it('净流入红、净流出绿（单位：亿元）', () => {
    expect(flowToColor(2)).toBe('#dc2626');
    expect(flowToColor(0.5)).toBe('#f87171');
    expect(flowToColor(-0.5)).toBe('#34d399');
    expect(flowToColor(-2)).toBe('#059669');
  });

  it('零值中性灰', () => {
    expect(flowToColor(0)).toBe('#94a3b8');
  });
});

describe('heatmapCellColor', () => {
  it('按维度分派到对应取色器', () => {
    const item = { pct_change: 8, flow_net: -3 };
    expect(heatmapCellColor(item, 'pct')).toBe(pctToColor(8));
    expect(heatmapCellColor(item, 'flow')).toBe(flowToColor(-3));
  });
});

describe('heatmapLabelValue', () => {
  it('涨跌幅带符号 + 百分号', () => {
    expect(heatmapLabelValue({ pct_change: 3.14159 }, 'pct')).toBe('+3.14%');
    expect(heatmapLabelValue({ pct_change: -2 }, 'pct')).toBe('-2.00%');
  });

  it('主力净额带符号 + 亿', () => {
    expect(heatmapLabelValue({ flow_net: 1.234 }, 'flow')).toBe('+1.23亿');
    expect(heatmapLabelValue({ flow_net: -0.5 }, 'flow')).toBe('-0.50亿');
  });

  it('缺失一律显示破折号（不显示成 0）', () => {
    expect(heatmapLabelValue({ pct_change: null }, 'pct')).toBe('—');
    expect(heatmapLabelValue({ flow_net: undefined }, 'flow')).toBe('—');
  });
});

describe('directionToColor / heatmapDirectionLabel', () => {
  const cell = { prob: 0.52, rel_prob: 0.47, market_prob: 0.55, rank: 0.8 };

  it('按截面排名着色：前 20% 红、后 20% 绿、中间灰', () => {
    expect(directionToColor(0.9)).toBe('#dc2626');
    expect(directionToColor(0.1)).toBe('#059669');
    expect(directionToColor(0.5)).toBe('#cbd5e1');
    expect(directionToColor(null)).toBe('#e2e8f0');
  });

  it('箭头=截面排名，数字=历史跑赢概率', () => {
    expect(heatmapDirectionLabel(cell)).toBe('↑47%');
    expect(heatmapDirectionLabel({ ...cell, rank: 0.2 })).toBe('↓47%');
    expect(heatmapDirectionLabel(null)).toBe('—');
  });

  it('direction 模式按周期取值并落到取色/标签', () => {
    const item = { direction: { '10': cell } };
    expect(heatmapCellColor(item, 'direction', 10)).toBe('#dc2626');
    expect(heatmapLabelValue(item, 'direction', 10)).toBe('↑47%');
    expect(heatmapCellColor(item, 'direction', 20)).toBe('#e2e8f0');
  });
});

describe('sentimentToColor', () => {
  it('热度 0~1 映射红/灰/绿，缺失灰白', () => {
    expect(sentimentToColor(0.9)).toBe('#dc2626');
    expect(sentimentToColor(0.5)).toBe('#cbd5e1');
    expect(sentimentToColor(0.1)).toBe('#059669');
    expect(sentimentToColor(null)).toBe('#e2e8f0');
  });

  it('sentiment 模式标签显示热度，不显示成 0', () => {
    expect(heatmapLabelValue({ sentiment_score: 0.78 }, 'sentiment')).toBe('热度78');
    expect(heatmapLabelValue({ sentiment_score: null }, 'sentiment')).toBe('—');
  });
});

describe('computeSentimentScores', () => {
  it('四个分量按截面分位平均：全高=1、全低=0', () => {
    const out = computeSentimentScores([
      { name: '热', breadth_flow: 0.9, breadth_flow_chg: 0.2, pct_period: 5, flow_ratio_accel: 0.1 },
      { name: '中', breadth_flow: 0.5, breadth_flow_chg: 0.0, pct_period: 0, flow_ratio_accel: 0.0 },
      { name: '冷', breadth_flow: 0.1, breadth_flow_chg: -0.2, pct_period: -5, flow_ratio_accel: -0.1 },
    ]);
    expect(out.find((x) => x.name === '热')?.sentiment_score).toBe(1);
    expect(out.find((x) => x.name === '冷')?.sentiment_score).toBe(0);
    expect(out.find((x) => x.name === '中')?.sentiment_score).toBe(0.5);
  });

  it('缺失分量不参与平均；全缺失为 null（不补 0）', () => {
    const out = computeSentimentScores([
      { name: 'A', breadth_flow: 0.8, pct_period: 2 },
      { name: 'B', breadth_flow: 0.2, pct_period: -2 },
      { name: 'C' },
    ]);
    expect(out.find((x) => x.name === 'A')?.sentiment_score).toBe(1);
    expect(out.find((x) => x.name === 'C')?.sentiment_score).toBeNull();
  });
});

describe('vpToColor / vpLabel', () => {
  it('同向加速红/绿，背离琥珀，缺失灰白', () => {
    expect(vpToColor(1, 0.2)).toBe('#dc2626');
    expect(vpToColor(-1, -0.2)).toBe('#059669');
    expect(vpToColor(1, -0.2)).toBe('#f59e0b');
    expect(vpToColor(null, 0.2)).toBe('#e2e8f0');
  });

  it('标签', () => {
    expect(vpLabel(1, 0.2)).toBe('↑共振');
    expect(vpLabel(-1, -0.2)).toBe('↓共振');
    expect(vpLabel(1, -0.2)).toBe('背离');
    expect(heatmapLabelValue({ flow_accel: 1, price_accel: 0.2 }, 'vp')).toBe('↑共振');
  });
});

describe('HEATMAP_COLOR_LABEL', () => {
  it('所有维度都有中文文案', () => {
    expect(HEATMAP_COLOR_LABEL.pct).toBe('涨跌幅');
    expect(HEATMAP_COLOR_LABEL.flow).toBe('主力净额');
    expect(HEATMAP_COLOR_LABEL.accel).toBe('加速度');
    expect(HEATMAP_COLOR_LABEL.direction).toBe('方向概率');
    expect(HEATMAP_COLOR_LABEL.sentiment).toBe('情绪热度');
    expect(HEATMAP_COLOR_LABEL.vp).toBe('量价共振');
  });
});
