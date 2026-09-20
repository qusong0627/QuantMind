/**
 * 控制台状态机测试（T-RC-20）。
 *
 * 重点是新增的 `config_pending`（新版本待生效）：热更新只写 Redis 快照，托管
 * 调度器**下一个周期**才重读。这中间的窗口如果界面显示「运行中 · v3」而不提示
 * 「v3 尚未生效」，用户会以为参数已经变了——而实际下单用的还是旧参数。
 */

import { describe, expect, it } from 'vitest';
import { deriveRunState, RUN_STATE_META, buildInputNodes } from '../topologyTypes';
import type { RealTradingStatus } from '../../../../../services/realTradingService';

const base = (over: Partial<RealTradingStatus>): RealTradingStatus =>
  ({ status: 'running', user_id: 'u1', ...over }) as RealTradingStatus;

describe('deriveRunState', () => {
  it('maps the backend status vocabulary, defaulting unknown to idle', () => {
    expect(deriveRunState(null)).toBe('idle');
    expect(deriveRunState(base({ status: 'starting' }))).toBe('starting');
    expect(deriveRunState(base({ status: 'running' }))).toBe('running');
    expect(deriveRunState(base({ status: 'stopped' }))).toBe('stopped');
    expect(deriveRunState(base({ status: 'not_running' }))).toBe('stopped');
    expect(deriveRunState(base({ status: 'error' }))).toBe('error');
    expect(deriveRunState(base({ status: 'wat' as never }))).toBe('idle');
  });

  it('running + observe_only is the observing state, not plain running', () => {
    expect(deriveRunState(base({ status: 'running', trading_permission: 'observe_only' }))).toBe('observing');
    expect(deriveRunState(base({ status: 'running', trading_permission: 'full' }))).toBe('running');
  });

  it('flags config_pending when a hot update has not been picked up by a cycle yet', () => {
    // 配置在 10:00 更新，最近一个周期是 09:30 → 新版本还没生效
    const pending = deriveRunState(
      base({
        config_version: 3,
        config_updated_at: '2026-09-20T10:00:00+00:00',
        latest_cycle: { at: '2026-09-20T09:30:00+00:00', status: 'completed' },
      }),
    );
    expect(pending).toBe('config_pending');
    expect(RUN_STATE_META.config_pending.label).toContain('待生效');
  });

  it('is plain running once a cycle has run after the config update', () => {
    const applied = deriveRunState(
      base({
        config_version: 3,
        config_updated_at: '2026-09-20T10:00:00+00:00',
        latest_cycle: { at: '2026-09-20T10:05:00+00:00', status: 'completed' },
      }),
    );
    expect(applied).toBe('running');
  });

  it('never claims config_pending without evidence (no version, no cycle, bad dates)', () => {
    // 没有 config_version → 从来没热更过
    expect(deriveRunState(base({ config_updated_at: '2026-09-20T10:00:00Z' }))).toBe('running');
    // 有版本但没有周期记录 → 无从判断「生效没」，不臆断待生效
    expect(deriveRunState(base({ config_version: 2, config_updated_at: '2026-09-20T10:00:00Z' }))).toBe('running');
    // 时间不可解析 → 不判
    expect(
      deriveRunState(base({ config_version: 2, config_updated_at: 'not-a-date', latest_cycle: { at: 'x' } })),
    ).toBe('running');
    // 待生效只对 running 有意义：停止态不该说「待生效」
    expect(
      deriveRunState(
        base({
          status: 'stopped',
          config_version: 2,
          config_updated_at: '2026-09-20T10:00:00Z',
          latest_cycle: { at: '2026-09-20T09:00:00Z' },
        }),
      ),
    ).toBe('stopped');
  });

  it('every run state has display metadata (no undefined lookups at render time)', () => {
    for (const key of ['idle', 'starting', 'running', 'observing', 'config_pending', 'stopped', 'error']) {
      const meta = RUN_STATE_META[key as keyof typeof RUN_STATE_META];
      expect(meta?.label).toBeTruthy();
      expect(meta?.dot).toBeTruthy();
    }
  });
});

describe('buildInputNodes', () => {
  const items = [
    { key: 'stream_freshness', label: '行情新鲜度', passed: true, detail: 'source=tdx_bridge' },
    { key: 'redis_ping', label: 'Redis', passed: false, detail: '连接超时' },
  ] as never[];

  it('groups precheck items into topology nodes and surfaces the first failure', () => {
    const nodes = buildInputNodes(items, false);
    const market = nodes.find((n) => n.key === 'market');
    const redis = nodes.find((n) => n.key === 'redis');
    expect(market?.state).toBe('ok');
    expect(redis?.state).toBe('error');
    expect(redis?.summary).toContain('连接超时');
    expect(redis?.details).toHaveLength(1);
  });

  it('观察态 detail downgrades an otherwise-passing item to warn', () => {
    const nodes = buildInputNodes(
      [{ key: 'stream_freshness', label: '行情', passed: true, detail: '观察态：仅观察' }] as never[],
      false,
    );
    expect(nodes.find((n) => n.key === 'market')?.state).toBe('warn');
  });

  it('tolerates empty input', () => {
    expect(buildInputNodes([], false)).toEqual([]);
    expect(buildInputNodes(undefined as never, false)).toEqual([]);
  });
});
