/**
 * 因子研究页 —— 「左侧分类 → 右侧排行榜限定」的接线契约。
 *
 * 这条线有三个容易悄悄坏掉的点（都在页面上，不在子组件里）：
 *
 * 1. 点分类时若正停在单因子/合成等页签，限定只作用于排行榜 —— 页面必须自动切回
 *    排行榜，否则用户点了没有任何可见变化，会以为点了没反应；
 * 2. 切数据集必须清空限定 —— l1/l2 是各库自己的命名空间（经典库 l2 是「动量」
 *    这类中文名），残留私人库的分类名去筛经典库只会得到一块空榜；
 * 3. 目录加载态要一路传到左侧（目录 1.1 MB，首载数秒），否则空目录会被说成
 *    「无匹配因子」。
 *
 * 子组件在这里换成桩：测的是接线，不是子组件渲染（各有各的用例）。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import FactorResearchPage from '../FactorResearchPage';
import type { CategoryFilter } from '../../types/factorResearch';

const { catalogMock, leaderboardMock } = vi.hoisted(() => ({
  catalogMock: vi.fn(),
  leaderboardMock: vi.fn(),
}));

vi.mock('../../services/factorResearchService', async () => {
  const actual = await vi.importActual<typeof import('../../services/factorResearchService')>(
    '../../services/factorResearchService',
  );
  return { ...actual, getCatalog: catalogMock, getLeaderboard: leaderboardMock };
});

vi.mock('../../../../store', () => ({
  useAppSelector: () => false,
}));

// 目录桩：回显页面传下来的 loading 与 categoryFilter，并提供两个触发按钮
vi.mock('../../components/CatalogSidebar', () => ({
  CatalogSidebar: ({
    loading,
    categoryFilter,
    onSelectCategory,
  }: {
    loading?: boolean;
    categoryFilter: CategoryFilter | null;
    onSelectCategory: (f: CategoryFilter | null) => void;
  }) => (
    <div data-testid="sidebar">
      <span data-testid="sidebar-loading">{loading ? '1' : '0'}</span>
      <span data-testid="sidebar-filter">
        {categoryFilter ? `${categoryFilter.l1}/${categoryFilter.l2 ?? ''}` : 'none'}
      </span>
      <button onClick={() => onSelectCategory({ l1: 'rd_mined', l2: null })}>桩-选分类</button>
      <button onClick={() => onSelectCategory(null)}>桩-清分类</button>
    </div>
  ),
}));

vi.mock('../../components/LeaderboardTab', () => ({
  LeaderboardTab: ({ categoryFilter }: { categoryFilter: CategoryFilter | null }) => (
    <div data-testid="tab-leaderboard">
      {categoryFilter ? `限定:${categoryFilter.l1}` : '全部'}
    </div>
  ),
}));
vi.mock('../../components/SingleFactorTab', () => ({
  SingleFactorTab: () => <div data-testid="tab-single" />,
}));
vi.mock('../../components/CompareTab', () => ({ CompareTab: () => null }));
vi.mock('../../components/ComposeTab', () => ({ ComposeTab: () => null }));
vi.mock('../../components/ScreeningTab', () => ({ ScreeningTab: () => null }));
vi.mock('../../components/RegisterToTrainingModal', () => ({ RegisterToTrainingModal: () => null }));
vi.mock('../../components/factor-report/FactorReportPanel', () => ({ FactorReportPanel: () => null }));
vi.mock('../../components/SnapshotPanel', () => ({ SnapshotPanel: () => null }));
vi.mock('../../components/ScanPanel', () => ({ ScanPanel: () => null }));

const renderPage = () =>
  render(
    <MemoryRouter>
      <FactorResearchPage />
    </MemoryRouter>,
  );

beforeEach(() => {
  catalogMock.mockReset();
  leaderboardMock.mockReset();
  catalogMock.mockResolvedValue({ factors: [], l1_order: [], l2_order: {}, meta: {} });
  leaderboardMock.mockResolvedValue({ leaderboard: [], meta: {} });
});

describe('FactorResearchPage 分类限定接线', () => {
  test('目录在途时把加载态传给左侧目录（区分「加载中」与「无匹配」）', async () => {
    let resolveCatalog: (v: unknown) => void = () => {};
    catalogMock.mockImplementation(
      () => new Promise((res) => { resolveCatalog = res; }),
    );
    renderPage();

    expect(screen.getByTestId('sidebar-loading').textContent).toBe('1');
    resolveCatalog({ factors: [], l1_order: [], l2_order: {}, meta: {} });
    await waitFor(() => expect(screen.getByTestId('sidebar-loading').textContent).toBe('0'));
  });

  test('点分类：自动切回排行榜并把限定传给榜单；清除后回到全部', async () => {
    renderPage();

    // 先离开排行榜，验证「限定只对榜单生效 → 点了就切回去」
    fireEvent.click(screen.getByText('单因子分析'));
    expect(await screen.findByTestId('tab-single')).toBeTruthy();

    fireEvent.click(screen.getByText('桩-选分类'));
    expect(await screen.findByTestId('tab-leaderboard')).toBeTruthy();
    expect(screen.getByTestId('tab-leaderboard').textContent).toBe('限定:rd_mined');
    expect(screen.getByTestId('sidebar-filter').textContent).toBe('rd_mined/');

    fireEvent.click(screen.getByText('桩-清分类'));
    await waitFor(() => expect(screen.getByTestId('tab-leaderboard').textContent).toBe('全部'));
  });

  test('从单因子页签点头部「排行榜」能切回（回排行榜要清 ?tab=，不得被滞后 URL 拽回）', async () => {
    renderPage();

    fireEvent.click(screen.getByText('单因子分析'));
    expect(await screen.findByTestId('tab-single')).toBeTruthy();

    fireEvent.click(screen.getByText('排行榜'));
    expect(await screen.findByTestId('tab-leaderboard')).toBeTruthy();
  });

  test('切数据集清空分类限定（l1/l2 是各库自己的命名空间）', async () => {
    renderPage();

    fireEvent.click(screen.getByText('桩-选分类'));
    await waitFor(() => expect(screen.getByTestId('sidebar-filter').textContent).toBe('rd_mined/'));

    fireEvent.click(screen.getByText('经典因子'));
    await waitFor(() => expect(screen.getByTestId('sidebar-filter').textContent).toBe('none'));
  });

  test('目录可收起（给榜单让出宽度），再点展开还原；收起不影响榜单限定', async () => {
    renderPage();

    fireEvent.click(screen.getByText('桩-选分类'));
    await waitFor(() => expect(screen.getByTestId('tab-leaderboard').textContent).toBe('限定:rd_mined'));

    fireEvent.click(screen.getByTestId('toggle-catalog'));
    expect(screen.queryByTestId('sidebar')).toBeNull();
    // 收起目录只是腾地方：限定状态不该被清掉
    expect(screen.getByTestId('tab-leaderboard').textContent).toBe('限定:rd_mined');

    fireEvent.click(screen.getByTestId('toggle-catalog'));
    expect(screen.getByTestId('sidebar')).toBeTruthy();
  });
});
