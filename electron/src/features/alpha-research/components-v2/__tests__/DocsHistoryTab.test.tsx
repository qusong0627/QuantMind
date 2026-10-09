/**
 * 挖掘历史 · 文档解析 Tab —— 文档链的回看与管理面。
 *
 * 钉死的边：
 * - 状态按后端字面量渲染，认不出的**原样显示**（不塞进近似的桶里骗人）；
 * - 「看文本 / 继续挖掘」只对 parsed/organized 开放——点了只会报错的按钮
 *   不如禁用；
 * - 删除是二次确认 + 连解析文件清除，取消时一个请求都不发；
 * - 「继续挖掘」把整行交给 onResume（AppRoot 管跳转），本组件不自己发任务。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { DocsHistoryTab, DOCS_PAGE_SIZE } from '../DocsHistoryTab';
import type { DocRow } from '../../services-v2/docMiningApi';

const { listDocsMock, getDocQuotaMock, getDocFileTextMock, deleteDocMock } = vi.hoisted(
  () => ({
    listDocsMock: vi.fn(),
    getDocQuotaMock: vi.fn(),
    getDocFileTextMock: vi.fn(),
    deleteDocMock: vi.fn(),
  }),
);

vi.mock('../../services-v2/docMiningApi', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../../services-v2/docMiningApi')>();
  return {
    ...actual,
    listDocs: listDocsMock,
    getDocQuota: getDocQuotaMock,
    getDocFileText: getDocFileTextMock,
    deleteDoc: deleteDocMock,
  };
});

const QUOTA = {
  day: '20261009',
  user_id: 'u-1',
  user_used: 30,
  user_limit: 200,
  platform_used: 100,
  platform_budget: 10000,
  user_remaining: 170,
  platform_remaining: 9900,
  exhausted: false,
  warning: false,
  token_configured: true,
};

function mkRow(over: Partial<DocRow> = {}): DocRow {
  return {
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
    ...over,
  };
}

beforeEach(() => {
  listDocsMock.mockReset();
  getDocQuotaMock.mockReset();
  getDocFileTextMock.mockReset();
  deleteDocMock.mockReset();
  listDocsMock.mockResolvedValue({
    items: [mkRow()],
    total: 1,
    limit: DOCS_PAGE_SIZE,
    offset: 0,
  });
  getDocQuotaMock.mockResolvedValue(QUOTA);
  getDocFileTextMock.mockResolvedValue('# 解析全文');
});

describe('列表渲染', () => {
  test('文件名/状态/页数/时间/已挖掘徽标', async () => {
    listDocsMock.mockResolvedValue({
      items: [
        mkRow({ status: 'organized', task_id: 't-abc12345' }),
        mkRow({ doc_id: 'd-2', filename: 'broken.pdf', status: 'parse_failed', error: '页数超限' }),
      ],
      total: 2,
      limit: DOCS_PAGE_SIZE,
      offset: 0,
    });

    render(<DocsHistoryTab />);

    expect(await screen.findByText('paper.pdf')).toBeTruthy();
    expect(screen.getByText('已整理')).toBeTruthy();
    expect(screen.getByText('已挖掘')).toBeTruthy();
    expect(screen.getByText('解析失败')).toBeTruthy();
    expect(screen.getByText('页数超限')).toBeTruthy();
    expect(screen.getByText(/共 2 份/)).toBeTruthy();
  });

  test('认不出的状态原样显示（不塞进近似的桶）', async () => {
    listDocsMock.mockResolvedValue({
      items: [mkRow({ status: 'future_state' })],
      total: 1,
      limit: DOCS_PAGE_SIZE,
      offset: 0,
    });

    render(<DocsHistoryTab />);

    expect(await screen.findByText('future_state')).toBeTruthy();
  });

  test('空列表给空态说明', async () => {
    listDocsMock.mockResolvedValue({ items: [], total: 0, limit: DOCS_PAGE_SIZE, offset: 0 });
    render(<DocsHistoryTab />);
    expect(await screen.findByText('还没有上传过文档')).toBeTruthy();
  });

  test('加载失败给错误与重试；重试真的再拉', async () => {
    listDocsMock.mockRejectedValueOnce(new Error('boom'));
    render(<DocsHistoryTab />);

    expect(await screen.findByText(/加载失败：boom/)).toBeTruthy();
    listDocsMock.mockResolvedValue({
      items: [mkRow()],
      total: 1,
      limit: DOCS_PAGE_SIZE,
      offset: 0,
    });
    fireEvent.click(screen.getByText('重试'));
    expect(await screen.findByText('paper.pdf')).toBeTruthy();
  });

  test('MinerU Token 未配置：配额条换成配置提示', async () => {
    getDocQuotaMock.mockResolvedValue({ ...QUOTA, token_configured: false });
    render(<DocsHistoryTab />);
    expect(await screen.findByText(/解析服务未配置/)).toBeTruthy();
  });
});

describe('看文本 / 继续挖掘：只对解析完成的开放', () => {
  test('解析中的行两个按钮都禁用', async () => {
    listDocsMock.mockResolvedValue({
      items: [mkRow({ status: 'parsing' })],
      total: 1,
      limit: DOCS_PAGE_SIZE,
      offset: 0,
    });
    render(<DocsHistoryTab onResume={vi.fn()} />);
    await screen.findByText('paper.pdf');

    expect((screen.getByText('看文本').closest('button') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByText('继续挖掘').closest('button') as HTMLButtonElement).disabled).toBe(true);
  });

  test('看文本拉全文并缓存；收起再展开不重复拉', async () => {
    render(<DocsHistoryTab />);
    await screen.findByText('paper.pdf');

    fireEvent.click(screen.getByText('看文本'));
    expect(await screen.findByText('# 解析全文')).toBeTruthy();
    expect(getDocFileTextMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('收起'));
    fireEvent.click(screen.getByText('看文本'));
    expect(await screen.findByText('# 解析全文')).toBeTruthy();
    expect(getDocFileTextMock).toHaveBeenCalledTimes(1);
  });

  test('读原文失败：行内显示失败原因，不炸整页', async () => {
    getDocFileTextMock.mockRejectedValue(new Error('404'));
    render(<DocsHistoryTab />);
    await screen.findByText('paper.pdf');

    fireEvent.click(screen.getByText('看文本'));
    expect(await screen.findByText(/原文读取失败：404/)).toBeTruthy();
  });

  test('继续挖掘把整行原样交给 onResume', async () => {
    const onResume = vi.fn();
    const row = mkRow({ status: 'organized', task_id: 't-abc12345' });
    listDocsMock.mockResolvedValue({ items: [row], total: 1, limit: DOCS_PAGE_SIZE, offset: 0 });

    render(<DocsHistoryTab onResume={onResume} />);
    await screen.findByText('paper.pdf');

    fireEvent.click(screen.getByText('继续挖掘'));
    expect(onResume).toHaveBeenCalledWith(expect.objectContaining({ doc_id: 'd-1' }));
  });
});

describe('删除：二次确认 + 连解析文件清除', () => {
  test('确认后 deleteDoc 并重拉列表', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<DocsHistoryTab />);
    await screen.findByText('paper.pdf');
    const before = listDocsMock.mock.calls.length;

    fireEvent.click(screen.getByText('删除'));

    await waitFor(() => expect(deleteDocMock).toHaveBeenCalledWith('d-1'));
    await waitFor(() => expect(listDocsMock.mock.calls.length).toBeGreaterThan(before));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('不可恢复'));
    confirmSpy.mockRestore();
  });

  test('取消时一个请求都不发', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<DocsHistoryTab />);
    await screen.findByText('paper.pdf');

    fireEvent.click(screen.getByText('删除'));

    expect(deleteDocMock).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  test('删除失败显示原因', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    deleteDocMock.mockRejectedValue({ response: { data: { detail: '文档不存在' } } });
    render(<DocsHistoryTab />);
    await screen.findByText('paper.pdf');

    fireEvent.click(screen.getByText('删除'));

    expect(await screen.findByText(/删除失败：文档不存在/)).toBeTruthy();
    confirmSpy.mockRestore();
  });
});

describe('分页与外部刷新', () => {
  test('下一页按 DOCS_PAGE_SIZE 翻页，参数如实下发', async () => {
    listDocsMock.mockResolvedValue({
      items: [mkRow()],
      total: 45,
      limit: DOCS_PAGE_SIZE,
      offset: 0,
    });
    render(<DocsHistoryTab />);
    await screen.findByText('paper.pdf');

    expect((screen.getByText('上一页') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByText('下一页'));

    await waitFor(() =>
      expect(listDocsMock).toHaveBeenCalledWith(
        expect.objectContaining({ offset: DOCS_PAGE_SIZE }),
      ),
    );
  });

  test('refreshSeq 代次递增触发重拉（HistoryPage 头部「刷新」）', async () => {
    const { rerender } = render(<DocsHistoryTab refreshSeq={0} />);
    await screen.findByText('paper.pdf');
    const before = listDocsMock.mock.calls.length;

    rerender(<DocsHistoryTab refreshSeq={1} />);

    await waitFor(() =>
      expect(listDocsMock.mock.calls.length).toBeGreaterThan(before),
    );
  });

  test('表格只在任务无关的 5 列里展示（文件名列含错误行内提示）', async () => {
    render(<DocsHistoryTab />);
    await screen.findByText('paper.pdf');
    const table = within(screen.getByRole('table'));
    for (const h of ['文件名', '状态', '页数', '上传时间', '操作']) {
      expect(table.getByText(h)).toBeTruthy();
    }
  });
});
