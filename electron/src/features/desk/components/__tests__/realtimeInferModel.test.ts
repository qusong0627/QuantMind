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

  it('中文模型名：有显示名用显示名，缺失回落目录基名', () => {
    const base = { enabled: 'true', model_dir: '/app/models/users/1/CN/mdl_cn_train_x' };
    const withName = inferViewState(base, null, Date.now(), false, null, '远程测试9_2024全年_CN');
    expect(withName.modelDisplayName).toBe('远程测试9_2024全年_CN');
    expect(withName.modelName).toBe('mdl_cn_train_x');

    const without = inferViewState(base, null, Date.now(), false, null, '');
    expect(without.modelDisplayName).toBe('mdl_cn_train_x');
  });
});

/**
 * 专业实时推理该显示的运行面：**心跳 / 时延 / 治理 / 台账**。
 *
 * 用户原话：「这个内容和功能有些简陋啊 不像是专业的实时推理」。
 * 此前的卡片只把 counters 里的 4 个数字摆出来（已发布/分数条数/闸门拦截/最近周期），
 * 一个运维看这张卡时真正要回答的是：循环还活着吗、跑一轮要多久、有没有在降级、
 * 台账有没有写坏、这批分能不能复现。这些数据引擎侧本来就每周期镜像，只是没显示。
 */
describe('realtimeInferModel · 运行面（心跳/时延/治理/台账）', () => {
  const cfg = { enabled: 'true', cadence_s: '15', min_live_coverage: '0.5' };
  const T0 = Date.parse('2026-09-20T22:00:00+08:00');
  const iso = (msAgo: number) => new Date(T0 - msAgo).toISOString();

  it('心跳新鲜（< 2×节拍）→ live，并给出年龄', () => {
    const v = inferViewState(cfg, { updated_at: iso(5_000), counters: {} }, T0);
    expect(v.heartbeat).toBe('live');
    expect(v.heartbeatAgeS).toBe(5);
    expect(v.heartbeatText).toContain('5s');
  });

  it('心跳超过 2×节拍 → stale，文案点明节拍与"循环可能已停"', () => {
    const v = inferViewState(cfg, { updated_at: iso(90_000), counters: {} }, T0);
    expect(v.heartbeat).toBe('stale');
    expect(v.heartbeatText).toContain('15s');
    expect(v.heartbeatText).toMatch(/循环|停/);
  });

  it('从未写过镜像 → missing（不是"正常"，也不是"陈旧"）', () => {
    const v = inferViewState(cfg, { updated_at: null, counters: {} }, T0);
    expect(v.heartbeat).toBe('missing');
    expect(v.heartbeatAgeS).toBeNull();
  });

  it('服务端时间超前（时钟偏差）不得判成"陈旧"——那会冤枉一个健康的服务', () => {
    const v = inferViewState(cfg, { updated_at: iso(-600_000), counters: {} }, T0);
    expect(v.heartbeat).toBe('skewed');
    expect(v.heartbeatText).toContain('时钟');
  });

  it('节拍 600s 时 90s 不算陈旧（阈值跟随节拍，不写死 5 分钟）', () => {
    const slow = { ...cfg, cadence_s: '600' };
    expect(inferViewState(slow, { updated_at: iso(90_000), counters: {} }, T0).heartbeat).toBe('live');
  });

  it('治理器：正常/降载/重度降载三档 + 有效节拍', () => {
    const ok = inferViewState(cfg, { counters: { degrade_level: 0 } }, T0, false, null, '', { level: 0, effective_cadence_s: 15 });
    expect(ok.degradeText).toBe('正常');
    expect(ok.degradeTone).toBe('ok');
    expect(ok.effectiveCadenceS).toBe(15);

    const g1 = inferViewState(cfg, { counters: {} }, T0, false, null, '', { level: 1, effective_cadence_s: 15 });
    expect(g1.degradeText).toContain('降载');
    expect(g1.degradeTone).toBe('warn');

    const g2 = inferViewState(cfg, { counters: {} }, T0, false, null, '', { level: 2, effective_cadence_s: 30 });
    expect(g2.degradeText).toMatch(/重度|放慢/);
    expect(g2.degradeTone).toBe('bad');
    expect(g2.effectiveCadenceS).toBe(30);
  });

  it('无治理器快照时回落 counters.degrade_level（旧镜像也要能显示）', () => {
    const v = inferViewState(cfg, { counters: { degrade_level: 1 } }, T0);
    expect(v.degradeTone).toBe('warn');
    expect(v.effectiveCadenceS).toBe(15);
  });

  it('时延：本周期 + 近窗 p95 + 节拍预算；p95 样本不足时如实说"样本不足"', () => {
    const withP95 = inferViewState(cfg, { counters: { last_ms: 486.6 } }, T0, false, null, '', { p95_ms: 13000 });
    expect(withP95.lastMsText).toBe('487 ms');
    expect(withP95.p95Text).toBe('13.0 s');
    expect(withP95.p95OverBudget).toBe(true);

    const noP95 = inferViewState(cfg, { counters: { last_ms: 380.3 } }, T0, false, null, '', { p95_ms: null });
    expect(noP95.p95Text).toBe('样本不足');
    expect(noP95.p95OverBudget).toBe(false);

    const none = inferViewState(cfg, { counters: {} }, T0);
    expect(none.lastMsText).toBe('—');
    expect(none.p95Text).toBe('—');
  });

  it('产出：累计周期/发布周期/发布率，分数条数与本次条数分开', () => {
    const v = inferViewState(
      cfg,
      { counters: { cycles: 143, published: 19, scores: 10032, last_scores: 528 } },
      T0,
    );
    expect(v.cycles).toBe(143);
    expect(v.publishRateText).toBe('19/143 周期');
    expect(v.scores).toBe(10032);
    expect(v.lastScores).toBe(528);
  });

  it('台账：条数与写错数（写错 > 0 必须能被看见）', () => {
    const v = inferViewState(cfg, { counters: { ledger_entries: 19, ledger_errors: 0 } }, T0);
    expect(v.ledgerText).toBe('19 条');
    expect(v.ledgerErrors).toBe(0);

    const bad = inferViewState(cfg, { counters: { ledger_entries: 19, ledger_errors: 2 } }, T0);
    expect(bad.ledgerErrors).toBe(2);
    expect(bad.ledgerText).toContain('2 错');
  });

  it('可复现线索：run_id 与基线来源透传（回放验收要用）', () => {
    const v = inferViewState(
      { ...cfg, model_dir: '/app/models/users/1/CN/mdl_cn' },
      { counters: { last_run_id: 'rt-mdl_cn-20260920' } },
      T0,
      false,
      null,
      '远程测试9',
      { level: 0 },
      'quantdb_factors 直读（与批量推理同源）',
    );
    expect(v.lastRunId).toBe('rt-mdl_cn-20260920');
    expect(v.baselineSource).toContain('quantdb_factors');
  });

  it('镜像时间以本地 hh:mm:ss 展示（面板要能一眼看到"这是几点的状态"）', () => {
    const v = inferViewState(cfg, { updated_at: new Date(2026, 8, 20, 22, 3, 4).toISOString(), counters: {} }, T0);
    expect(v.mirrorUpdatedText).toBe('22:03:04');
  });
});
