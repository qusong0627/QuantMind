/**
 * 全局更新提醒的可见行为。
 *
 * 钉住三条：
 * 1) 落后 > 0 时必须写明「哪个分支、几个提交」——只写「有新版本」用户无法判断
 *    该不该现在停机更新；
 * 2) 管理员有「查看并更新」入口，普通用户没有（更新接口是 admin-only）；
 * 3) 「稍后」只压住当前这组版本，同一组不再重复弹；新提交出现时重新提醒。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';

const mockRefresh = vi.fn();
let mockUpdate: Record<string, unknown> | null = null;

vi.mock('../../../hooks/useSystemUpdateCheck', () => ({
  useSystemUpdateCheck: () => ({
    version: undefined,
    update: mockUpdate,
    isLoading: false,
    isFetching: false,
    refresh: mockRefresh,
  }),
}));

import { SystemUpdateNotifier } from '../SystemUpdateNotifier';

function makeStore(isAdmin: boolean) {
  return configureStore({
    reducer: {
      auth: (state = { user: { is_admin: isAdmin } }) => state,
    },
  });
}

function renderNotifier(isAdmin = true) {
  return render(
    <Provider store={makeStore(isAdmin)}>
      <MemoryRouter>
        <SystemUpdateNotifier />
      </MemoryRouter>
    </Provider>,
  );
}

const updateInfo = (overrides: Record<string, unknown> = {}) => ({
  behind: 3,
  behind_capped: false,
  upstream_branch: 'next',
  current_short: '27cb9bc0',
  latest_short: 'abcdef12',
  latest_commit: 'abcdef1234567890',
  latest_subject: 'feat: 新增更新提醒',
  checked_at: 0,
  is_up_to_date: false,
  ...overrides,
});

beforeEach(() => {
  sessionStorage.clear();
  mockUpdate = null;
  mockRefresh.mockReset();
});

describe('SystemUpdateNotifier', () => {
  it('落后时浮出提醒，写明分支与提交数', () => {
    mockUpdate = updateInfo();
    renderNotifier();

    expect(screen.getByTestId('system-update-notifier')).toBeInTheDocument();
    expect(screen.getByText('发现新版本')).toBeInTheDocument();
    expect(screen.getByText('落后 3 个提交')).toBeInTheDocument();
    expect(screen.getByText('next')).toBeInTheDocument();
    expect(screen.getByText(/当前/)).toHaveTextContent('27cb9bc0 → 最新 abcdef12');
    expect(screen.getByText(/新增更新提醒/)).toBeInTheDocument();
  });

  it('管理员有更新入口，普通用户只有提示', () => {
    mockUpdate = updateInfo();

    const { unmount } = renderNotifier(true);
    expect(screen.getByRole('button', { name: /查看并更新/ })).toBeInTheDocument();
    unmount();

    sessionStorage.clear();
    renderNotifier(false);
    expect(screen.queryByRole('button', { name: /查看并更新/ })).not.toBeInTheDocument();
    expect(screen.getByText(/请联系管理员/)).toBeInTheDocument();
  });

  it('点「稍后」后同一组版本不再提示', async () => {
    mockUpdate = updateInfo();
    const user = userEvent.setup();
    const { unmount } = renderNotifier();

    await user.click(screen.getByRole('button', { name: '稍后' }));
    expect(screen.queryByTestId('system-update-notifier')).not.toBeInTheDocument();
    expect(sessionStorage.getItem('qm:system-update:dismissed')).toBe(
      '27cb9bc0:abcdef1234567890:3',
    );

    // 同一组版本重新挂载（模拟页面切换）→ 仍然压住
    unmount();
    renderNotifier();
    expect(screen.queryByTestId('system-update-notifier')).not.toBeInTheDocument();
  });

  it('上游又前进时，旧的「稍后」不再生效', async () => {
    mockUpdate = updateInfo();
    const user = userEvent.setup();
    const { unmount } = renderNotifier();
    await user.click(screen.getByRole('button', { name: '稍后' }));
    unmount();

    mockUpdate = updateInfo({
      behind: 4,
      latest_short: '99999999',
      latest_commit: '9999999988887777',
    });
    renderNotifier();

    expect(screen.getByTestId('system-update-notifier')).toBeInTheDocument();
    expect(screen.getByText('落后 4 个提交')).toBeInTheDocument();
  });

  it('已最新 / 无检查结果时不渲染', () => {
    const { unmount } = renderNotifier();
    expect(screen.queryByTestId('system-update-notifier')).not.toBeInTheDocument();
    unmount();

    mockUpdate = updateInfo({ behind: 0, is_up_to_date: true });
    renderNotifier();
    expect(screen.queryByTestId('system-update-notifier')).not.toBeInTheDocument();
  });
});
