import { describe, test, expect } from 'vitest';
import { BOX_IDS } from '../types';
import { MARKET_CONTENT, formatBoxTitle, getMarketContent } from '../marketContent';

const MARKETS = ['CN', 'HK', 'US', 'FUTURES', 'CRYPTO'] as const;

describe('marketContent 规格完整性', () => {
  test('每个市场都声明了全部 6 个框', () => {
    for (const market of MARKETS) {
      const content = MARKET_CONTENT[market];
      expect(content, `缺少市场 ${market}`).toBeTruthy();
      for (const boxId of BOX_IDS) {
        const box = content.boxes[boxId];
        expect(box, `${market}.${boxId} 未声明`).toBeTruthy();
        expect(box.title.length, `${market}.${boxId} 标题为空`).toBeGreaterThan(0);
        expect(box.emptyTitle.length, `${market}.${boxId} 空态标题为空`).toBeGreaterThan(0);
        expect(box.emptyHint.length, `${market}.${boxId} 空态说明为空`).toBeGreaterThan(0);
      }
    }
  });

  test('未知市场回退 A 股规格而不是抛错', () => {
    expect(getMarketContent('XX').market).toBe('CN');
    expect(getMarketContent(undefined).market).toBe('CN');
  });
});

describe('formatBoxTitle', () => {
  test('替换 {label} 与 {mode} 占位符', () => {
    const fund = MARKET_CONTENT.HK.boxes.fund;

    expect(formatBoxTitle(fund, { label: '港股', mode: '模拟' })).toBe('资金概览 (港股/模拟)');
    expect(formatBoxTitle(fund, { label: '美股', mode: '实盘' })).toBe('资金概览 (美股/实盘)');
  });

  test('占位符缺失时按空串替换，不残留大括号', () => {
    const title = formatBoxTitle({ title: '{label}概览 {mode}' }, { label: 'A股' });

    expect(title).toBe('A股概览');
    expect(title).not.toContain('{');
  });
});

describe('内容分层：市场差异只在规格里表达', () => {
  test('非 A 股市场关闭无市场口径的图表面板并给出说明', () => {
    for (const market of ['HK', 'US', 'FUTURES', 'CRYPTO'] as const) {
      const charts = MARKET_CONTENT[market].boxes.charts;
      expect(charts.panels?.portfolioSeries, `${market} 应关闭组合绩效面板`).toBe(false);
      expect(charts.panelNote, `${market} 应说明原因`).toBeTruthy();
    }
  });

  test('A 股保留全部图表面板（默认开启）', () => {
    expect(MARKET_CONTENT.CN.boxes.charts.panels?.portfolioSeries).toBeUndefined();
  });

  test('未开通模拟盘的市场给出开通入口，区块链不给', () => {
    expect(MARKET_CONTENT.HK.boxes.fund.canOpenAccount).toBe(true);
    expect(MARKET_CONTENT.US.boxes.fund.canOpenAccount).toBe(true);
    expect(MARKET_CONTENT.CRYPTO.boxes.fund.canOpenAccount).toBe(false);
  });

  test('货币符号随市场变化', () => {
    expect(MARKET_CONTENT.CN.boxes.fund.currency).toBe('¥');
    expect(MARKET_CONTENT.HK.boxes.fund.currency).toBe('HK$');
    expect(MARKET_CONTENT.US.boxes.fund.currency).toBe('$');
  });
});
