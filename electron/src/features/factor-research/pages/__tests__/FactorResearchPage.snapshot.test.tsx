/**
 * 因子研究页 —— 「快照」入口必须是**可达的**（页面 × 真实 SnapshotPanel 的交接契约）。
 *
 * 为什么必须用真的 SnapshotPanel：`FactorResearchPage.scan.test.tsx` 把它桩成一个
 * 静态 div，于是「面板会不会自己收起来」这件事在那条用例里根本不存在 —— 而 bug
 * 恰恰出在这条缝里。
 *
 * 原缺陷（2026-10-07 读码确认）：SnapshotPanel 首轮轮询只要拿到
 * `exists && !running` 就回调 `onReady`，而页面当时的 `onReady` 无条件
 * `setPanel('none')`。于是**只要快照已经存在**（常态），用户点「快照」按钮看到的是
 * 面板一闪而过：里面那个真正发起构建的「重算快照」按钮永远点不到。
 * 扫描面板的「重算快照」走同一条路，同样是死胡同 —— 也正是「扫描 → 重算」这条
 * 主路径承诺的事，实际一次都发生不了。
 *
 * 修好之后的形状：
 * 1. **手动打开**（「快照」按钮 / 扫描面板的重算）→ 就绪后**不**自动收起，
 *    面板里的「重算快照」必须可点、且真的发出构建请求；
 * 2. **自动打开**（榜单 503，快照缺失）→ 建完仍要收起并把数据带回来，
 *    这个既有行为不能被顺手改坏。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import FactorResearchPage from '../FactorResearchPage';

const { catalogMock, leaderboardMock, statusMock, buildMock } = vi.hoisted(() => ({
  catalogMock: vi.fn(),
  leaderboardMock: vi.fn(),
  statusMock: vi.fn(),
  buildMock: vi.fn(),
}));

vi.mock('../../services/factorResearchService', async () => {
  const actual = await vi.importActual<typeof import('../../services/factorResearchService')>(
    '../../services/factorResearchService',
  );
  return {
    ...actual,
    getCatalog: catalogMock,
    getLeaderboard: leaderboardMock,
    getSnapshotStatus: statusMock,
    postBuildSnapshot: buildMock,
  };
});

vi.mock('../../../../store', () => ({ useAppSelector: () => false }));

vi.mock('../../components/CatalogSidebar', () => ({ CatalogSidebar: () => <div /> }));
vi.mock('../../components/LeaderboardTab', () => ({ LeaderboardTab: () => <div data-testid="tab-leaderboard" /> }));
vi.mock('../../components/SingleFactorTab', () => ({ SingleFactorTab: () => null }));
vi.mock('../../components/CompareTab', () => ({ CompareTab: () => null }));
vi.mock('../../components/ComposeTab', () => ({ ComposeTab: () => null }));
vi.mock('../../components/ScreeningTab', () => ({ ScreeningTab: () => null }));
vi.mock('../../components/RegisterToTrainingModal', () => ({ RegisterToTrainingModal: () => null }));
vi.mock('../../components/factor-report/FactorReportPanel', () => ({ FactorReportPanel: () => null }));
// ScanPanel 只留「重算」这一个信号；真实渲染由 ScanPanel.test.tsx 负责
vi.mock('../../components/ScanPanel', () => ({
  ScanPanel: ({ onRebuild }: { onRebuild: () => void }) => (
    <div data-testid="panel-scan">
      <button onClick={onRebuild}>桩-重算</button>
    </div>
  ),
}));

/** 快照已生成且不在构建中 —— 常态，也正是旧实现里「一闪而过」的触发条件 */
const READY_SNAPSHOT = {
  exists: true,
  running: false,
  built_at: '2026-09-19 11:22:33',
  window: ['2016-01-04', '2026-09-30'] as [string, string],
  n_factors: 2752,
  n_dates: 129,
  step: 'done',
  log_tail: [],
};

const renderPage = () =>
  render(
    <MemoryRouter>
      <FactorResearchPage />
    </MemoryRouter>,
  );

/** 等首屏两请求落地，避免断言时页面还在 loading */
const settle = async () => {
  await screen.findByTestId('tab-leaderboard');
};

