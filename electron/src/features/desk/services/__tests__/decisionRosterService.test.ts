/**
 * 决策名册服务（HTTP 层）。
 *
 * 这一层是**唯一**没被卡测试覆盖的接缝（卡测试把服务整个 mock 掉了），而它的失败形态
 * 是静默的：形状读错时 `state` 变成 `{}`，界面只是显示成「一家都没配」——不报错、不红。
 * 所以这里逐条钉住线上形状：
 * ① 三个方法**同一个读法**（现状在 `data`，不在 `data.state`）；
 * ② 非 2xx 抬 `RosterApiError`，`status` 与逐条 `errors` 都要带上（卡靠 403 降级）；
 * ③ 401/403 这种没有 JSON 体的响应，也不能把 `status` 丢掉。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  RosterApiError,
  clearRoster,
  getRoster,
  saveRoster,
} from '../decisionRosterService';

const STATE = {
  roster_configured: true,
  source: 'roster',
  entries: [],
  limit: 8,
  env: 'QM_DECISION_LLM_ROSTER',
  runtime_env_path: '/app/config/runtime.env',
  round: { enabled: false, env: 'QM_DECISION_ROUND_ENABLED', note: '' },
  last: null,
  status_error: '',
  single: { ok: true, error: '' },
  error: '',
};

interface Stub {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

function stubFetch(status: number, payload: unknown, asText = false) {
  const calls: Stub[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({
        url: String(url),
        method: init.method || 'GET',
        headers: (init.headers || {}) as Record<string, string>,
        body: init.body ? JSON.parse(String(init.body)) : undefined,
      });
      const text = asText ? String(payload) : JSON.stringify(payload);
      return {
        ok: status >= 200 && status < 300,
        status,
        text: async () => text,
      } as unknown as Response;
    }),
  );
  return calls;
}

describe('decisionRosterService', () => {
  beforeEach(() => {
    localStorage.setItem('access_token', 'tok-abc');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('GET：现状直接读 data，accepted 单独抬出来', async () => {
    const calls = stubFetch(200, { success: true, data: STATE, accepted: { model: '模型名' } });
    const got = await getRoster();
    expect(got.state.source).toBe('roster');
    expect(got.accepted.model).toBe('模型名');
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toMatch(/\/decision\/roster$/);
    expect(calls[0].headers.Authorization).toBe('Bearer tok-abc');
  });

  it('PUT：现状也在 data（**不是** data.state），载荷是 {entries}', async () => {
    const calls = stubFetch(200, { success: true, data: STATE, applied: { ok: true } });
    const state = await saveRoster([{ model: 'glm-4.6' }]);
    expect(state.source).toBe('roster');
    expect(state.roster_configured).toBe(true);
    expect(calls[0].method).toBe('PUT');
    expect(calls[0].body).toEqual({ entries: [{ model: 'glm-4.6' }] });
  });

  it('DELETE：现状同样读 data', async () => {
    const calls = stubFetch(200, {
      success: true,
      data: { ...STATE, roster_configured: false, source: 'single' },
    });
    const state = await clearRoster();
    expect(state.source).toBe('single');
    expect(calls[0].method).toBe('DELETE');
  });

  it('400：抬 RosterApiError，逐条原因全部带出（不只第一条）', async () => {
    stubFetch(400, {
      success: false,
      error: '第 1 项缺 model',
      errors: ['第 1 项缺 model', '第 3 项与第 1 项归一后重名'],
    });
    await expect(saveRoster([{ model: '' }])).rejects.toMatchObject({
      status: 400,
      message: '第 1 项缺 model',
      errors: ['第 1 项缺 model', '第 3 项与第 1 项归一后重名'],
    });
  });

  it('403：没有 JSON 体时 status 仍要保住（卡靠它降级成「需管理员权限」）', async () => {
    stubFetch(403, 'Forbidden', true);
    const err = await getRoster().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(RosterApiError);
    expect((err as RosterApiError).status).toBe(403);
    expect((err as RosterApiError).errors).toEqual([]);
  });

  it('没登录时不带 Authorization 头（别发个空的 Bearer）', async () => {
    localStorage.removeItem('access_token');
    const calls = stubFetch(200, { success: true, data: STATE });
    await getRoster();
    expect('Authorization' in calls[0].headers).toBe(false);
  });
});
