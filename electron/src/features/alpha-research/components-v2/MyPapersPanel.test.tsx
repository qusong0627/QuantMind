import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  listPersonalPapers: vi.fn(), downloadPersonalPaper: vi.fn(), migratePersonalPaper: vi.fn(), deletePersonalPaper: vi.fn(),
}));
vi.mock('../services-v2/papersApi', () => api);
vi.mock('../../../store', () => ({ useAppSelector: (selector: (state: unknown) => unknown) => selector({ auth: { isAuthenticated: true, isLoading: false } }) }));
vi.mock('../../../store/sessionBoundary', () => ({
  getSessionGeneration: () => 1, registerSessionReset: () => () => {},
}));

import { MyPapersPanel } from './MyPapersPanel';

describe('paper mining entry', () => {
  it('imports the selected paper into research documents and refreshes the list', async () => {
    api.listPersonalPapers.mockResolvedValueOnce([{ paper_id: 'p1', filename: 'paper.pdf', size_bytes: 8, created_at: '2026-01-01', sha256: 'abc' }]).mockResolvedValue([]);
    api.migratePersonalPaper.mockResolvedValue({ doc_id: 'd1' });
    const onImported = vi.fn();
    render(<MyPapersPanel onImported={onImported} />);
    fireEvent.click(await screen.findByRole('button', { name: '迁移到文档' }));
    await waitFor(() => expect(onImported).toHaveBeenCalledOnce());
    expect(api.migratePersonalPaper).toHaveBeenCalledWith('p1', expect.any(AbortSignal));
    expect(await screen.findByRole('status')).toHaveTextContent('开源版暂不支持论文解析');
  });

  it('confirms deletion in the application before removing the paper', async () => {
    api.listPersonalPapers.mockReset().mockResolvedValueOnce([{ paper_id: 'p2', filename: 'old.pdf', size_bytes: 8, created_at: '2026-01-01', sha256: 'abc' }]).mockResolvedValue([]);
    api.deletePersonalPaper.mockReset().mockResolvedValue(undefined);
    render(<MyPapersPanel />);
    fireEvent.click(await screen.findByRole('button', { name: '删除' }));
    const dialog = await screen.findByRole('dialog', { name: '删除旧论文' });
    expect(dialog).toHaveTextContent('old.pdf');
    expect(api.deletePersonalPaper).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '取 消' }));
    expect(api.deletePersonalPaper).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    const confirmButton = (await screen.findByRole('dialog', { name: '删除旧论文' })).querySelector('button.ant-btn-dangerous');
    expect(confirmButton).not.toBeNull();
    fireEvent.click(confirmButton!);
    await waitFor(() => expect(api.deletePersonalPaper).toHaveBeenCalledOnce());
  });
});
