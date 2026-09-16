/**
 * 信息通知卡：体检告警类型（health）渲染 + 策略类通知导航（T-P4-06 通知接线）。
 * 用真实 redux store 与真实 zustand backtestCenterStore（只 mock 数据源 hook）。
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../../hooks/useNotifications', () => ({
  useNotifications: () => ({
    notifications: [
      {
        id: 1,
        title: '体检复检：E2E策略 结论退化 A → L',
        content: '月度复检结论由 A 退化为 L（可信度 30）。t 检验不过。',
        type: 'health',
        level: 'error',
        is_read: false,
        created_at: new Date().toISOString(),
        action_url: '/strategy',
      },
    ],
    loading: false,
    unreadCount: 1,
    typeCounts: { health: 1 },
    total: 1,
    hasMore: false,
    loadMore: vi.fn(),
    markAsRead: vi.fn().mockResolvedValue(undefined),
    markAllAsRead: vi.fn(),
    clearNotifications: vi.fn(),
    refresh: vi.fn(),
    connectRealtime: vi.fn(),
    disconnectRealtime: vi.fn(),
  }),
  resolveNotificationTarget: () => 'strategy',
  getNavigationHint: () => '策略管理',
}));

import store from '../../../store';
import { useBacktestCenterStore } from '../../../stores/backtestCenterStore';
import { NotificationQuickCard } from '../NotificationQuickCard';

const renderCard = () =>
  render(
    <Provider store={store}>
      <MemoryRouter>
        <NotificationQuickCard />
      </MemoryRouter>
    </Provider>
  );

describe('NotificationQuickCard 体检告警（T-P4-06 通知接线）', () => {
  it('health 类型通知渲染：体检统计格 + 条目', () => {
    renderCard();
    expect(screen.getByText('体检')).toBeTruthy(); // 第 5 统计格
    expect(screen.getByText(/结论退化 A → L/)).toBeTruthy();
  });

  it('点击策略类通知 → 落策略管理模块（此前无分支=点了不跳）', async () => {
    useBacktestCenterStore.getState().setActiveModule('quick-backtest');
    const user = userEvent.setup();
    renderCard();
    await user.click(screen.getByText(/结论退化 A → L/));
    await vi.waitFor(() =>
      expect(useBacktestCenterStore.getState().activeModule).toBe('strategy-management')
    );
  });
});
