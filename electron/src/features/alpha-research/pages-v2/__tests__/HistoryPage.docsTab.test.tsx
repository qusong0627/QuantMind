/**
 * 挖掘历史页的「文档解析」Tab —— 构建期开关决定整排 Tab 的存在与否。
 *
 * 钉死的边：
 * - 开关关（生产默认）：**整排 Tab 不渲染**，页面与从前一字不差——
 *   文档链没开的部署里，挖掘任务列表照常工作；
 * - 开关开：两个 Tab 切换；文档 Tab 上市场/状态筛选让位（它们只过滤任务表），
 *   头部「刷新」改拉文档列表；
 * - 文档 Tab 的数据来自 docMiningApi（/alpha-agent/docs），与任务表两条数据源
 *   互不串扰。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { HistoryPage } from '../HistoryPage';
import type { MiningHistoryRow } from '../../services-v2/api';
import type { DocRow } from '../../services-v2/docMiningApi';

const { flagState } = vi.hoisted(() => ({ flagState: { enabled: false } }));

vi.mock('../../../../config/docMiningFlags', () => ({
  isDocMiningEnabled: () => flagState.enabled,
  ENABLE_DOC_MINING: false,
  DOC_MINING_DISABLED_DETAIL: 'doc_mining_disabled',
}));

const { getMiningHistoryMock, listDocsMock, getDocQuotaMock } = vi.hoisted(() => ({
  getMiningHistoryMock: vi.fn(),
  listDocsMock: vi.fn(),
  getDocQuotaMock: vi.fn(),
}));

vi.mock('../../services-v2/api', () => ({
  getMiningHistory: getMiningHistoryMock,
  MINING_HISTORY_PAGE_SIZE: 50,
}));

vi.mock('../../services-v2/docMiningApi', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../../services-v2/docMiningApi')>();
  return {
    ...actual,
    listDocs: listDocsMock,
    getDocQuota: getDocQuotaMock,
  };
});

const ok = (data: unknown) => ({ success: true, data });

const TASK_ROW: MiningHistoryRow = {
  task_id: 'a1b2c3d4e5f60718',
  user_id: '10000001',
  market: 'a_share',
  universe: 'csi300',
  data_source: 'parquet',
  direction: '动量反转 × 波动率过滤',
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
};

const DOC_ROW: DocRow = {
  doc_id: 'd-1',
  filename: 'paper.pdf',
  ext: '.pdf',
  size_bytes: 1000,
  parse_state: null,
  page_count: 12,
  status: 'parsed',
  organize_kind: null,
  organize_prompt_version: null,
  organized_at: null,
  task_id: null,
  error: null,
  created_at: '2026-10-09T01:02:03Z',
  updated_at: '2026-10-09T01:02:03Z',
};

beforeEach(() => {
  flagState.enabled = false;
  getMiningHistoryMock.mockReset();
  listDocsMock.mockReset();
  getDocQuotaMock.mockReset();
  getMiningHistoryMock.mockResolvedValue(ok({ tasks: [TASK_ROW], total: 1 }));
  listDocsMock.mockResolvedValue({ items: [DOC_ROW], total: 1, limit: 20, offset: 0 });
  getDocQuotaMock.mockResolvedValue({
    day: '20261009',
    user_id: 'u-1',
    user_used: 0,
    user_limit: 200,
    platform_used: 0,
    platform_budget: 10000,
    user_remaining: 200,
    platform_remaining: 10000,
    exhausted: false,
    warning: false,
    token_configured: true,
  });
});

describe('构建期开关关闭：文档链完全不存在', () => {
  test('没有 Tab 排、没有「文档解析」，任务列表照常', async () => {
    render(<HistoryPage onResumeDoc={vi.fn()} />);

    expect(await screen.findByText('动量反转 × 波动率过滤')).toBeTruthy();
    expect(screen.queryByRole('tablist')).toBeNull();
    expect(screen.queryByText('文档解析')).toBeNull();
    expect(screen.getByLabelText('状态筛选')).toBeTruthy();
    // 文档链的 API 一次都不该被碰到
    expect(listDocsMock).not.toHaveBeenCalled();
    expect(getDocQuotaMock).not.toHaveBeenCalled();
  });
});

describe('构建期开关打开：两个 Tab 切换', () => {
  test('默认任务 Tab：筛选可用、文档 API 未触碰', async () => {
    flagState.enabled = true;
    render(<HistoryPage onResumeDoc={vi.fn()} />);

    expect(await screen.findByText('动量反转 × 波动率过滤')).toBeTruthy();
    expect(screen.getByRole('tab', { name: /挖掘任务/ })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /文档解析/ })).toBeTruthy();
    expect(screen.getByLabelText('状态筛选')).toBeTruthy();
    expect(listDocsMock).not.toHaveBeenCalled();
  });

  test('切到文档 Tab：拉文档列表、筛选让位；头部刷新改拉文档', async () => {
    flagState.enabled = true;
    render(<HistoryPage onResumeDoc={vi.fn()} />);
    await screen.findByText('动量反转 × 波动率过滤');

    fireEvent.click(screen.getByRole('tab', { name: /文档解析/ }));

    expect(await screen.findByText('paper.pdf')).toBeTruthy();
    // 任务表的行与筛选让位（两套数据互不串扰）
    expect(screen.queryByText('动量反转 × 波动率过滤')).toBeNull();
    expect(screen.queryByLabelText('状态筛选')).toBeNull();

    const before = listDocsMock.mock.calls.length;
    fireEvent.click(screen.getByTitle('刷新'));
    await waitFor(() => expect(listDocsMock.mock.calls.length).toBeGreaterThan(before));
  });

  test('切回任务 Tab：任务表回来、文档列表让位', async () => {
    flagState.enabled = true;
    render(<HistoryPage onResumeDoc={vi.fn()} />);
    await screen.findByText('动量反转 × 波动率过滤');

    fireEvent.click(screen.getByRole('tab', { name: /文档解析/ }));
    await screen.findByText('paper.pdf');

    fireEvent.click(screen.getByRole('tab', { name: /挖掘任务/ }));

    expect(await screen.findByText('动量反转 × 波动率过滤')).toBeTruthy();
    expect(screen.queryByText('paper.pdf')).toBeNull();
    expect(screen.getByLabelText('状态筛选')).toBeTruthy();
  });

  test('文档 Tab 上「继续挖掘」经 onResumeDoc 上抛（AppRoot 管跳转）', async () => {
    flagState.enabled = true;
    const onResumeDoc = vi.fn();
    render(<HistoryPage onResumeDoc={onResumeDoc} />);
    await screen.findByText('动量反转 × 波动率过滤');

    fireEvent.click(screen.getByRole('tab', { name: /文档解析/ }));
    await screen.findByText('paper.pdf');
    fireEvent.click(screen.getByText('继续挖掘'));

    expect(onResumeDoc).toHaveBeenCalledWith(
      expect.objectContaining({ doc_id: 'd-1' }),
    );
  });
});
