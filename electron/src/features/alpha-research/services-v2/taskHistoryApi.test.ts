import { beforeEach, describe, expect, it, vi } from 'vitest';

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../../../services/aiStrategyClients', () => ({ apiClient: client }));

import { archiveMiningTask, cancelMining, getMiningResult, getTaskHistory, getTaskLog, getTaskMiningResult, getTaskTrajectory, restoreMiningTask } from './taskHistoryApi';

describe('task history API', () => {
  beforeEach(() => vi.clearAllMocks());

  it('uses the owner-scoped history endpoint with pagination and status filter', async () => {
    const page = { tasks: [], total: 0, limit: 20, offset: 40 };
    const controller = new AbortController();
    client.get.mockResolvedValue({ data: { data: page } });

    await expect(getTaskHistory({ limit: 20, offset: 40, status: 'failed' }, controller.signal)).resolves.toEqual(page);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/tasks/history?limit=20&offset=40&status=failed',
      { signal: controller.signal },
    );
  });

  it('can include archived records in the owner-scoped history query', async () => {
    const page = { tasks: [], total: 0, limit: 20, offset: 0 };
    client.get.mockResolvedValue({ data: { data: page } });
    await getTaskHistory({ limit: 20, offset: 0, includeArchived: true });
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/tasks/history?limit=20&offset=0&include_archived=true',
      { signal: undefined },
    );
  });

  it('bounds task logs and forwards cancellation to private task actions', async () => {
    const controller = new AbortController();
    client.get.mockResolvedValue({ data: { data: { lines: ['line'], total: 1 } } });
    client.post.mockResolvedValue({ data: { code: 200 } });

    await expect(getTaskLog('task-1', controller.signal)).resolves.toEqual({ lines: ['line'], total: 1 });
    expect(client.get).toHaveBeenCalledWith('/alpha-agent/tasks/task-1/log?tail=500&offset=0', { signal: controller.signal });
    await cancelMining('task-1', controller.signal);
    expect(client.post).toHaveBeenCalledWith('/alpha-agent/tasks/task-1/cancel', undefined, { signal: controller.signal });
  });

  it('archives only through the authenticated task action and encodes task IDs', async () => {
    const controller = new AbortController();
    client.post.mockResolvedValue({ data: { code: 200 } });
    await archiveMiningTask('task/1', controller.signal);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/tasks/task%2F1/archive',
      undefined,
      { signal: controller.signal },
    );
    await restoreMiningTask('task/1', controller.signal);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/tasks/task%2F1/restore',
      undefined,
      { signal: controller.signal },
    );
  });

  it('reads statistical results through the owner-checked task result endpoint', async () => {
    const controller = new AbortController();
    const result = { selected: ['candidate-1'], candidates: [{ key: 'candidate-1', name: '动量', expression: 'Ref($close, 1)', selected: true }] };
    client.get.mockResolvedValue({ data: { data: { task_id: 'task/1', algorithm: 'statistical', status: 'completed', result, factors: [] } } });

    await expect(getMiningResult('task/1', controller.signal)).resolves.toEqual(result);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/tasks/task%2F1/result',
      { signal: controller.signal },
    );
  });

  it('reads persisted RD-Agent factors through the unified task result contract', async () => {
    const controller = new AbortController();
    const result = {
      task_id: 'task-rd',
      algorithm: 'rdagent',
      status: 'completed',
      result: null,
      factors: [{ factor_id: 'factor-1', factor_name: '动量', factor_formulation: 'Ref($close, 5)', status: 'pending', rank_ic: 0.12, factor_revision_no: 1 }],
    };
    client.get.mockResolvedValue({ data: { data: result } });
    await expect(getTaskMiningResult('task-rd', controller.signal)).resolves.toEqual(result);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/tasks/task-rd/result',
      { signal: controller.signal },
    );
  });

  it('reads the bounded RD-Agent trajectory with an encoded task ID', async () => {
    const controller = new AbortController();
    const trajectory = { task_id: 'task/1', loops: [], attribution: 'workspace_metadata_only' };
    client.get.mockResolvedValue({ data: { data: trajectory } });

    await expect(getTaskTrajectory('task/1', controller.signal)).resolves.toEqual(trajectory);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/tasks/task%2F1/trajectory',
      { signal: controller.signal },
    );
  });
});
