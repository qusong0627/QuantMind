/** 资金流方块图纯数据模型：面积、分组、概率回退与标签缺失口径。 */
import { describe, it, expect } from 'vitest';
import {
  blockColor,
  blockLabel,
  buildSectorBlocks,
  buildStockBlocks,
  BLOCK_INFLOW,
  BLOCK_OUTFLOW,
  BLOCK_NEUTRAL,
} from '../components/flowBlockModel';
import type { FlowPanoramaResponse, FlowProbabilityResponse } from '../services/moneyFlowService';

const sector = (name: string, net: number, pct: number | null = 1.2) =>
  ({ name, net_main: net, pct_change: pct, accel: 2e8, members: 10 } as any);

const stockNode = (key: string, name: string, sectorName: string, net: number) =>
  ({ kind: 'stock', key, name, sector: sectorName, net, pct_change: 0.8, accel: 1e8 } as any);

const panorama = {
  nodes: [
    stockNode('SH600000', '甲', '银行', 3e8),
    stockNode('SH600001', '乙', '银行', -1e8),
    stockNode('SZ000001', '丙', '半导体', 2e8),
  ],
  sectors: [sector('银行', 2e8), sector('半导体', 1e8)],
} as unknown as FlowPanoramaResponse;

const probability = {
  available: true,
  artifact: {
    latest: {
      sectors: [{ name: '银行', n: 2, p10: 0.48 }, { name: '半导体', n: 1, p10: 0.42 }],
      stocks: { prob_map: { SH600000: { '10': 0.61 }, SH600001: { '10': 0.31 } } },
    },
    thresholds: { '10': { hi: 0.55, lo: 0.4 } },
  },
} as unknown as FlowProbabilityResponse;

describe('buildSectorBlocks', () => {
  it('面积=|净额|、按面积降序、带聚合概率', () => {
    const blocks = buildSectorBlocks(panorama.sectors, probability, 10);
    expect(blocks.map((b) => b.name)).toEqual(['银行', '半导体']);
    expect(blocks[0].value).toBe(2e8);
    expect(blocks[0].score).toBe(0.48);
    expect(blocks[1].score).toBe(0.42);
  });

  it('模型不可用时概率为空，不补 0', () => {
    const blocks = buildSectorBlocks(panorama.sectors, null, 10);
    expect(blocks.every((b) => b.score === null && b.prob === null)).toBe(true);
  });
});

describe('buildStockBlocks', () => {
  it('按板块分组、父块面积为子块和、个股取 prob_map', () => {
    const blocks = buildStockBlocks(panorama, probability, 10);
    expect(blocks.map((b) => b.name)).toEqual(['银行', '半导体']);
    expect(blocks[0].children?.map((c) => c.name)).toEqual(['甲', '乙']);
    expect(blocks[0].value).toBe(4e8);
    expect(blocks[0].children?.[0].score).toBe(0.61);
  });

  it('未覆盖的个股概率为空（不是 0）', () => {
    const blocks = buildStockBlocks(panorama, probability, 10);
    const semiconductor = blocks.find((b) => b.name === '半导体');
    expect(semiconductor?.children?.[0].score).toBeNull();
  });
});

describe('blockColor / blockLabel', () => {
  const base = { kind: 'sector' as const, net: 1e8, pct: 1.23 };

  it('概率可用时按阈值红/绿/灰；不可用时按净额符号', () => {
    expect(blockColor({ ...base, score: 0.6, prob: 0.6 }, true, 0.55, 0.45)).toBe(BLOCK_INFLOW);
    expect(blockColor({ ...base, score: 0.4, prob: 0.4 }, true, 0.55, 0.45)).toBe(BLOCK_OUTFLOW);
    expect(blockColor({ ...base, score: 0.5, prob: 0.5 }, true, 0.55, 0.45)).toBe(BLOCK_NEUTRAL);
    expect(blockColor({ ...base, score: null, prob: null }, false, 0.55, 0.45)).toBe(BLOCK_INFLOW);
    expect(blockColor({ ...base, net: -1e8, score: null, prob: null }, false, 0.55, 0.45)).toBe(BLOCK_OUTFLOW);
  });

  it('标签带名称/涨幅/概率；缺失显示 —', () => {
    const label = blockLabel({ ...base, name: '银行', score: 0.48, prob: 0.48 }, true);
    expect(label).toContain('银行');
    expect(label).toContain('+1.2%');
    expect(label).toContain('概率48');
    const missing = blockLabel({ ...base, name: '银行', pct: null, score: null, prob: null }, false);
    expect(missing).toContain('—');
  });
});
