/**
 * 决策名册视图模型（纯函数）。
 *
 * 三条口径要钉住，它们各自对应一个真实会犯的错：
 * ① key **只报状态不回显值**（后端只回布尔与变量名，界面别自己拼半个 key）；
 * ② 编辑草稿里**留空 ≠ 要删**：留空是不传（沿用），只有显式 clearKey 才发 clear_api_key；
 * ③ 未登记的轮次状态**原样回显**，不编中文（编了就等于替后端发明了一套状态词）。
 */

import { describe, it, expect } from 'vitest';
import {
  blankDraft,
  buildSavePayload,
  clearGuard,
  draftFromEntry,
  entryEndpointText,
  entryKeyText,
  entryStatusLine,
  lastRoundLine,
  overLimitHint,
  rosterHeadline,
  roundStatusLabel,
  roundStatusTone,
  sourceLabel,
  sourceTone,
  tuningText,
  type RosterEntryView,
  type RosterState,
} from '../decisionRosterModel';

function entry(over: Partial<RosterEntryView> = {}): RosterEntryView {
  return {
    index: 1,
    model: 'glm-4.6',
    agent: 'glm-4.6',
    ok: true,
    error: '',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    base_url_env: '',
    api_key_env: 'QM_DECISION_LLM_KEY_GLM_4_6',
    api_key_inline: false,
    api_key_set: true,
    timeout: null,
    max_tokens: null,
    temperature: null,
    status: null,
    ...over,
  };
}

function state(over: Partial<RosterState> = {}): RosterState {
  return {
    roster_configured: true,
    source: 'roster',
    entries: [entry()],
    limit: 8,
    env: 'QM_DECISION_LLM_ROSTER',
    runtime_env_path: '/app/config/runtime.env',
    round: { enabled: false, env: 'QM_DECISION_ROUND_ENABLED', note: '' },
    last: null,
    status_error: '',
    single: { ok: true, error: '' },
    error: '',
    ...over,
  };
}

describe('模式标签', () => {
  it('三种来源各给一个中文，未登记的原样回显', () => {
    expect([sourceLabel('roster'), sourceLabel('single'), sourceLabel('none')]).toEqual([
      '名册',
      '单家三件套',
      '未配置',
    ]);
    expect(sourceLabel('weird')).toBe('weird');
    expect(sourceLabel('')).toBe('未知');
  });

  it('色调：名册=好、单家=注意、其余=坏', () => {
    expect([sourceTone('roster'), sourceTone('single'), sourceTone('none'), sourceTone('x')]).toEqual([
      'ok',
      'warn',
      'bad',
      'bad',
    ]);
  });
});

describe('rosterHeadline', () => {
  it('未启名册时区分「单家还在」与「一家都没配」', () => {
    expect(rosterHeadline(state({ roster_configured: false, source: 'single', entries: [] }))).toBe(
      '单家三件套（未启名册）',
    );
    expect(rosterHeadline(state({ roster_configured: false, source: 'none', entries: [] }))).toBe(
      '一家都没配',
    );
  });

  it('启了名册时报家数，有坏的就一起报', () => {
    expect(rosterHeadline(state())).toBe('名册 1/8 家');
    const mixed = state({ entries: [entry(), entry({ index: 2, model: 'qwen3-max', ok: false })] });
    expect(rosterHeadline(mixed)).toBe('名册 2/8 家 · 1 家不可用');
  });
});

describe('逐家展示文案', () => {
  it('key 只报状态：变量形态/字面值/回落全局/没配各有话', () => {
    expect(entryKeyText(entry())).toBe('已配置（变量 QM_DECISION_LLM_KEY_GLM_4_6）');
    expect(
      entryKeyText(entry({ api_key_env: 'GLM_API_KEY', api_key_set: false })),
    ).toBe('未配置（变量 GLM_API_KEY 是空的）');
    expect(entryKeyText(entry({ api_key_inline: true, api_key_env: '' }))).toContain('写在名册里');
    expect(entryKeyText(entry({ api_key_env: '', api_key_set: true }))).toBe(
      '已配置（回落全局三件套）',
    );
    expect(entryKeyText(entry({ api_key_env: '', api_key_set: false }))).toBe('未配置');
  });

  it('端点：字面值优先，其次变量名，都没有就说回落全局', () => {
    expect(entryEndpointText(entry())).toBe('https://open.bigmodel.cn/api/paas/v4');
    expect(entryEndpointText(entry({ base_url: '', base_url_env: 'GLM_API_BASE' }))).toBe(
      '变量 GLM_API_BASE',
    );
    expect(entryEndpointText(entry({ base_url: '', base_url_env: '' }))).toBe('（回落全局端点）');
  });

  it('一个调参都没覆盖时说「跟随全局」，不显示成三个空值', () => {
    expect(tuningText(entry())).toBe('跟随全局');
    expect(tuningText(entry({ timeout: 120, max_tokens: 2000, temperature: 0.3 }))).toBe(
      '超时 120s · 上限 2000 · 温度 0.3',
    );
  });
});

