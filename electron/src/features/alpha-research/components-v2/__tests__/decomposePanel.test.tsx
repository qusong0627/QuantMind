import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { DecomposePanel, composeDirection } from '../DecomposePanel';
import { normalizeAgentTask } from '../../services-v2/api';
import type { DecomposeCard } from '../../services-v2/api';

/**
 * 拆解面板契约：拆解只读不落任务；派发才逐条成任务（回执 index 对齐派发
 * 顺序；失败条目跳过并在完成态列出）；排队回执进注册表时带位次。
 */

const apiMocks = vi.hoisted(() => ({
  decomposeDirection: vi.fn(),
  dispatchMiningBatch: vi.fn(),
}));

vi.mock('../../services-v2/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services-v2/api')>();
  return {
    ...actual,
    decomposeDirection: apiMocks.decomposeDirection,
    dispatchMiningBatch: apiMocks.dispatchMiningBatch,
  };
});

const ctxMocks = vi.hoisted(() => ({ adoptDispatchedTasks: vi.fn() }));

vi.mock('../../context-v2/TaskContext', () => ({
  useTaskContext: () => ({ adoptDispatchedTasks: ctxMocks.adoptDispatchedTasks }),
}));

const CARD_A: DecomposeCard = {
  title: '短期反转',
  hypothesis: '5 日动量反转在多空两端均有超额',
  rationale: '浮盈兑现压力',
  categories: ['momentum'],
  evaluation_hint: '看 rankIC 与分组单调性',
};

const CARD_B: DecomposeCard = {
  title: '量价背离',
  hypothesis: '放量滞涨的股票未来收益更差',
  categories: ['liquidity'],
};

const okDecompose = (cards: DecomposeCard[], dropped = 0) => ({
  success: true,
  data: {
    promptVersion: 'decompose_v1',
    cards,
    dropped,
    maxCards: 6,
    context: { categories: 5, poolDigestChars: 120, poolFactors: 3, model: 'm1' },
  },
});

const receipt = (over: Record<string, unknown>) => ({
  index: 0,
  taskId: 't0',
  status: 'running',
  queuePosition: null,
  directionPreview: '预览',
  error: null,
  ...over,
});

const renderPanel = () =>
  render(
    <DecomposePanel
      request={{ key: 1, direction: '动量与波动率方向', market: 'a_share', universe: 'csi300' }}
      onClose={vi.fn()}
      onOpenDashboard={vi.fn()}
    />,
  );

beforeEach(() => {
  vi.clearAllMocks();
});

describe('composeDirection', () => {
  it('多行拼接且省略空字段（不产生空冒号行）', () => {
    expect(composeDirection(CARD_A)).toBe(
      '短期反转\n假设：5 日动量反转在多空两端均有超额\n依据：浮盈兑现压力\n验证建议：看 rankIC 与分组单调性',
    );
    expect(composeDirection(CARD_B)).toBe('量价背离\n假设：放量滞涨的股票未来收益更差');
  });

  it('标题/假设被编辑为空 → 结果为空串（派发侧据此拦截）', () => {
    expect(composeDirection({ title: '  ', hypothesis: '', categories: [] })).toBe('');
  });
});

