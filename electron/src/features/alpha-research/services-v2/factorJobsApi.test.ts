import { beforeEach, describe, expect, it, vi } from 'vitest';

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('../../../services/aiStrategyClients', () => ({ apiClient: client }));

import {
  cancelFactorJob,
  addFactorToPool,
  downloadFactorJobReport,
  evaluateFactorLifecycle,
  getFactorLifecycleHistory,
  getFactorJobHistory,
  getFactorPoolPanel,
  getFactorSourceRevisions,
  getFactorValidationHistory,
  listFactorPool,
  listFactorPoolEdges,
  rebuildFactorPoolEdges,
  rebuildFactorPoolScores,
  rebuildFactorPoolTaskRoundEdges,
  rebuildFactorPoolValueEdges,
  restoreFactorLifecycle,
  removeFactorFromPool,
  submitFactorBatch,
  submitFactorMaterialization,
  submitFactorJob,
} from './factorJobsApi';

describe('factor research jobs API', () => {
  beforeEach(() => vi.clearAllMocks());

  it('submits validation as an owner-scoped job request without identity overrides', async () => {
    const job = { job_id: 'job-1', kind: 'validate', status: 'queued' };
    client.post.mockResolvedValue({ data: { code: 202, data: job } });

    await expect(submitFactorJob({
      factor_id: 'factor-1',
      kind: 'validate',
      universe: 'csi500',
      start_date: '2023-01-01',
      end_date: '2023-12-31',
      data_source: 'qlib_bin',
    })).resolves.toEqual(job);

    expect(client.post).toHaveBeenCalledWith('/alpha-agent/factor-jobs', {
      factor_id: 'factor-1',
      kind: 'validate',
      universe: 'csi500',
      start_date: '2023-01-01',
      end_date: '2023-12-31',
      data_source: 'qlib_bin',
    }, { headers: { 'Idempotency-Key': expect.any(String) } });
  });

  it('allows session-bound cancellation of a private factor job', async () => {
    const controller = new AbortController();
    const job = { job_id: 'job-1', kind: 'validate', status: 'stopping' };
    client.post.mockResolvedValue({ data: { code: 200, data: job } });

    await expect(cancelFactorJob('job-1', controller.signal)).resolves.toEqual(job);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factor-jobs/job-1/cancel',
      undefined,
      { signal: controller.signal },
    );
  });

  it('previews and explicitly applies lifecycle evaluation for one encoded factor', async () => {
    const result = {
      factor_id: 'factor/a',
      state: 'active',
      blind_until: null,
      eval_status: 'ok',
      action: 'none',
      applied: false,
    };
    const controller = new AbortController();
    client.post.mockResolvedValue({ data: { code: 200, data: result } });

    await expect(evaluateFactorLifecycle('factor/a', false, controller.signal)).resolves.toEqual(result);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factors/factor%2Fa/lifecycle/evaluate',
      undefined,
      { signal: controller.signal },
    );

    await evaluateFactorLifecycle('factor/a', true);
    expect(client.post).toHaveBeenLastCalledWith(
      '/alpha-agent/factors/factor%2Fa/lifecycle/evaluate?apply=true',
      undefined,
      { signal: undefined },
    );
  });

  it('restores a downweighted factor with a required owner-attributed reason', async () => {
    const result = { factor_id: 'factor-1', lifecycle: { state: 'active' } };
    const controller = new AbortController();
    client.post.mockResolvedValue({ data: { code: 200, data: result } });

    await expect(restoreFactorLifecycle('factor-1', 'reviewed the latest code', controller.signal))
      .resolves.toEqual(result);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factors/factor-1/lifecycle/restore',
      { reason: 'reviewed the latest code' },
      { signal: controller.signal },
    );
  });

  it('reads paginated factor lifecycle history through the authenticated session', async () => {
    const result = { events: [], next_before_event_id: null };
    const controller = new AbortController();
    client.get.mockResolvedValue({ data: { code: 200, data: result } });

    await expect(getFactorLifecycleHistory(
      'factor/a', { limit: 10, beforeEventId: 21 }, controller.signal,
    )).resolves.toEqual(result);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/factors/factor%2Fa/lifecycle/history?limit=10&before_event_id=21',
      { signal: controller.signal },
    );
  });

  it('submits a factor batch with an idempotency key', async () => {
    const job = { job_id: 'job-batch', kind: 'backtest', status: 'queued' };
    client.post.mockResolvedValue({ data: { code: 202, data: job } });
    await expect(submitFactorBatch({
      factor_ids: ['factor-a', 'factor-b'],
      kind: 'backtest',
      universe: 'csi300',
      data_source: 'qlib_bin',
    })).resolves.toEqual(job);
    expect(client.post).toHaveBeenCalledWith('/alpha-agent/factor-jobs/batch', {
      factor_ids: ['factor-a', 'factor-b'],
      kind: 'backtest',
      universe: 'csi300',
      data_source: 'qlib_bin',
    }, { headers: { 'Idempotency-Key': expect.any(String) } });
  });

  it('submits private feature materialization with an idempotency key', async () => {
    const job = { job_id: 'job-materialize', kind: 'materialize', status: 'queued' };
    client.post.mockResolvedValue({ data: { code: 202, data: job } });
    await expect(submitFactorMaterialization({
      factor_id: 'factor-a',
      universe: 'csi300',
    })).resolves.toEqual(job);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factor-jobs/materialize',
      { factor_id: 'factor-a', universe: 'csi300' },
      { headers: { 'Idempotency-Key': expect.any(String) } },
    );
  });

  it('loads a filtered owner-scoped page of factor job history', async () => {
    const page = { tasks: [{ job_id: 'job-1', kind: 'backtest', status: 'completed' }], has_more: true, limit: 20, offset: 20 };
    const controller = new AbortController();
    client.get.mockResolvedValue({ data: { data: page } });

    await expect(getFactorJobHistory({ limit: 20, offset: 20, status: 'completed' }, controller.signal)).resolves.toEqual({
      jobs: page.tasks,
      has_more: true,
      limit: 20,
      offset: 20,
    });
    expect(client.get).toHaveBeenCalledWith('/alpha-agent/factor-jobs/history', {
      params: { limit: 20, offset: 20, status: 'completed' },
      signal: controller.signal,
    });
  });

  it('loads private validation evidence and latest attempt pointer', async () => {
    const data = {
      latest_validation_id: 'validation-1',
      attempts: [{ validation_id: 'validation-1', factor_id: 'factor-a', is_latest: true }],
    };
    const controller = new AbortController();
    client.get.mockResolvedValue({ data: { data } });
    await expect(getFactorValidationHistory('factor/a', controller.signal)).resolves.toEqual(data);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/factors/factor%2Fa/validation-history',
      { params: { limit: 20 }, signal: controller.signal },
    );
  });

  it('loads only the selected factor immutable source revisions', async () => {
    const controller = new AbortController();
    const revisions = [{
      revision_no: 2,
      factor_name: 'Factor A',
      factor_code: 'close',
      factor_code_sha256: 'a'.repeat(64),
      factor_formulation: null,
      created_at: '2026-10-20T00:00:00Z',
    }];
    client.get.mockResolvedValue({ data: { data: { revisions, total: 1 } } });
    await expect(getFactorSourceRevisions('factor/a', controller.signal)).resolves.toEqual(revisions);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/factors/factor%2Fa/revisions',
      { params: { limit: 20 }, signal: controller.signal },
    );
  });

  it('loads the current session factor pool with bounded query params', async () => {
    const controller = new AbortController();
    const items = [{ factor_id: 'factor-a', market: 'a_share', universe: 'csi300' }];
    client.get.mockResolvedValue({ data: { data: { items, count: 1 } } });
    await expect(listFactorPool({ universe: 'csi300', limit: 100 }, controller.signal)).resolves.toEqual(items);
    expect(client.get).toHaveBeenCalledWith('/alpha-agent/factors/pool', {
      params: { universe: 'csi300', limit: 100 },
      signal: controller.signal,
    });
  });

  it('loads only the selected owner factor panel summary', async () => {
    const controller = new AbortController();
    const panel = {
      factor_id: 'factor-a',
      market: 'a_share',
      universe: 'csi300',
      panel_version: 'version-a',
      points: [{ trade_date: '2024-01-02', coverage: 100, mean_rank_pct: 0.5, mean_zscore: 0 }],
    };
    client.get.mockResolvedValue({ data: { data: panel } });
    await expect(getFactorPoolPanel('factor-a', { universe: 'csi300' }, controller.signal))
      .resolves.toEqual(panel);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/factors/pool/factor-a/panel',
      { params: { market: 'a_share', universe: 'csi300' }, signal: controller.signal },
    );
  });

  it('adds and removes pool membership using factor-specific owner-scoped routes', async () => {
    const controller = new AbortController();
    client.post.mockResolvedValue({ data: { data: { in_pool: true } } });
    client.delete.mockResolvedValue({ data: { data: { in_pool: false } } });
    await addFactorToPool('factor/a', { universe: 'csi300' }, controller.signal);
    await removeFactorFromPool('factor/a', { universe: 'csi300' }, controller.signal);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factors/pool/factor%2Fa',
      { market: 'a_share', universe: 'csi300' },
      { signal: controller.signal },
    );
    expect(client.delete).toHaveBeenCalledWith(
      '/alpha-agent/factors/pool/factor%2Fa',
      { params: { market: 'a_share', universe: 'csi300' }, signal: controller.signal },
    );
  });

  it('rebuilds and reads private formula-similarity edges', async () => {
    const controller = new AbortController();
    const result = { factor_count: 8, edge_count: 4, threshold: 0.6, top_k: 3 };
    const edges = [{
      universe: 'csi300',
      src_factor_id: 'factor-a',
      dst_factor_id: 'factor-b',
      src_factor_name: 'A',
      dst_factor_name: 'B',
      relation: 'similar_to',
      method: 'formula',
      weight: 0.75,
    }];
    client.post.mockResolvedValue({ data: { data: result } });
    client.get.mockResolvedValue({ data: { data: { items: edges } } });
    await expect(rebuildFactorPoolEdges({ universe: 'csi300' }, controller.signal)).resolves.toEqual(result);
    await expect(listFactorPoolEdges({ universe: 'csi300' }, controller.signal)).resolves.toEqual(edges);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factors/pool/graph/rebuild',
      { market: 'a_share', universe: 'csi300' },
      { signal: controller.signal },
    );
    expect(client.get).toHaveBeenCalledWith('/alpha-agent/factors/pool/graph/edges', {
      params: { universe: 'csi300' },
      signal: controller.signal,
    });
  });

  it('rebuilds private value-correlation edges from saved panels', async () => {
    const controller = new AbortController();
    const result = { panel_count: 8, edge_count: 4, threshold: 0.5, top_k: 3 };
    client.post.mockResolvedValue({ data: { data: result } });
    await expect(rebuildFactorPoolValueEdges({ universe: 'csi300' }, controller.signal))
      .resolves.toEqual(result);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factors/pool/graph/value-rebuild',
      { market: 'a_share', universe: 'csi300' },
      { signal: controller.signal },
    );
  });

  it('rebuilds adjacent task-round edges for the private pool', async () => {
    const controller = new AbortController();
    const result = { factor_count: 8, task_count: 2, edge_count: 6 };
    client.post.mockResolvedValue({ data: { data: result } });
    await expect(rebuildFactorPoolTaskRoundEdges({ universe: 'csi300' }, controller.signal))
      .resolves.toEqual(result);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factors/pool/graph/task-round-rebuild',
      { market: 'a_share', universe: 'csi300' },
      { signal: controller.signal },
    );
  });

  it('rebuilds pool scores for the selected private universe', async () => {
    const controller = new AbortController();
    const result = {
      member_count: 12,
      scored_count: 10,
      missing_icir_count: 2,
      updated_count: 12,
      universe_count: 1,
    };
    client.post.mockResolvedValue({ data: { data: result } });
    await expect(rebuildFactorPoolScores({ universe: 'csi300' }, controller.signal))
      .resolves.toEqual(result);
    expect(client.post).toHaveBeenCalledWith(
      '/alpha-agent/factors/pool/score/rebuild',
      { market: 'a_share', universe: 'csi300' },
      { signal: controller.signal },
    );
  });

  it('downloads a PDF from the owner-scoped report endpoint', async () => {
    const controller = new AbortController();
    const pdf = new Blob(['%PDF-1.4'], { type: 'application/pdf' });
    client.get.mockResolvedValue({ data: pdf });
    await expect(downloadFactorJobReport('job/a', controller.signal)).resolves.toBe(pdf);
    expect(client.get).toHaveBeenCalledWith(
      '/alpha-agent/factor-jobs/job%2Fa/report.pdf',
      { responseType: 'blob', signal: controller.signal },
    );
  });
});
