import { describe, expect, it } from 'vitest';
import {
  QUANTBOT_INTENTS,
  confirmConsequence,
  needsTwoStepConfirm,
} from '../quantbotIntents';

describe('四类意图模型（T-FE-13）', () => {
  it('恰好四类意图，key 唯一，每类 ≥3 个示例且文案非空', () => {
    expect(QUANTBOT_INTENTS.map((i) => i.key)).toEqual([
      'write_strategy',
      'screen_stocks',
      'analyze',
      'help',
    ]);
    for (const intent of QUANTBOT_INTENTS) {
      expect(intent.label.length).toBeGreaterThan(0);
      expect(intent.description.length).toBeGreaterThan(0);
      expect(intent.examples.length).toBeGreaterThanOrEqual(3);
      for (const ex of intent.examples) {
        expect(ex.label.length).toBeGreaterThan(0);
        expect(ex.prompt.length).toBeGreaterThan(10);
      }
    }
  });
});

describe('write 两步确认判定（六铁律：永不直达写操作）', () => {
  it('danger 变体与写类词命中；只读动作不打扰', () => {
    expect(needsTwoStepConfirm({ label: '查看详情', variant: 'default' })).toBe(false);
    expect(needsTwoStepConfirm({ label: '立即执行', variant: 'primary' })).toBe(true);
    expect(needsTwoStepConfirm({ label: '一键下单' })).toBe(true);
    expect(needsTwoStepConfirm({ label: '清仓', variant: 'danger' })).toBe(true);
    expect(needsTwoStepConfirm({ label: '停止任务' })).toBe(true);
    expect(needsTwoStepConfirm(null)).toBe(false);
    expect(needsTwoStepConfirm(undefined)).toBe(false);
  });

  it('后果文案包含动作名与不可逆提示', () => {
    const text = confirmConsequence('立即执行');
    expect(text).toContain('立即执行');
    expect(text).toContain('不可逆');
  });
});
