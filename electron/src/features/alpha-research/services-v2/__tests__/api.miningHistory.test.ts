/**
 * 挖掘历史 API 桥 —— 参数/信封/方向透传（机构级 P0 / T-FM-04 的服务层）。
 *
 * 端点 `GET /api/v1/alpha-agent/tasks/history` 返回
 * `{code, data:{tasks,total,limit,offset}}`。三条要钉的边：
 *
 * 1. **过滤参数只在有值时出现**：`market/status` 传 undefined 不能变成
 *    字符串 "undefined" 打到后端（后端会把它当未知状态 → 400）。
 * 2. **limit/offset 总是显式带**（分页由前端驱动，后端默认 50 不许靠默认装懂）。
 * 3. **normalizeAgentTask 从 direction 建 config.userInput**：监视器
 *    （GET /tasks）与状态接口现在都返回 direction，此前 configHint 缺省是
 *    `{userInput: ''}`，任务行永远显示不出方向。
 */
import { describe, test, expect, vi, beforeEach } from 'vitest';

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: getMock, post: vi.fn() },
}));

import { getMiningHistory, listTasks, getMiningStatus } from '../api';

function row() {
  return {
    task_id: 't-1',
    user_id: 'u-1',
    market: 'a_share',
    universe: 'csi300',
    data_source: 'parquet',
    direction: '动量反转',
    source: 'text',
    doc_id: null,
    status: 'completed',
    progress_pct: 100,
    current_loop: 3,
    loop_n: 3,
    error: null,
    factor_count: 5,
    created_at: '2026-10-09T00:00:00Z',
    updated_at: '2026-10-09T01:00:00Z',
    completed_at: '2026-10-09T01:00:00Z',
  };
}

beforeEach(() => {
  getMock.mockReset();
  getMock.mockResolvedValue({ data: { code: 200, data: { tasks: [row()], total: 1 } } });
});

describe('getMiningHistory', () => {
  test('无过滤时 URL 只有 limit/offset；返回行原样透传', async () => {
    const r = await getMiningHistory();
    expect(getMock).toHaveBeenCalledTimes(1);
    const url: string = getMock.mock.calls[0][0];
    expect(url.startsWith('/alpha-agent/tasks/history?')).toBe(true);
    expect(url).toContain('limit=50');
    expect(url).toContain('offset=0');
    expect(url).not.toContain('market=');
    expect(url).not.toContain('status=');

    expect(r.success).toBe(true);
    expect(r.data?.tasks[0].direction).toBe('动量反转');
    expect(r.data?.tasks[0].factor_count).toBe(5);
    expect(r.data?.total).toBe(1);
  });

  test('过滤与分页进查询参数', async () => {
    await getMiningHistory({ market: 'a_share', status: 'failed', limit: 20, offset: 40 });
    const url: string = getMock.mock.calls[0][0];
    expect(url).toContain('market=a_share');
    expect(url).toContain('status=failed');
    expect(url).toContain('limit=20');
    expect(url).toContain('offset=40');
  });

  test('空信封（后端异常形状）不炸：空数组 + total 0', async () => {
    getMock.mockResolvedValue({ data: {} });
    const r = await getMiningHistory();
    expect(r.success).toBe(true);
    expect(r.data).toEqual({ tasks: [], total: 0 });
  });
});

describe('normalizeAgentTask：direction → config.userInput（监视器方向摘要的来源）', () => {
  test('listTasks 的行带方向进 config.userInput', async () => {
    getMock.mockResolvedValue({
      data: {
        code: 200,
        data: { tasks: [{ task_id: 't-9', status: 'running', direction: '尾盘资金流' }] },
      },
    });
    const r = await listTasks();
    expect(r.data?.tasks[0].config.userInput).toBe('尾盘资金流');
  });

  test('getMiningStatus 的行同样带方向；缺 direction 时是空串而不是 undefined', async () => {
    getMock.mockResolvedValue({
      data: { code: 200, data: { task_id: 't-9', status: 'completed', direction: '动量' } },
    });
    const r = await getMiningStatus('t-9');
    expect(r.data?.task.config.userInput).toBe('动量');

    getMock.mockResolvedValue({
      data: { code: 200, data: { task_id: 't-10', status: 'completed' } },
    });
    const r2 = await getMiningStatus('t-10');
    expect(r2.data?.task.config.userInput).toBe('');
  });
});
