/**
 * 推理中心展示面合规测试（P0）。
 *
 * 纪律：截面选股/组合选股只描述**位置**（靠前/居中/靠后），不出现买卖字眼；
 * 执行面（下单、委托、成交）不受本测试约束。
 *
 * 这里同时做两件事：
 * 1. 渲染测试——断言用户真正看到的 DOM 与 CSV 内容；
 * 2. 源码扫描——防止后续把「买入/卖出」又写回展示组件（含注释）。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { InferenceRankingItem, InferenceRankingResult } from '../../../services/modelTrainingService';
import { buildCrossSectionCsv, CrossSectionTable } from '../components/CrossSectionTable';
import { RankingList } from '../components/RankingList';
import { ModelScoreCurveGrid } from '../components/ModelScoreCurveGrid';
import { ratingPositionLabel } from '../positionLabels';

vi.mock('../../stock-terminal/components/InferenceScoreChart', () => ({
  InferenceScoreChart: () => <div data-testid="score-chart" />,
}));

const BANNED_WORDS = ['买入', '卖出', '加仓', '减仓', '建仓', '清仓'];

function makeRows(): InferenceRankingItem[] {
  return [
    { rank: 1, code: 'SH600001', name: '甲', score: 0.81, signal: 'buy', industry: '银行' },
    { rank: 2, code: 'SH600002', name: '乙', score: 0.42, signal: 'hold', industry: '银行' },
    { rank: 3, code: 'SH600003', name: '丙', score: 0.03, signal: 'sell', industry: '银行' },
  ];
}

function makeRanking(): InferenceRankingResult {
  return {
    run_id: 'run-20261019-abcdef',
    inference_date: '2026-10-19',
    target_date: '2026-10-20',
    model_id: 'mdl_demo',
    rankings: makeRows(),
  };
}

describe('CrossSectionTable 合规展示', () => {
  it('用位置词渲染三态，不出现买卖字眼', () => {
    const { container } = render(
      <CrossSectionTable
        ranking={makeRanking()}
        loading={false}
        fallbackFrom={null}
        onSelect={() => undefined}
      />,
    );

    expect(screen.getAllByText('靠前').length).toBeGreaterThan(0);
    expect(screen.getAllByText('居中').length).toBeGreaterThan(0);
    expect(screen.getAllByText('靠后').length).toBeGreaterThan(0);
    for (const word of BANNED_WORDS) {
      expect(container.textContent).not.toContain(word);
    }
  });

  it('表头与筛选器叫「位置」，不再叫「信号」', () => {
    render(
      <CrossSectionTable
        ranking={makeRanking()}
        loading={false}
        fallbackFrom={null}
        onSelect={() => undefined}
      />,
    );

    expect(screen.getAllByText('位置').length).toBeGreaterThan(0);
    expect(screen.queryByText('信号')).toBeNull();
  });

  it('CSV 表头与单元格使用位置词', () => {
    const csv = buildCrossSectionCsv(makeRows(), {
      runId: 'run-20261019-abcdef',
      inferenceDate: '2026-10-19',
      targetDate: '2026-10-20',
      modelId: 'mdl_demo',
      total: 3,
    });

    expect(csv).toContain('位置');
    expect(csv).not.toContain('信号');
    expect(csv).toContain('靠前');
    expect(csv).toContain('居中');
    expect(csv).toContain('靠后');
    expect(csv).not.toContain('buy');
    expect(csv).not.toContain('sell');
    expect(csv).not.toContain('hold');
    for (const word of BANNED_WORDS) {
      expect(csv).not.toContain(word);
    }
  });

  it('参考价列标出来源：盘前兜底是「昨收」，实时是「实时」，缺失留空', () => {
    const csv = buildCrossSectionCsv(
      [
        { rank: 1, code: 'SH600001', name: '甲', score: 0.81, signal: 'buy', expected_price: 12.34, expected_price_source: 'live' },
        { rank: 2, code: 'SH600002', name: '乙', score: 0.42, signal: 'hold', expected_price: 9.1, expected_price_source: 'prev_close' },
        { rank: 3, code: 'SH600003', name: '丙', score: 0.03, signal: 'sell', expected_price: null, expected_price_source: null },
      ],
      {
        runId: 'run-20261019-abcdef',
        inferenceDate: '2026-10-19',
        targetDate: '2026-10-20',
        modelId: 'mdl_demo',
        total: 3,
      },
    );

    expect(csv).toContain('参考价');
    expect(csv).toContain('实时');
    expect(csv).toContain('昨收');
    expect(csv).toContain('12.34');
    // 缺失行的列数必须与表头一致，且不得带上任何来源角标（缺失就是缺失，不补 0）
    const lines = csv.split('\r\n');
    const header = lines.find((l) => l.startsWith('排名')) ?? '';
    const missingRow = lines.find((l) => l.includes('丙')) ?? '';
    expect(missingRow.split(',').length).toBe(header.split(',').length);
    expect(missingRow).not.toContain('实时');
    expect(missingRow).not.toContain('昨收');
    for (const word of BANNED_WORDS) {
      expect(csv).not.toContain(word);
    }
  });
});

describe('RankingList 合规展示', () => {
  it('不再用涨跌箭头暗示方向，改为位置标签', () => {
    const { container } = render(
      <RankingList rankings={makeRows()} loading={false} onSelect={() => undefined} />,
    );

    expect(container.textContent).not.toContain('↑');
    expect(container.textContent).not.toContain('↓');
    expect(screen.getAllByText('靠前').length).toBeGreaterThan(0);
    expect(screen.getAllByText('居中').length).toBeGreaterThan(0);
    expect(screen.getAllByText('靠后').length).toBeGreaterThan(0);
    for (const word of BANNED_WORDS) {
      expect(container.textContent).not.toContain(word);
    }
  });
});

describe('ModelScoreCurveGrid 评级译法', () => {
  it('把后端已知评级归一为位置词（STRONG_BUY 是已知枚举，不是未知值）', () => {
    expect(ratingPositionLabel('STRONG_BUY')).toBe('靠前');
    expect(ratingPositionLabel('BUY')).toBe('靠前');
    expect(ratingPositionLabel('HOLD')).toBe('居中');
    expect(ratingPositionLabel('SELL')).toBe('靠后');
    expect(ratingPositionLabel('SOMETHING_ELSE')).toBe('—');
    expect(ratingPositionLabel(null)).toBe('—');
  });

  it('卡片页脚渲染位置词，不渲染后端枚举', () => {
    const { container } = render(
      <ModelScoreCurveGrid
        consensus={[
          {
            model_id: 'm1',
            model_name: '模型一',
            model_type: 'lightgbm',
            score: 0.12,
            expected_return: 0.01,
            rating: 'STRONG_BUY',
            horizon: 10,
          },
          {
            model_id: 'm2',
            model_name: '模型二',
            model_type: 'xgboost',
            score: -0.02,
            expected_return: -0.01,
            rating: 'SELL',
            horizon: 10,
          },
        ]}
        consensusScore={50}
        suffixSymbol="600001.SH"
        asOfDate="2026-10-19"
      />,
    );

    expect(screen.getAllByText('靠前').length).toBeGreaterThan(0);
    expect(screen.getAllByText('靠后').length).toBeGreaterThan(0);
    expect(container.textContent).not.toContain('STRONG_BUY');
    expect(container.textContent).not.toContain('SELL');
    for (const word of BANNED_WORDS) {
      expect(container.textContent).not.toContain(word);
    }
  });
});

describe('源码扫描：展示组件不得写回买卖字眼', () => {
  const files = [
    'components/CrossSectionTable.tsx',
    'components/RankingList.tsx',
    'components/ModelScoreCurveGrid.tsx',
    'components/ModelConsensusPanel.tsx',
    'components/IndividualWorkbench.tsx',
    'positionLabels.ts',
    // 截面选股「历史统计」：单模型 TopK 统计面板与口径纯函数
    'components/crossstats/CrossSectionWorkspace.tsx',
    'components/crossstats/SelectionStatsPanel.tsx',
    'components/crossstats/selectionStatsModel.ts',
  ];

  it('固定文件清单非空且存在', () => {
    expect(files.length).toBeGreaterThan(0);
    for (const rel of files) {
      const abs = path.resolve(__dirname, '..', rel);
      expect(readFileSync(abs, 'utf-8').length).toBeGreaterThan(0);
    }
  });

  it('不含买入/卖出/加仓/减仓/建仓/清仓', () => {
    for (const rel of files) {
      const text = readFileSync(path.resolve(__dirname, '..', rel), 'utf-8');
      for (const word of BANNED_WORDS) {
        expect(text, `${rel} 不应出现「${word}」`).not.toContain(word);
      }
      // 单字「买/卖」也要拦：它们通常以「买卖」「买点」等形式漏网
      expect(text, `${rel} 不应出现「买」`).not.toContain('买');
      expect(text, `${rel} 不应出现「卖」`).not.toContain('卖');
    }
  });
});