beforeEach(() => {
  catalogMock.mockReset();
  leaderboardMock.mockReset();
  statusMock.mockReset();
  buildMock.mockReset();
  catalogMock.mockResolvedValue({ factors: [], l1_order: [], l2_order: {}, meta: {} });
  leaderboardMock.mockResolvedValue({ leaderboard: [], meta: {} });
  statusMock.mockResolvedValue(READY_SNAPSHOT);
  buildMock.mockResolvedValue({ started: true, running: true });
});

describe('FactorResearchPage × SnapshotPanel：入口可达性', () => {
  test('点「快照」后面板不会自己收起，里面的「重算快照」可点', async () => {
    renderPage();
    await settle();

    fireEvent.click(screen.getByText('快照'));

    // 面板在首轮轮询（拿到 exists && !running）之后仍然在
    expect(await screen.findByText('快照已生成')).toBeTruthy();
    expect(screen.getByText('重算快照')).toBeTruthy();
  });

  test('面板里的「重算快照」真的发出构建请求（不再是死胡同）', async () => {
    renderPage();
    await settle();

    fireEvent.click(screen.getByText('快照'));
    fireEvent.click(await screen.findByText('重算快照'));

    await waitFor(() => expect(buildMock).toHaveBeenCalledWith('private'));
  });

  test('扫描面板 →「重算快照」也落到可点的按钮上（这条主路径此前一次都走不通）', async () => {
    renderPage();
    await settle();

    fireEvent.click(screen.getByTestId('open-scan'));
    fireEvent.click(await screen.findByText('桩-重算'));

    expect(await screen.findByText('重算快照')).toBeTruthy();
    fireEvent.click(screen.getByText('重算快照'));

    await waitFor(() => expect(buildMock).toHaveBeenCalledWith('private'));
  });

  test('「快照」按钮可以收起面板（手动入口不能只开不关）', async () => {
    renderPage();
    await settle();

    fireEvent.click(screen.getByText('快照'));
    await screen.findByText('重算快照');

    fireEvent.click(screen.getByText('快照'));

    await waitFor(() => expect(screen.queryByText('重算快照')).toBeNull());
  });

  test('榜单 503 自动弹出时，快照就绪后仍要自动收起（既有行为不能被改坏）', async () => {
    const { ApiError } = await import('../../services/factorResearchService');
    leaderboardMock.mockRejectedValue(new ApiError(503, '因子快照不完整'));

    renderPage();
    await waitFor(() => expect(leaderboardMock).toHaveBeenCalled());

    // 自动弹出的面板在首轮轮询后就该收起
    await waitFor(() => expect(screen.queryByText('重算快照')).toBeNull());
    // 收起后榜单并没有回来（mock 一直 503），页面停在 503 分支上——
    // 这正是「继续自动弹」的燃料，见下一条用例。
    expect(screen.queryByTestId('tab-leaderboard')).toBeNull();
  });

  test('榜单持续 503 时不能变成自转的请求循环（自动弹出只允许发生一次）', async () => {
    const { ApiError } = await import('../../services/factorResearchService');
    // 最坏情况：状态接口说「就绪」而榜单一直 503（快照文件在但不完整）。
    // 旧实现的循环：弹出 → onReady → 收起 + reloadKey++ → 榜单又 503 → 再弹出……
    leaderboardMock.mockRejectedValue(new ApiError(503, '因子快照不完整'));

    renderPage();
    await waitFor(() => expect(leaderboardMock).toHaveBeenCalled());

    // 循环若存在，这一段等待里调用数会一直涨（每轮一次）——
    // 用「静置前后计数不变」而不是绝对值来钉住它（首屏次数不该被测试写死）。
    await waitFor(() => expect(screen.queryByText('重算快照')).toBeNull());
    await new Promise((r) => setTimeout(r, 300));
    const settled = leaderboardMock.mock.calls.length;

    await new Promise((r) => setTimeout(r, 300));
    expect(leaderboardMock.mock.calls.length).toBe(settled);

    // 面板不再自动弹，用户拿到的是带手动入口的 503 提示
    expect(screen.queryByText('重算快照')).toBeNull();
    expect(screen.getByText('→ 打开快照计算面板')).toBeTruthy();
  });
});
