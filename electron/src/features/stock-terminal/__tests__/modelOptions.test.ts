/**
 * 模型下拉选项构建（候选信号页「推理模型」）。
 *
 * 2026-10-20 用户复报「选择模型显示不全」：后端原先把「近 90 天无信号」的
 * 注册表模型整段丢掉。现在后端全量回传，前端用分组把事实摆出来——
 * 「近 90 天有信号」在前、「近 90 天无信号」在后，且后者仍然可选。
 */

import { describe, it, expect } from 'vitest';
import {
  buildModelSelectOptions,
  buildTerminalModelOptions,
  modelOptionLabel,
} from '../modelOptions';
import type { StockModelOption } from '../../stock-terminal-shared/types';

const m = (patch: Partial<StockModelOption>): StockModelOption => ({
  model_id: 'mdl_x',
  display_name: '模型 X',
  ...patch,
});

describe('modelOptionLabel', () => {
  it('近 30 天有信号：只显示名称', () => {
    expect(modelOptionLabel(m({ has_signals_30d: true, has_signals_90d: true })))
      .toBe('模型 X');
  });

  it('30–90 天无信号：带弱化后缀', () => {
    expect(modelOptionLabel(m({ has_signals_30d: false, has_signals_90d: true })))
      .toBe('模型 X · 近 30 天无信号');
  });

  it('近 90 天无信号：交给分组标题交代，不重复后缀', () => {
    expect(modelOptionLabel(m({ has_signals_30d: false, has_signals_90d: false })))
      .toBe('模型 X');
  });

  it('老后端无 has_signals_90d：保持旧文案', () => {
    expect(modelOptionLabel(m({ has_signals_30d: false })))
      .toBe('模型 X · 近 30 天无信号');
  });

  it('没有显示名时回退 model_id', () => {
    expect(modelOptionLabel(m({ display_name: '', model_id: 'mdl_abc' }))).toBe('mdl_abc');
  });
});

describe('buildModelSelectOptions', () => {
  it('全部都有近 90 天信号：退回平铺列表（不显示分组）', () => {
    const out = buildModelSelectOptions([
      m({ model_id: 'a', display_name: 'A', has_signals_90d: true }),
      m({ model_id: 'b', display_name: 'B', has_signals_30d: false, has_signals_90d: true }),
    ]);
    expect(out).toEqual([
      { label: 'A', value: 'a' },
      { label: 'B · 近 30 天无信号', value: 'b' },
    ]);
  });

  it('混合：有信号在前、无信号分组在后，数量写进组标题', () => {
    const out = buildModelSelectOptions([
      m({ model_id: 'sig', display_name: '有信号', has_signals_90d: true }),
      m({ model_id: 'new1', display_name: '新训练一', has_signals_90d: false }),
      m({ model_id: 'new2', display_name: '新训练二', has_signals_90d: false }),
    ]);
    expect(out).toEqual([
      {
        label: '近 90 天有信号（1）',
        options: [{ label: '有信号', value: 'sig' }],
      },
      {
        label: '近 90 天无信号（2）',
        options: [
          { label: '新训练一', value: 'new1' },
          { label: '新训练二', value: 'new2' },
        ],
      },
    ]);
  });

  it('全部无信号：只出无信号分组（注册表模型仍可选）', () => {
    const out = buildModelSelectOptions([
      m({ model_id: 'n1', display_name: 'N1', has_signals_90d: false }),
    ]);
    expect(out).toEqual([
      {
        label: '近 90 天无信号（1）',
        options: [{ label: 'N1', value: 'n1' }],
      },
    ]);
  });

  it('老后端字段缺失：平铺，不臆造分组', () => {
    const out = buildModelSelectOptions([
      m({ model_id: 'a', display_name: 'A' }),
      m({ model_id: 'b', display_name: 'B', has_signals_30d: false }),
    ]);
    expect(out).toEqual([
      { label: 'A', value: 'a' },
      { label: 'B · 近 30 天无信号', value: 'b' },
    ]);
  });

  it('空列表：空数组', () => {
    expect(buildModelSelectOptions([])).toEqual([]);
  });
});

describe('buildTerminalModelOptions（个股终端默认模型下拉）', () => {
  it('默认项 value 为空字符串，不传字面量 default', () => {
    const out = buildTerminalModelOptions([], '默认模型A');
    expect(out).toEqual([{ value: '', label: '默认模型 · 默认模型A' }]);
  });

  it('选项带 T+N、默认标记与无 pred 置灰', () => {
    const out = buildTerminalModelOptions([
      { model_id: 'a', display_name: '模型A', horizon: 20, is_default: true, has_pred: true },
      { model_id: 'b', display_name: '模型B', horizon: 5, has_pred: false },
    ]);
    expect(out[1]).toEqual({ value: 'a', label: '模型A · T+20 · 默认' });
    expect(out[2]).toEqual({
      value: 'b',
      label: '模型B · T+5 · 无 pred',
      disabled: true,
    });
  });

  it('老后端无 horizon/has_pred：不编造，仍可选', () => {
    const out = buildTerminalModelOptions([{ model_id: 'x', display_name: 'X' }]);
    expect(out[1]).toEqual({ value: 'x', label: 'X' });
    expect(out[1].disabled).toBeUndefined();
  });

  it('没有显示名时回退 model_id', () => {
    const out = buildTerminalModelOptions([{ model_id: 'mdl_abc' }]);
    expect(out[1].label).toBe('mdl_abc');
  });

  it('有 pred 时显示精确最后分数日（月-日）', () => {
    const out = buildTerminalModelOptions([
      { model_id: 'a', display_name: 'A', horizon: 20, has_pred: true, pred_last_date: '2026-11-12' },
    ]);
    expect(out[1].label).toBe('A · T+20 · 分数至 11-12');
  });
});
