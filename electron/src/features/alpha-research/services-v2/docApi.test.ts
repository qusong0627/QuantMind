import { beforeEach, describe, expect, it, vi } from 'vitest';

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock('../../../services/aiStrategyClients', () => ({ apiClient: client }));

import {
  dispatchDocumentMiningBatch,
  deleteResearchDocument,
  decomposeResearchDirection,
  dispatchDocumentMining,
  downloadResearchDocument,
  getResearchDocumentMarkdown,
  listResearchDocuments,
  reconcileDocumentParse,
  saveOrganizedDraft,
  submitDocumentOrganization,
  submitDocumentParse,
  uploadResearchDocument,
} from './docApi';

describe('private research document API', () => {
  beforeEach(() => vi.clearAllMocks());

  it('lists a bounded page without accepting identity overrides', async () => {
    const page = { items: [], has_more: true, limit: 20, offset: 40 };
    client.get.mockResolvedValue({ data: { code: 200, data: page } });
    const signal = new AbortController().signal;

    await expect(listResearchDocuments({ limit: 20, offset: 40 }, signal)).resolves.toEqual(page);
    expect(client.get).toHaveBeenCalledWith('/alpha-agent/docs', {
      params: { limit: 20, offset: 40 },
      signal,
    });
  });

  it('uploads a PDF as multipart and supports request cancellation', async () => {
    const doc = { doc_id: 'doc-1', filename: 'research.pdf', status: 'uploaded' };
    client.post.mockResolvedValue({ data: { code: 200, data: doc } });
    const file = new File(['%PDF-1.7'], 'research.pdf', { type: 'application/pdf' });
    const signal = new AbortController().signal;

    await expect(uploadResearchDocument(file, signal)).resolves.toEqual(doc);
    const [url, body, options] = client.post.mock.calls[0];
    expect(url).toBe('/alpha-agent/docs');
    expect(body).toBeInstanceOf(FormData);
    expect((body as FormData).get('file')).toMatchObject({ name: 'research.pdf' });
    expect(options).toEqual({ signal });
  });

  it('uploads multiple PDFs in merge order using repeated file fields', async () => {
    const doc = { doc_id: 'doc-2', filename: 'body.pdf（共2个文件）', status: 'uploaded', files_count: 2 };
    client.post.mockResolvedValue({ data: { code: 200, data: doc } });
    const files = [
      new File(['%PDF-1.7 body'], 'body.pdf', { type: 'application/pdf' }),
      new File(['%PDF-1.7 appendix'], 'appendix.pdf', { type: 'application/pdf' }),
    ];

    await expect(uploadResearchDocument(files)).resolves.toEqual(doc);
    const form = client.post.mock.calls[0][1] as FormData;
    expect(form.getAll('file').map((file) => (file as File).name)).toEqual([
      'body.pdf',
      'appendix.pdf',
    ]);
  });

  it('encodes document IDs for private download and deletion', async () => {
    const blob = new Blob(['%PDF-1.7'], { type: 'application/pdf' });
    client.get.mockResolvedValue({ data: blob });
    client.delete.mockResolvedValue({ data: { code: 200 } });
    const signal = new AbortController().signal;

    await expect(downloadResearchDocument('doc/a', signal)).resolves.toBe(blob);
    expect(client.get).toHaveBeenCalledWith('/alpha-agent/docs/doc%2Fa/original', {
      responseType: 'blob',
      signal,
    });
    await deleteResearchDocument('doc/a', signal);
    expect(client.delete).toHaveBeenCalledWith('/alpha-agent/docs/doc%2Fa', { signal });
  });

  it('submits parsing with a fresh idempotency key and a cancellable request', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'request-1' });
    const job = { job_id: 'job-1', status: 'queued' as const };
    client.post.mockResolvedValue({ data: { data: job } });
    const signal = new AbortController().signal;

    await expect(submitDocumentParse('doc/a', signal)).resolves.toEqual(job);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/docs/doc%2Fa/parse',
      undefined,
      { headers: { 'Idempotency-Key': 'request-1' }, signal },
    );
    vi.unstubAllGlobals();
  });

  it('reconciles an uncertain parse using the same durable job', async () => {
    const job = { job_id: 'job-1', status: 'running' as const };
    client.post.mockResolvedValue({ data: { data: job } });
    const signal = new AbortController().signal;

    await expect(reconcileDocumentParse('doc/a', 'batch-123', signal)).resolves.toEqual(job);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/docs/doc%2Fa/parse/reconcile',
      { batch_id: 'batch-123' },
      { signal },
    );
  });

  it('submits private document organization with its mode and idempotency key', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'organize-request' });
    const job = { job_id: 'job-organize', status: 'queued' as const };
    client.post.mockResolvedValue({ data: { data: job } });
    const signal = new AbortController().signal;

    await expect(submitDocumentOrganization(
      'doc/a',
      { kind: 'paper', extra: '关注数据口径' },
      signal,
    )).resolves.toEqual(job);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/docs/doc%2Fa/organize',
      { kind: 'paper', extra: '关注数据口径' },
      { headers: { 'Idempotency-Key': 'organize-request' }, signal },
    );
    vi.unstubAllGlobals();
  });

  it('saves an edited organized draft through the owner document endpoint', async () => {
    client.put.mockResolvedValue({ data: { code: 200 } });
    const signal = new AbortController().signal;

    await saveOrganizedDraft('doc/a', '# 私人整理稿', signal);
    expect(client.put).toHaveBeenCalledWith(
      '/alpha-agent/docs/doc%2Fa/organized-text',
      { organized_text: '# 私人整理稿' },
      { signal },
    );
  });

  it('dispatches a reviewed document direction with an idempotency key', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'mining-request' });
    const task = { task_id: 'task-1', status: 'queued' };
    client.post.mockResolvedValue({ data: { data: task } });
    const signal = new AbortController().signal;

    await expect(dispatchDocumentMining('doc/a', '价量研究方向', signal)).resolves.toEqual(task);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/evolve',
      {
        algorithm: 'rdagent',
        market: 'a_share',
        universe: 'csi300',
        loop_n: 3,
        direction: '价量研究方向',
        doc_id: 'doc/a',
      },
      { headers: { 'Idempotency-Key': 'mining-request' }, signal },
    );
    vi.unstubAllGlobals();
  });

  it('decomposes a reviewed direction without posting client identity', async () => {
    const result = {
      prompt_version: 'decompose_v2',
      cards: [{ title: '量价背离', hypothesis: '量价背离后收益反转', categories: [] }],
      dropped: 0,
      max_cards: 6,
    };
    client.post.mockResolvedValue({ data: { data: result } });
    const signal = new AbortController().signal;

    await expect(decomposeResearchDirection('来自本人文档的研究方向', signal, ['factor-a'])).resolves.toEqual(result);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/directions/decompose',
      { direction: '来自本人文档的研究方向', market: 'a_share', universe: 'csi300', max_cards: 6, seed_factor_ids: ['factor-a'] },
      { signal },
    );
  });

  it('dispatches reviewed document directions as one retryable owner-scoped batch', async () => {
    const receipt = {
      batch_id: 'batch-123',
      items: [
        { index: 0, direction: '方向一', task_id: 'task-a', status: 'queued', queued: true, queue_position: 1, error: null },
        { index: 1, direction: '方向二', task_id: null, status: 'failed', queued: false, queue_position: null, error: '失败' },
      ],
      started: 0,
      queued: 1,
      failed: 1,
    };
    client.post.mockResolvedValue({ data: { data: receipt } });
    const signal = new AbortController().signal;

    await expect(dispatchDocumentMiningBatch('doc-a', ['方向一', '方向二'], 'batch-123', signal, ['factor-a', null])).resolves.toEqual(receipt);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/directions/dispatch',
      {
        batch_id: 'batch-123',
        doc_id: 'doc-a',
        directions: ['方向一', '方向二'],
        market: 'a_share',
        universe: 'csi300',
        loop_n: 3,
        seed_factor_ids: ['factor-a', null],
      },
      { signal },
    );
  });

  it('reads bounded parsed Markdown through the owner-checked endpoint', async () => {
    const preview = { doc_id: 'doc/a', content: '# Private', truncated: false, max_bytes: 524288 };
    client.get.mockResolvedValue({ data: { data: preview } });
    const signal = new AbortController().signal;

    await expect(getResearchDocumentMarkdown('doc/a', signal)).resolves.toEqual(preview);
    expect(client.get).toHaveBeenCalledWith('/alpha-agent/docs/doc%2Fa/markdown', { signal });
  });
});