describe('轮次状态', () => {
  it('五个状态各有中文，未登记的取值原样回显', () => {
    expect(
      ['ok', 'skipped', 'aborted', 'llm_failed', 'error'].map(roundStatusLabel),
    ).toEqual(['完成', '跳过', '中止', '模型未就绪', '出错']);
    expect(roundStatusLabel('brand_new')).toBe('brand_new');
    expect(roundStatusLabel(undefined)).toBe('无记录');
  });

  it('色调把「跳过/中止」与「模型未就绪/出错」分开', () => {
    expect(['ok', 'skipped', 'aborted', 'llm_failed', 'error', undefined].map(roundStatusTone)).toEqual(
      ['ok', 'warn', 'warn', 'bad', 'bad', 'off'],
    );
  });

  it('状态行带时间/槽位/结果/腿数，note 附在后面', () => {
    const line = entryStatusLine({
      ts: '2026-09-24T14:30:02+08:00',
      slot_label: '14:30',
      status: 'ok',
      legs: 3,
      submitted: 3,
      note: '提交完成',
    });
    expect(line).toBe('2026-09-24 14:30 · 14:30 · 完成 · 腿 3 · 提交 3 —— 提交完成');
    expect(entryStatusLine(null)).toBe('无状态镜像');
  });

  it('汇总行：无镜像/无 state 都不编内容', () => {
    expect(lastRoundLine(null)).toBe('—');
    expect(lastRoundLine(state())).toContain('还没跑成过一轮');
    expect(lastRoundLine(state({ last: { status: 'ok', slot_label: '14:30' } }))).toContain('完成');
  });
});

describe('清空闸门（条件取服务端 single.ok，不自己判）', () => {
  it('没有名册时没有可清的', () => {
    expect(clearGuard(state({ roster_configured: false }))).toBe('当前没有名册可清');
  });

  it('单家三件套不可用时禁用并说清原因与下一步', () => {
    const why = clearGuard(state({ single: { ok: false, error: '未配置' } }));
    expect(why).toContain('QM_DECISION_LLM_BASE_URL');
    expect(why).toContain('回滚');
  });

  it('单家可用时放行', () => {
    expect(clearGuard(state())).toBe('');
  });
});

describe('家数上限只提示不拦', () => {
  it('超限给提示，未超限不说话', () => {
    expect(overLimitHint(state(), 8)).toBe('');
    expect(overLimitHint(state(), 9)).toContain('超过上限 8 家');
  });
});

describe('草稿 → 载荷', () => {
  it('留空字段不传：没填 key 就不发 api_key（=沿用原 key）', () => {
    const payload = buildSavePayload([{ ...blankDraft(), model: ' glm-4.6 ' }]);
    expect(payload).toEqual({ entries: [{ model: 'glm-4.6' }] });
  });

  it('填了 key 才发 api_key；显式 clearKey 才发 clear_api_key', () => {
    const withKey = buildSavePayload([
      { ...blankDraft(), model: 'glm-4.6', apiKey: 'sk-new', baseUrl: 'https://x/v1', timeout: '90' },
    ]);
    expect(withKey.entries[0]).toEqual({
      model: 'glm-4.6',
      base_url: 'https://x/v1',
      api_key: 'sk-new',
      timeout: 90,
    });
    const cleared = buildSavePayload([{ ...blankDraft(), model: 'glm-4.6', clearKey: true }]);
    expect(cleared.entries[0]).toEqual({ model: 'glm-4.6', clear_api_key: true });
    expect('api_key' in cleared.entries[0]).toBe(false);
  });

  it('非数字串当没填（不传 NaN 给后端）', () => {
    const payload = buildSavePayload([
      { ...blankDraft(), model: 'glm-4.6', timeout: '很久', maxTokens: '2000', temperature: '0.3' },
    ]);
    expect(payload.entries[0]).toEqual({ model: 'glm-4.6', max_tokens: 2000, temperature: 0.3 });
  });

  it('draftFromEntry 不回填 key、也不把变量名当字面值回填', () => {
    const draft = draftFromEntry(
      entry({ base_url: '', base_url_env: 'GLM_API_BASE', timeout: 120, max_tokens: null }),
    );
    expect(draft.apiKey).toBe('');
    expect(draft.baseUrl).toBe('');
    expect(draft.timeout).toBe('120');
    expect(draft.maxTokens).toBe('');
  });

  it('draftFromEntry 对字面值端点照常回填（用户要能看着它改）', () => {
    expect(draftFromEntry(entry()).baseUrl).toBe('https://open.bigmodel.cn/api/paas/v4');
  });
});
