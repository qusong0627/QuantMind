import { describe, expect, it } from 'vitest';
import { gateHint, inferViewState } from '../realtimeInferModel';

describe('realtimeInferModel', () => {
  it('启用态 + 模型名 + 覆盖率展示', () => {
    const v = inferViewState(
      { enabled: 'true', model_dir: '/app/models/users/1/CN/mdl_x', cadence_s: '15', min_live_coverage: '0.5' },
      { updated_at: new Date().toISOString(), counters: { published: 3, scores: 1587, last_live_coverage: 0.62 } },
    );
    expect(v.enabled).toBe(true);
    expect(v.modelName).toBe('mdl_x');
    expect(v.coverageText).toBe('62%');
    expect(v.published).toBe(3);
    expect(v.staleMirror).toBe(false);
  });

  it('缺配置/缺镜像：如实 — 且不假绿', () => {
    const v = inferViewState(null, null);
    expect(v.available).toBe(false);
    expect(v.modelName).toBe('未配置');
    expect(v.coverageText).toBe('—');
    expect(v.enabled).toBe(false);
  });

  it('镜像陈旧 >5 分钟 → staleMirror', () => {
    const old = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    const v = inferViewState({ enabled: 'true' }, { updated_at: old, counters: {} });
    expect(v.staleMirror).toBe(true);
  });

  it('闸门提示语义（关闭闸门=接受 T-1 口径）', () => {
    expect(gateHint(0)).toContain('T-1');
    expect(gateHint(0.5)).toContain('50%');
  });

  it('last_skip/last_error 透传（诚实展示）', () => {
    const v = inferViewState(
      { enabled: 'true' },
      { counters: { last_skip: 'live_coverage 0% < 50%', last_error: 'ONNX 导出失败' } },
    );
    expect(v.lastSkip).toContain('live_coverage');
    expect(v.lastError).toContain('ONNX');
  });

  it('ONNX 三态 + modelDir 透传（就绪/缺失/未取到）', () => {
    const base = { enabled: 'true', model_dir: '/app/models/users/1/CN/mdl_x' };
    const ready = inferViewState(base, null, Date.now(), false, {
      ready: true,
      size_bytes: 18445,
    });
    expect(ready.modelDir).toBe('/app/models/users/1/CN/mdl_x');
    expect(ready.onnxReady).toBe(true);
    expect(ready.onnxText).toContain('就绪');
    expect(ready.onnxText).toContain('KB');

    const missing = inferViewState(base, null, Date.now(), false, { ready: false });
    expect(missing.onnxReady).toBe(false);
    expect(missing.onnxText).toContain('缺失');

    const unknown = inferViewState(base, null, Date.now(), false, null);
    expect(unknown.onnxReady).toBeNull();
    expect(unknown.onnxText).toBe('—');
  });
});
