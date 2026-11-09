import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ listResearchDocuments: vi.fn(), deleteResearchDocument: vi.fn() }));
vi.mock('../services-v2/docApi', () => api);
vi.mock('../services-v2/factorJobsApi', () => ({}));
vi.mock('../../../store', () => ({ useAppSelector: (selector: (state: unknown) => unknown) => selector({ auth: { isAuthenticated: true, isLoading: false } }) }));
vi.mock('../../../store/sessionBoundary', () => ({ getSessionGeneration: () => 1, registerSessionReset: () => () => {} }));

import { DocumentCenterPage } from './DocumentCenterPage';

describe('research document deletion', () => {
  it('hides deleted rows and removes a document after confirmation', async () => {
    const row = { doc_id: 'd1', filename: 'active.pdf', size_bytes: 8, sha256: 'a'.repeat(64), status: 'uploaded', created_at: '2026-01-01' };
    const deleted = { ...row, doc_id: 'd2', filename: 'deleted.pdf', status: 'deleted' };
    api.listResearchDocuments.mockResolvedValueOnce({ items: [row, deleted], has_more: false }).mockResolvedValue({ items: [{ ...row, status: 'deleted' }, deleted], has_more: false });
    api.deleteResearchDocument.mockResolvedValue(undefined);
    render(<DocumentCenterPage hideHeader />);
    expect(await screen.findByText('active.pdf')).toBeInTheDocument();
    expect(screen.getByTitle('开源版暂不支持论文解析')).toBeDisabled();
    expect(screen.queryByText('deleted.pdf')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle('删除文档'));
    const dialog = await screen.findByRole('dialog', { name: '删除文档' });
    expect(dialog.parentElement).toHaveClass('ant-modal-centered');
    fireEvent.click(within(dialog).getByRole('button', { name: /删\s*除/ }));
    await waitFor(() => expect(screen.queryByText('active.pdf')).not.toBeInTheDocument());
    expect(api.deleteResearchDocument).toHaveBeenCalledWith('d1', expect.any(AbortSignal));
  });
});
