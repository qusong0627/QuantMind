import React from 'react';
import { Provider } from 'react-redux';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import store from '../../../store';
import { invalidateSession } from '../../../store/sessionBoundary';
import { getSessionScopedStorageKey } from '../../../store/sessionScopedStorage';
import { TaskProvider, useTaskContext } from './TaskContext';

const api = vi.hoisted(() => ({
  startMining: vi.fn(),
  previewMining: vi.fn(),
  getMiningStatus: vi.fn(),
  cancelMining: vi.fn(),
  listTasks: vi.fn(),
  startBacktest: vi.fn(),
  getBacktestStatus: vi.fn(),
  cancelBacktest: vi.fn(),
  connectMiningWs: vi.fn(),
  healthCheck: vi.fn(),
}));

vi.mock('../services-v2/api', () => api);
vi.mock('../utils-v2/miningDirections', () => ({
  getDefaultMiningDirection: () => '价量因子挖掘',
  getStoredDirectionConfig: () => ({ labels: [], mode: 'selected' }),
}));

function TaskProbe() {
  const { miningTask } = useTaskContext();
  return (
    <div>
      <span data-testid="task-id">{miningTask?.taskId ?? 'empty'}</span>
    </div>
  );
}

function PreviewThenStartProbe() {
  const { previewMining, startMining } = useTaskContext();
  const config = {
    userInput: '稳定配置预览',
    algorithm: 'rdagent' as const,
    miningMarket: 'a_share' as const,
    universe: 'csi500' as const,
    freq: 'day' as const,
  };
  return (
    <button type="button" onClick={async () => {
      await previewMining(config);
      const key = getSessionScopedStorageKey('autoalpha_config');
      expect(key).not.toBeNull();
      localStorage.setItem(key!, JSON.stringify({ defaultMaxRounds: 9 }));
      startMining(config);
    }}>预览并提交</button>
  );
}

describe('TaskProvider session boundary', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.healthCheck.mockResolvedValue(true);
    api.listTasks.mockResolvedValue({ data: { tasks: [] } });
    localStorage.setItem('user', JSON.stringify({ tenant_id: 'test-tenant', user_id: 'task-context-test' }));
    const key = getSessionScopedStorageKey('autoalpha_config');
    expect(key).not.toBeNull();
    localStorage.setItem(key!, JSON.stringify({ defaultMaxRounds: 3 }));
    api.connectMiningWs.mockReturnValue({ close: vi.fn(), _pollingTimeoutId: null });
    store.dispatch({ type: 'auth/login/fulfilled', payload: {
      require_mfa: false,
      user: { user_id: 'task-context-test' },
      token: 'test-token',
      refreshToken: null,
      tokenExpiryTime: null,
    } });
    expect(store.getState().auth.isAuthenticated).toBe(true);
    expect(store.getState().auth.isLoading).toBe(false);
  });

  afterEach(() => {
    cleanup();
    invalidateSession();
    localStorage.clear();
  });

  it('clears private task state when the login session changes', async () => {
    api.listTasks.mockResolvedValue({ data: { tasks: [{
      taskId: 'private-task-a',
      status: 'running',
      config: {},
      progress: { phase: 'parsing', currentRound: 0, totalRounds: 1, progress: 0, message: '', timestamp: '' },
      logs: [],
      createdAt: '',
      updatedAt: '',
    }] } });
    render(
      <Provider store={store}>
        <TaskProvider><TaskProbe /></TaskProvider>
      </Provider>,
    );

    await waitFor(() => expect(screen.getByTestId('task-id').textContent).toBe('private-task-a'));

    act(() => { store.dispatch({ type: 'auth/loginWithSmsCode/pending' }); });
    expect(screen.getByTestId('task-id').textContent).toBe('empty');
  });

  it('submits the exact parameter snapshot that the user previewed', async () => {
    api.previewMining.mockResolvedValue({ success: true, data: { algorithm: { key: 'rdagent' } } });
    api.startMining.mockResolvedValue({
      success: true,
      data: {
        taskId: 'previewed-task',
        task: {
          taskId: 'previewed-task', status: 'queued', config: {}, progress: {}, logs: [],
          createdAt: '', updatedAt: '',
        },
      },
    });
    render(
      <Provider store={store}>
        <TaskProvider><PreviewThenStartProbe /></TaskProvider>
      </Provider>,
    );

    act(() => { screen.getByRole('button', { name: '预览并提交' }).click(); });
    await waitFor(() => expect(api.startMining).toHaveBeenCalledTimes(1));
    expect(api.startMining.mock.calls[0][0]).toEqual(api.previewMining.mock.calls[0][0]);
    expect(api.startMining.mock.calls[0][0].maxRounds).toBe(3);
  });
});
