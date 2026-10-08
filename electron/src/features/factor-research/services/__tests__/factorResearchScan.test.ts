/**
 * 「扫描因子来源」的 HTTP 层契约。
 *
 * 这一层是**唯一**没被组件测试覆盖的接缝（组件测试把服务整个 mock 掉了），
 * 而它读错时是静默的：URL 少个前缀 → 404，页面只是"扫描失败"；状态码丢了 →
 * 「数据目录没挂上」和「快照没建」被显示成同一句话，用户被指去点一键计算白等。
 * 所以这里钉住：路径、方法、dataset 参数、以及 status 原样带出。
 */
import { describe, test, expect, vi, afterEach } from 'vitest';
import { ApiError, getScanSources } from '../factorResearchService';
import { SERVICE_ENDPOINTS } from '../../../../config/services';

interface Stub {
  url: string;
  method: string;
}

function stubFetch(status: number, payload: unknown) {
  const calls: Stub[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url: String(url), method: init.method || 'GET' });
      const text = JSON.stringify(payload);
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => payload,
        text: async () => text,
      } as unknown as Response;
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

const DIFF = {
  new: [{ code: 'new1', library: 'alpha_library', library_label: 'Alpha 因子库' }],
  missing: [],
  new_by_library: { alpha_library: 1 },
  unchanged_count: 4,
  discovered_count: 5,
  catalog_count: 4,
  dataset: 'private',
  snapshot_at: '2026-09-19T11:22:33',
  snapshot_source: 'auto',
};

describe('getScanSources', () => {
  test('GET 到 /factor-research/scan，带 dataset=private，无请求体', async () => {
    const calls = stubFetch(200, DIFF);

    const out = await getScanSources();

    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toBe(`${SERVICE_ENDPOINTS.USER_SERVICE}/factor-research/scan?dataset=private`);
    expect(out.new.map((x) => x.code)).toEqual(['new1']);
    expect(out.discovered_count).toBe(5);
  });

  test('非 2xx 抬 ApiError 且保留状态码（400/503 的文案完全不同）', async () => {
    stubFetch(400, { detail: '扫描仅支持私人因子库（classic 因子目录来自内置清单，无可扫描的来源）' });

    const err = await getScanSources().catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(400);
    expect((err as Error).message).toContain('私人因子库');
  });

  test('503 的中文 detail 要出现在消息里（页面直接显示 message）', async () => {
    stubFetch(503, { detail: '6_ml_datasets 目录缺失: /data/quantdb/6_ml_datasets' });

    const err = (await getScanSources().catch((e: unknown) => e)) as ApiError;

    expect(err.status).toBe(503);
    expect(err.message).toContain('6_ml_datasets');
  });

  test('空差异原样返回（页面据此显示「已是最新」，不能靠计数猜）', async () => {
    stubFetch(200, { ...DIFF, new: [], missing: [], new_by_library: {}, unchanged_count: 4, discovered_count: 4 });

    const out = await getScanSources();

    expect(out.new).toEqual([]);
    expect(out.missing).toEqual([]);
  });
});
