/**
 * FactorLibraryPage —— 视图切换与「列表默认」的用户反馈落点。
 *
 * 用户原话：「因子库界面难看；显示方式可以选择吗，现在是方块的、列表不能显示吗？」
 * 钉死的边：
 * - 默认 = 列表（FactorTable 密集表），且 13 个因子全部可见（第 11 个也不截）；
 * - 切到卡片后持久化到 localStorage（qa_factor_lib_view='cards'），重新挂载仍在卡片；
 * - 「AI 解读」「导出IDE」两个被移除的入口在因子库上不复现。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { FactorLibraryPage } from '../FactorLibraryPage';

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
  backtestClient: { get: vi.fn(), post: vi.fn() },
}));

// vi.mock 工厂被提升到文件顶部执行——夹具必须走 vi.hoisted，否则工厂里
// 引用普通 const 会在初始化前被求值（ReferenceError）
const { libraryFactors } = vi.hoisted(() => ({
  libraryFactors: Array.from({ length: 13 }, (_, i) => ({
    factorId: `f${i + 1}`,
    factorName: `因子${i + 1}`,
    factorExpression: 'close/mean(close,5)',
    factorDescription: '',
    quality: 'medium' as const,
    market: 'a_share',
    universe: 'csi300',
    ic: i === 0 ? 0.0321 : undefined, // 缺失保持 undefined（显「—」）
    round: 0,
    direction: '正向',
    createdAt: '2026-10-01T00:00:00Z',
  })),
}));

vi.mock('../../services-v2/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services-v2/api')>();
  return {
    ...actual, // classifyQuality / UNIVERSE_LABELS 等保持真实
    getFactors: vi.fn().mockResolvedValue({
      success: true,
      data: {
        factors: libraryFactors,
        total: libraryFactors.length,
        limit: 200,
        offset: 0,
        serverLimit: 500,
      },
    }),
    getUniverses: vi.fn().mockResolvedValue({ success: true, data: { universes: [] } }),
    getFactoryFactors: vi.fn().mockResolvedValue({
      success: true,
      data: { factors: [], generatedAt: null },
    }),
    getFactorDetail: vi.fn().mockResolvedValue({ success: true, data: { factor: null } }),
  };
});

vi.mock('../../services/alphaAgentService', () => ({
  alphaAgentService: {
    listMarkets: vi.fn().mockResolvedValue([]),
    promoteByExpression: vi.fn(),
  },
}));

vi.mock('../../context-v2/TaskContext', () => ({
  useTaskContext: () => ({
    attachBacktestTask: vi.fn().mockResolvedValue(undefined),
    refreshMiningFactors: vi.fn().mockResolvedValue(undefined),
  }),
}));

vi.mock('../../context-v2/RunQueueContext', () => ({
  useBacktestQueue: () => ({
    entries: {},
    activeCount: 0,
    enqueue: vi.fn(),
    cancel: vi.fn(),
    reset: vi.fn(),
  }),
  useMaterializeRun: () => ({
    running: false,
    runningIds: new Set<string>(),
    lastResult: null,
    warning: null,
    start: vi.fn(),
    refresh: vi.fn().mockResolvedValue(undefined),
    clearResult: vi.fn(),
  }),
}));

const noop = vi.fn();

beforeEach(() => {
  localStorage.clear();
  noop.mockReset();
});

describe('FactorLibraryPage：列表默认 + 视图切换持久化', () => {
  test('默认列表视图：13 行全渲染（第 11 个也可见），localStorage 记 list', async () => {
    const { container } = render(<FactorLibraryPage onNavigate={noop} />);

    // 等清单落地
    await screen.findByText('因子13');
    expect(screen.getByText(/共 13 行/)).toBeTruthy();
    expect(container.querySelectorAll('tbody tr')).toHaveLength(13);
    expect(screen.getByText('因子11')).toBeTruthy();

    expect(localStorage.getItem('qa_factor_lib_view')).toBe('list');
    // 被移除的两个入口不复现
    expect(screen.queryByText(/AI 解读/)).toBeNull();
    expect(screen.queryByText(/导出IDE/)).toBeNull();
  });

  test('切到卡片 → 持久化为 cards；重新挂载仍是卡片；再切回列表', async () => {
    const first = render(<FactorLibraryPage onNavigate={noop} />);
    await screen.findByText('因子13');

    fireEvent.click(screen.getByTitle('卡片视图'));

    expect(localStorage.getItem('qa_factor_lib_view')).toBe('cards');
    // 表格（列表视图）消失，卡片仍在
    expect(screen.queryByText(/共 13 行/)).toBeNull();
    expect(screen.getByText('因子11')).toBeTruthy();

    // 重新挂载 = 刷新页面：视图从 localStorage 恢复
    first.unmount();
    render(<FactorLibraryPage onNavigate={noop} />);
    await screen.findByText('因子13');
    await waitFor(() =>
      expect(screen.getByTitle('卡片视图').getAttribute('aria-pressed')).toBe('true'),
    );
    expect(screen.queryByText(/共 13 行/)).toBeNull();

    // 切回列表
    fireEvent.click(screen.getByTitle('列表视图'));
    expect(localStorage.getItem('qa_factor_lib_view')).toBe('list');
    await screen.findByText(/共 13 行/);
  });
});
