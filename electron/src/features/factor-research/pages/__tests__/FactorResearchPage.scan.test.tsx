/**
 * 因子研究页 —— 「扫描」入口的接线契约。
 *
 * 这条线最容易悄悄坏掉的地方不是扫描本身（那由 ScanPanel 的用例守），而是**入口的可见性**
 * 与**两块面板的交接**：
 *
 * 1. 扫描只对私人库有意义。经典库的因子目录来自内置 catalog.py，没有「盘上有什么」
 *    可对，后端对 classic 是 400。若按钮在经典库下也渲染，用户点下去只会看到一句
 *    报错 —— 入口可见性必须跟着数据集走；
 * 2. 「重算快照」必须交回快照面板。前端只有那一边实现构建（进度轮询 + 日志），
 *    在扫描面板里再发一次 /build 就会有两份互不知情的进度状态。
 *
 * 子组件在这里换成桩：本文件测的是页面的接线，不是子组件的渲染（各有各的用例）。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import FactorResearchPage from '../FactorResearchPage';

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

// 子组件桩：只留出「是否被渲染」这一个信号
vi.mock('../../components/CatalogSidebar', () => ({ CatalogSidebar: () => <div data-testid="sidebar" /> }));
vi.mock('../../components/LeaderboardTab', () => ({ LeaderboardTab: () => <div data-testid="tab-leaderboard" /> }));
vi.mock('../../components/SingleFactorTab', () => ({ SingleFactorTab: () => null }));
vi.mock('../../components/CompareTab', () => ({ CompareTab: () => null }));
vi.mock('../../components/ComposeTab', () => ({ ComposeTab: () => null }));
vi.mock('../../components/ScreeningTab', () => ({ ScreeningTab: () => null }));
vi.mock('../../components/RegisterToTrainingModal', () => ({ RegisterToTrainingModal: () => null }));
vi.mock('../../components/factor-report/FactorReportPanel', () => ({ FactorReportPanel: () => null }));
vi.mock('../../components/SnapshotPanel', () => ({
  SnapshotPanel: () => <div data-testid="panel-snapshot" />,
}));
vi.mock('../../components/ScanPanel', () => ({
  ScanPanel: ({ onRebuild, onClose }: { onRebuild: () => void; onClose: () => void }) => (
    <div data-testid="panel-scan">
      <button onClick={onRebuild}>桩-重算</button>
      <button onClick={onClose}>桩-关闭</button>
    </div>
  ),
}));

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

describe('FactorResearchPage 扫描入口', () => {
  test('私人因子库下渲染「扫描」按钮（默认数据集即私人库）', async () => {
    renderPage();

    expect(await screen.findByTestId('open-scan')).toBeTruthy();
  });

  test('切到经典因子后不渲染扫描按钮（经典库无可扫描的来源，后端会 400）', async () => {
    renderPage();
    await screen.findByTestId('open-scan');

    fireEvent.click(screen.getByText('经典因子'));

    await waitFor(() => expect(screen.queryByTestId('open-scan')).toBeNull());
    // 「快照」按钮是数据集无关的，不能跟着一起消失
    expect(screen.getByText('快照')).toBeTruthy();
  });

  test('点「扫描」打开扫描面板；再点收起', async () => {
    renderPage();

    fireEvent.click(await screen.findByTestId('open-scan'));
    expect(await screen.findByTestId('panel-scan')).toBeTruthy();

    fireEvent.click(screen.getByTestId('open-scan'));
    await waitFor(() => expect(screen.queryByTestId('panel-scan')).toBeNull());
  });

  test('面板里的「重算快照」交回快照面板（前端只有一处发起构建）', async () => {
    renderPage();

    fireEvent.click(await screen.findByTestId('open-scan'));
    fireEvent.click(await screen.findByText('桩-重算'));

    expect(await screen.findByTestId('panel-snapshot')).toBeTruthy();
    expect(screen.queryByTestId('panel-scan')).toBeNull();
  });

  test('切数据集时关掉工具面板（否则会留下一块讲不通的旧结果）', async () => {
    renderPage();

    fireEvent.click(await screen.findByTestId('open-scan'));
    await screen.findByTestId('panel-scan');

    fireEvent.click(screen.getByText('经典因子'));

    await waitFor(() => expect(screen.queryByTestId('panel-scan')).toBeNull());
  });
});