describe('DecomposePanel', () => {
  it('拆解成功进入预览：卡片可编辑、全选计数与派发数一致', async () => {
    apiMocks.decomposeDirection.mockResolvedValue(okDecompose([CARD_A, CARD_B]));
    renderPanel();

    expect(await screen.findByDisplayValue('短期反转')).toBeTruthy();
    expect(screen.getByDisplayValue('量价背离')).toBeTruthy();
    expect(screen.getByText('全选（已选 2/2）')).toBeTruthy();
    expect(screen.getByRole('button', { name: '派发 2 个挖掘任务' })).toBeTruthy();
    // 请求参数保真：方向/市场/池原样下发
    expect(apiMocks.decomposeDirection).toHaveBeenCalledWith({
      direction: '动量与波动率方向',
      market: 'a_share',
      universe: 'csi300',
    });
  });

  it('取消勾选的卡片不进派发列表；派发成功后回执任务进注册表（含排队位次）', async () => {
    apiMocks.decomposeDirection.mockResolvedValue(okDecompose([CARD_A, CARD_B]));
    apiMocks.dispatchMiningBatch.mockResolvedValue({
      success: true,
      data: {
        items: [
          receipt({ index: 0, taskId: 't-run', status: 'running' }),
          receipt({ index: 1, taskId: 't-q', status: 'queued', queuePosition: 4 }),
        ],
        started: 1,
        queued: 1,
        failed: 0,
      },
    });
    renderPanel();
    await screen.findByDisplayValue('短期反转');

    // checkboxes: [0]=全选, [1]=CARD_A, [2]=CARD_B —— 取消第二张卡
    const boxes = screen.getAllByRole('checkbox');
    fireEvent.click(boxes[2]);
    expect(screen.getByText('全选（已选 1/2）')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '派发 1 个挖掘任务' }));
    await waitFor(() => expect(ctxMocks.adoptDispatchedTasks).toHaveBeenCalledTimes(1));

    const [directions] = apiMocks.dispatchMiningBatch.mock.calls[0];
    expect(directions.directions).toHaveLength(1);
    expect(directions.directions[0]).toContain('假设：5 日动量反转在多空两端均有超额');
    expect(directions.loopN).toBe(3);
    expect(directions.market).toBe('a_share');

    const adopted = ctxMocks.adoptDispatchedTasks.mock.calls[0][0];
    expect(adopted.map((t: any) => t.taskId)).toEqual(['t-run', 't-q']);
    expect(adopted[0].status).toBe('running');
    expect(adopted[1].status).toBe('queued');
    expect(adopted[1].queuePosition).toBe(4);
    expect(adopted[0].config.userInput).toContain('假设：');

    // 完成态摘要：已派发总数 + 排队数
    expect(await screen.findByText(/已派发 2 个任务/)).toBeTruthy();
    expect(screen.getByText(/1 个排队中/)).toBeTruthy();
  });

  it('失败条目跳过不进注册表，错误原文在完成态列出', async () => {
    apiMocks.decomposeDirection.mockResolvedValue(okDecompose([CARD_A]));
    apiMocks.dispatchMiningBatch.mockResolvedValue({
      success: true,
      data: {
        items: [
          receipt({
            index: 0,
            taskId: null,
            status: 'failed',
            directionPreview: '短期反转',
            error: '排队已满（您已排队 20/20）',
          }),
        ],
        started: 0,
        queued: 0,
        failed: 1,
      },
    });
    renderPanel();
    await screen.findByDisplayValue('短期反转');

    fireEvent.click(screen.getByRole('button', { name: '派发 1 个挖掘任务' }));
    expect(await screen.findByText(/1 个失败/)).toBeTruthy();
    expect(screen.getByText(/排队已满/)).toBeTruthy();
    expect(ctxMocks.adoptDispatchedTasks).not.toHaveBeenCalled();
  });

  it('拆解失败展示后端 detail，重试重新发起拆解', async () => {
    apiMocks.decomposeDirection.mockRejectedValueOnce({
      response: { data: { detail: '未配置 LLM API Key：可在个人中心填写' } },
    });
    apiMocks.decomposeDirection.mockResolvedValueOnce(okDecompose([CARD_A]));
    renderPanel();

    expect(await screen.findByText(/未配置 LLM API Key/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /重新拆解/ }));

    expect(await screen.findByDisplayValue('短期反转')).toBeTruthy();
    expect(apiMocks.decomposeDirection).toHaveBeenCalledTimes(2);
  });
});

describe('normalizeAgentTask（queued 一等状态）', () => {
  it('queued 不被折成 running，位次与中文文案齐备', () => {
    const t = normalizeAgentTask({
      task_id: 'q1',
      status: 'queued',
      queue_position: 3,
      direction: '方向A',
    });
    expect(t.status).toBe('queued');
    expect(t.queuePosition).toBe(3);
    expect(t.progress.message).toBe('排队中（第 3 位）');
    expect(t.config.userInput).toBe('方向A');
  });
});
