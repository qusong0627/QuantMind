/**
 * 挖掘历史页 —— 用户的原话是「不然每次挖了什么都没有显示」。
 *
 * 这张表现在是「挖过什么」的唯一入口，所以用例围绕它最容易骗人的几条边：
 *
 * - **显示的是后端落库的事实**：方向/来源/因子数全部来自 rd_agent_mining_tasks
 *   行，legacy 迁移行方向为空就是「—」，绝不回落成默认文案（那会让用户以为
 *   当时挖的是默认方向）。
 * - **失败行必须带原因**：error 是后端给的原文（比如重启对账写进去的
 *   "Server restarted while task was running"），表格里不能只剩一个红点。
 * - **操作收口**：查看结果/重跑都把整行原样交给回调（AppRoot 负责跳转），
 *   且「查看结果」在因子数为 0 时禁用——点进去只会得到一张空表。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { HistoryPage } from '../HistoryPage';
import type { MiningHistoryRow } from '../../services-v2/api';

const { getMiningHistoryMock } = vi.hoisted(() => ({
  getMiningHistoryMock: vi.fn(),
}));

vi.mock('../../services-v2/api', () => ({
  getMiningHistory: getMiningHistoryMock,
  // 常量是模块契约的一部分，mock 必须镜像真实导出（组件分页口径靠它）
  MINING_HISTORY_PAGE_SIZE: 50,
}));

const ok = (data: any) => ({ success: true, data });

function mkRow(over: Partial<MiningHistoryRow> = {}): MiningHistoryRow {
  return {
    task_id: 'a1b2c3d4e5f60718',
    user_id: '10000001',
    market: 'a_share',
    universe: 'csi300',
    data_source: 'parquet',
    direction: '动量反转 × 波动率过滤',
    direction_mode: null,
    direction_meta: null,
    source: 'text',
    doc_id: null,
    status: 'completed',
    progress_pct: 100,
    current_loop: 3,
    loop_n: 3,
    error: null,
    factor_count: 12,
    created_at: '2026-10-08T01:02:03Z',
    updated_at: '2026-10-08T02:00:00Z',
    completed_at: '2026-10-08T02:00:00Z',
    ...over,
  };
}

beforeEach(() => {
  getMiningHistoryMock.mockReset();
  getMiningHistoryMock.mockResolvedValue(ok({ tasks: [mkRow()], total: 1 }));
});

describe('HistoryPage：每一行都是「当时挖了什么」的事实', () => {
  test('渲染方向/来源/市场池/状态/因子数/时间', async () => {
    render(<HistoryPage />);

    expect(await screen.findByText('动量反转 × 波动率过滤')).toBeTruthy();
    // 行内事实收在表格里查：状态筛选下拉里也有同名的「已完成」option
    const table = within(screen.getByRole('table'));
    expect(table.getByText('文字')).toBeTruthy();
    expect(table.getByText('A股 · csi300')).toBeTruthy();
    expect(table.getByText('已完成')).toBeTruthy();
    expect(table.getByText('12')).toBeTruthy();
    expect(getMiningHistoryMock).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 50, offset: 0 }),
    );
  });

  test('legacy 迁移行方向为空显示「—」，不编造默认方向', async () => {
    getMiningHistoryMock.mockResolvedValue(
      ok({ tasks: [mkRow({ source: 'legacy', direction: '', factor_count: 8 })], total: 1 }),
    );

    render(<HistoryPage />);

    expect(await screen.findByText('历史迁移')).toBeTruthy();
    expect(screen.getByText('—')).toBeTruthy();
  });

  test('失败行必须显示后端给的原因（不能只剩一个红状态）', async () => {
    getMiningHistoryMock.mockResolvedValue(
      ok({
        tasks: [
          mkRow({
            status: 'failed',
            error: 'Server restarted while task was running',
            factor_count: 0,
          }),
        ],
        total: 1,
      }),
    );

    render(<HistoryPage />);

    expect(await screen.findByText('失败')).toBeTruthy();
    expect(screen.getByText(/Server restarted while task was running/)).toBeTruthy();
  });

  test('文档来源显示文档标识', async () => {
    getMiningHistoryMock.mockResolvedValue(
      ok({ tasks: [mkRow({ source: 'doc', doc_id: 'd0c1234567890abc' })], total: 1 }),
    );

    render(<HistoryPage />);

    expect(await screen.findByText(/文档 d0c12345/)).toBeTruthy();
  });

  test('方向模式徽章：类别选定/随机抽取；未知值原样呈现（不静默吞掉）', async () => {
    getMiningHistoryMock.mockResolvedValue(
      ok({
        tasks: [
          mkRow({ task_id: 't-sel', direction_mode: 'selected' }),
          mkRow({ task_id: 't-rnd', direction_mode: 'random' }),
          mkRow({ task_id: 't-fut', direction_mode: 'cards' }),
        ],
        total: 3,
      }),
    );

    render(<HistoryPage />);

    const table = within(await screen.findByRole('table'));
    expect(await table.findByText('类别选定')).toBeTruthy();
    expect(table.getByText('随机抽取')).toBeTruthy();
    expect(table.getByText('cards')).toBeTruthy();
  });

  test('模式未参与（null：自由文本/卡片派发/legacy）不渲染模式徽章', async () => {
    render(<HistoryPage />);

    expect(await screen.findByText('动量反转 × 波动率过滤')).toBeTruthy();
    expect(screen.queryByText('类别选定')).toBeNull();
    expect(screen.queryByText('随机抽取')).toBeNull();
  });

  test('抽样证据（T-MV-03）压进徽章工具提示：加权口径/seed/候选次数可见', async () => {
    getMiningHistoryMock.mockResolvedValue(
      ok({
        tasks: [
          mkRow({
            direction_mode: 'random',
            direction_meta: JSON.stringify({
              mode: 'random',
              weighting: 'blankness',
              seed: 987654321,
              picked: '动量反转 × 波动率过滤',
              candidates: [
                { direction: '动量反转 × 波动率过滤', attempts: 0, weight: 1 },
                { direction: '波动类方向', attempts: 9, weight: 0.1 },
              ],
            }),
          }),
        ],
        total: 1,
      }),
    );

    render(<HistoryPage />);

    const badge = await screen.findByTitle(/方向如何被选中/);
    expect(badge.getAttribute('title')).toContain('按空白度加权');
    expect(badge.getAttribute('title')).toContain('seed=987654321');
    expect(badge.getAttribute('title')).toContain('波动类方向=9次');
  });

  test('抽样证据是坏 JSON 时退回基础提示（宁缺勿错，不炸行）', async () => {
    getMiningHistoryMock.mockResolvedValue(
      ok({
        tasks: [mkRow({ direction_mode: 'random', direction_meta: '{not json' })],
        total: 1,
      }),
    );

    render(<HistoryPage />);

    const badge = await screen.findByTitle('方向如何被选中（类别选择路径）');
    expect(badge.textContent).toBe('随机抽取');
  });

  test('没有记录时给出空态说明，而不是空白表格', async () => {
    getMiningHistoryMock.mockResolvedValue(ok({ tasks: [], total: 0 }));

    render(<HistoryPage />);

    expect(await screen.findByText(/还没有挖掘记录/)).toBeTruthy();
  });

  test('加载失败给出错误与重试；重试真的再拉一次', async () => {
    getMiningHistoryMock.mockRejectedValueOnce(new Error('boom'));

    render(<HistoryPage />);

    expect(await screen.findByText(/加载失败/)).toBeTruthy();
    getMiningHistoryMock.mockResolvedValue(ok({ tasks: [mkRow()], total: 1 }));
    fireEvent.click(screen.getByText('重试'));

    expect(await screen.findByText('动量反转 × 波动率过滤')).toBeTruthy();
  });
});

describe('HistoryPage：过滤、分页与操作', () => {
  test('状态筛选进查询参数，并把分页拨回第一页', async () => {
    getMiningHistoryMock.mockResolvedValue(ok({ tasks: [mkRow()], total: 120 }));
    render(<HistoryPage />);
    await screen.findByText('动量反转 × 波动率过滤');

    // 先翻到第二页
    fireEvent.click(screen.getByText('下一页'));
    await waitFor(() =>
      expect(getMiningHistoryMock).toHaveBeenCalledWith(
        expect.objectContaining({ offset: 50 }),
      ),
    );

    fireEvent.change(screen.getByLabelText('状态筛选'), {
      target: { value: 'failed' },
    });
    await waitFor(() =>
      expect(getMiningHistoryMock).toHaveBeenCalledWith(
        expect.objectContaining({ status: 'failed', offset: 0 }),
      ),
    );
  });

  test('上一页在首页禁用；共 N 条如实展示', async () => {
    getMiningHistoryMock.mockResolvedValue(ok({ tasks: [mkRow()], total: 1 }));
    render(<HistoryPage />);
    await screen.findByText('动量反转 × 波动率过滤');

    expect(screen.getByText(/共 1 条/)).toBeTruthy();
    expect((screen.getByText('上一页') as HTMLButtonElement).disabled).toBe(true);
  });

  test('查看结果把整行交给回调；因子数为 0 时禁用', async () => {
    const onViewResults = vi.fn();
    getMiningHistoryMock.mockResolvedValue(
      ok({
        tasks: [
          mkRow(),
          // 第二行方向与首行区分：本用例只关心「因子数为 0 → 查看结果禁用」
          mkRow({ task_id: 'ffffffff00000000', factor_count: 0, direction: '零因子方向' }),
        ],
        total: 2,
      }),
    );

    render(<HistoryPage onViewResults={onViewResults} />);
    await screen.findByText('动量反转 × 波动率过滤');

    const buttons = screen.getAllByText('查看结果') as HTMLButtonElement[];
    expect(buttons[1].disabled).toBe(true);
    fireEvent.click(buttons[0]);
    expect(onViewResults).toHaveBeenCalledWith(
      expect.objectContaining({ task_id: 'a1b2c3d4e5f60718' }),
    );
  });

  test('重跑把整行交给回调（含 legacy 无方向行）', async () => {
    const onRetry = vi.fn();
    getMiningHistoryMock.mockResolvedValue(
      ok({ tasks: [mkRow({ source: 'legacy', direction: '' })], total: 1 }),
    );

    render(<HistoryPage onRetry={onRetry} />);
    await screen.findByText('历史迁移');

    fireEvent.click(screen.getByText('重跑'));
    expect(onRetry).toHaveBeenCalledWith(
      expect.objectContaining({ task_id: 'a1b2c3d4e5f60718' }),
    );
  });
});
