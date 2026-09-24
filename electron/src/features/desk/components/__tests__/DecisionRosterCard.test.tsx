/**
 * 交易台「决策模型名册」卡。
 *
 * 四条要在测试里钉住：
 * ① 卡头不得出现内部工单号（T-…/P2.9 对使用者是噪音）；
 * ② **决策轮开关只展示不代写**（改了必须重启 trade 服务，界面不提供写入口）；
 * ③ 逐家错误原文要显示出来（点名变量），不能只给一句「配置有误」；
 * ④ 单家三件套不可用时**清空按钮禁用**并显示原因（后端会拒绝并回滚，前端先说清）。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import type { Mock } from 'vitest';

vi.mock('../../services/decisionRosterService', () => ({
  getRoster: vi.fn(),
  saveRoster: vi.fn(),
  clearRoster: vi.fn(),
  RosterApiError: class extends Error {},
}));

import { clearRoster, getRoster, saveRoster } from '../../services/decisionRosterService';
import { DecisionRosterCard } from '../DecisionRosterCard';

const mockGet = getRoster as unknown as Mock;
const mockSave = saveRoster as unknown as Mock;
const mockClear = clearRoster as unknown as Mock;

const ENTRY_OK = {
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
  status: {
    ts: '2026-09-24T14:30:02+08:00',
    slot_label: '14:30',
    status: 'ok',
    legs: 3,
    submitted: 3,
    note: '提交完成',
  },
};

const ENTRY_BAD = {
  ...ENTRY_OK,
  index: 2,
  model: 'qwen3-max',
  agent: 'qwen3-max',
  ok: false,
  error: '决策 LLM 变量 MISSING_KEY 为空或仍是占位符',
  // 端点与 key 都得清掉：spread 会把它继承成第 1 家的字面端点，
  // 那样「这家坏了」的样本就自带一个会被原样发回去的端点，测出来的载荷是假的。
  base_url: '',
  base_url_env: '',
  api_key_env: 'MISSING_KEY',
  api_key_inline: false,
  api_key_set: false,
  status: null,
};

function rosterState(over: Record<string, unknown> = {}) {
  return {
    roster_configured: true,
    source: 'roster',
    entries: [ENTRY_OK, ENTRY_BAD],
    limit: 8,
    env: 'QM_DECISION_LLM_ROSTER',
    runtime_env_path: '/app/config/runtime.env',
    round: { enabled: false, env: 'QM_DECISION_ROUND_ENABLED', note: '' },
    last: { ts: '2026-09-24T14:30:02+08:00', slot_label: '14:30', status: 'ok', legs: 3 },
    status_error: '',
    single: { ok: true, error: '' },
    error: '',
    ...over,
  };
}

function loaded(state: Record<string, unknown>) {
  mockGet.mockResolvedValue({ state, accepted: {} });
  return render(<DecisionRosterCard />);
}

describe('DecisionRosterCard', () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockSave.mockReset();
    mockClear.mockReset();
  });

  it('卡头与正文都不出现内部工单号，标题就是「决策模型名册」', async () => {
    loaded(rosterState());
    await screen.findAllByText('glm-4.6'); // 模型名与身份（agent）同名，出现两次
    const text = document.body.textContent || '';
    expect(text).toContain('决策模型名册');
    expect(text).not.toMatch(/T-[A-Z0-9-]+/);
    expect(text).not.toContain('P2.9');
  });

  it('逐家列出模型与可用性，坏的那家把错误原文显示出来', async () => {
    loaded(rosterState());
    await screen.findAllByText('glm-4.6'); // 模型名与身份（agent）同名，出现两次
    expect(screen.getAllByText('qwen3-max').length).toBeGreaterThan(0);
    expect(screen.getByText('可用')).toBeTruthy();
    expect(screen.getByText('不可用')).toBeTruthy();
    expect(screen.getByText('决策 LLM 变量 MISSING_KEY 为空或仍是占位符')).toBeTruthy();
    expect(screen.getByText(/名册 2\/8 家 · 1 家不可用/)).toBeTruthy();
    // 上一轮的状态镜像按家显示
    expect(screen.getAllByText(/14:30 · 完成 · 腿 3 · 提交 3/).length).toBeGreaterThan(0);
  });

  it('决策轮开关只展示：给出当前值 + 说明改它要重启，且没有写入口', async () => {
    loaded(rosterState());
    await screen.findAllByText('glm-4.6'); // 模型名与身份（agent）同名，出现两次
    expect(screen.getByText('关')).toBeTruthy();
    expect(screen.getByText(/开关只在启动时判一次/)).toBeTruthy();
    // 卡上不得出现任何可切换决策轮的控件（开关/按钮/复选框的文案里都不能提决策轮）
    const controls = [...document.querySelectorAll('button, input')];
    const labels = controls.map((el) => `${el.textContent || ''}${el.getAttribute('aria-label') || ''}`);
    expect(labels.some((text) => text.includes('决策轮'))).toBe(false);
  });

  it('非管理员：403 降级为「需管理员权限」，不渲染配置面', async () => {
    mockGet.mockRejectedValue(Object.assign(new Error('403'), { status: 403 }));
    render(<DecisionRosterCard />);
    await screen.findByText(/需管理员权限/);
    expect(screen.queryByText('编辑名册')).toBeNull();
  });

  it('未启名册时显示单家三件套的实况（含错误原文）', async () => {
    loaded(
      rosterState({
        roster_configured: false,
        source: 'none',
        entries: [],
        single: { ok: false, error: '决策 LLM 未配置：QM_DECISION_LLM_BASE_URL 为空' },
      }),
    );
    await screen.findByText(/未启名册，走单家三件套/);
    expect(screen.getByText(/QM_DECISION_LLM_BASE_URL 为空/)).toBeTruthy();
    expect(screen.getByText('一家都没配')).toBeTruthy();
  });

  it('单家三件套不可用时清空按钮禁用并说明原因', async () => {
    loaded(rosterState({ single: { ok: false, error: '未配置' } }));
    await screen.findAllByText('glm-4.6'); // 模型名与身份（agent）同名，出现两次
    const button = screen.getByRole('button', { name: /清空名册/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText(/清空已禁用/)).toBeTruthy();
  });

  it('保存：只发改动过的字段（key 留空就不发 api_key）', async () => {
    mockSave.mockResolvedValue(rosterState());
    loaded(rosterState());
    await screen.findAllByText('glm-4.6'); // 模型名与身份（agent）同名，出现两次
    fireEvent.click(screen.getByRole('button', { name: '编辑名册' }));

    const modelInput = screen.getByLabelText('第 1 家模型名') as HTMLInputElement;
    expect(modelInput.value).toBe('glm-4.6'); // 草稿来自现状
    fireEvent.change(modelInput, { target: { value: 'glm-4.7' } });
    fireEvent.click(screen.getByRole('button', { name: '保存名册' }));

    await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(1));
    expect(mockSave).toHaveBeenCalledWith([
      { model: 'glm-4.7', base_url: 'https://open.bigmodel.cn/api/paas/v4' },
      { model: 'qwen3-max' }, // 第二行没改：不带 key/端点（=沿用）
    ]);
  });

  it('保存失败：逐条原因全部展示（不只第一条）', async () => {
    const err = Object.assign(new Error('第 2 项（qwen3-max）的 timeout=很久 不是数字'), {
      errors: [
        '第 2 项（qwen3-max）的 timeout=很久 不是数字',
        '第 3 项（glm-4.6）与第 1 项归一后重名（agent=glm-4.6）',
      ],
    });
    mockSave.mockRejectedValue(err);
    loaded(rosterState());
    await screen.findAllByText('glm-4.6'); // 模型名与身份（agent）同名，出现两次
    fireEvent.click(screen.getByRole('button', { name: '编辑名册' }));
    fireEvent.click(screen.getByRole('button', { name: '保存名册' }));

    await screen.findByText(/timeout=很久 不是数字/);
    expect(screen.getByText(/归一后重名/)).toBeTruthy();
    // 失败后仍停在编辑态（用户改完能再提交，不用重新输一遍）
    expect(screen.getByRole('button', { name: '保存名册' })).toBeTruthy();
  });

  it('清空要点两次确认才对（第一次只变成确认态）', async () => {
    mockClear.mockResolvedValue(rosterState({ roster_configured: false, source: 'single', entries: [] }));
    loaded(rosterState());
    await screen.findAllByText('glm-4.6'); // 模型名与身份（agent）同名，出现两次
    fireEvent.click(screen.getByRole('button', { name: /清空名册/ }));
    expect(mockClear).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /再点一次确认清空/ }));
    await waitFor(() => expect(mockClear).toHaveBeenCalledTimes(1));
    await screen.findByText(/已回到单家三件套/);
  });

  it('加一行 + 删一行：草稿数量跟着走', async () => {
    loaded(rosterState());
    await screen.findAllByText('glm-4.6'); // 模型名与身份（agent）同名，出现两次
    fireEvent.click(screen.getByRole('button', { name: '编辑名册' }));
    fireEvent.click(screen.getByRole('button', { name: /加一家/ }));
    expect(screen.getByLabelText('第 3 家模型名')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('移除第 3 家'));
    expect(screen.queryByLabelText('第 3 家模型名')).toBeNull();
  });
});
