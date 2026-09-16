/**
 * FE-F 合规与安全体验测试（T-FE-17 / T-FE-18）：
 * 风险问卷计分与询问策略 / 危险确认判定与文案 / 分散检查 / 留痕 / 确认卡渲染。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  RISK_QUESTIONS,
  evaluateRiskProfile,
  saveRiskProfile,
  loadRiskProfile,
  shouldAskRiskProfile,
  markRiskProfileSkipped,
} from '../riskProfile';
import {
  DANGER_SCENARIOS,
  buildLargeOrderScenario,
  isLargeOrderAmount,
  LARGE_ORDER_THRESHOLD,
  needsTwoStepConfirm,
  scenarioLines,
} from '../dangerAction';
import { checkDiversification } from '../diversification';
import { recordComplianceEvent, listComplianceEvents } from '../complianceLog';
import { DangerConfirmModal } from '../DangerConfirmModal';
import { ComplianceReturn, ComplianceFooter } from '../ComplianceChrome';

beforeEach(() => {
  window.localStorage.clear();
});

describe('风险问卷（T-FE-17 适当性）', () => {
  it('计分分档边界正确：≤2 稳健 / 3-6 平衡 / ≥7 进取', () => {
    expect(evaluateRiskProfile([0, 0, 0, 0, 0])).toEqual({ level: 'conservative', score: 0 });
    expect(evaluateRiskProfile([2, 0, 0, 0, 0]).level).toBe('conservative');
    expect(evaluateRiskProfile([2, 1, 0, 0, 0]).level).toBe('balanced');
    expect(evaluateRiskProfile([2, 2, 2, 0, 0])).toEqual({ level: 'balanced', score: 6 });
    expect(evaluateRiskProfile([2, 2, 2, 1, 0])).toEqual({ level: 'aggressive', score: 7 });
    expect(evaluateRiskProfile([2, 2, 2, 2, 2]).level).toBe('aggressive');
  });

  it('未作答的题按 0 计（null 不报错）', () => {
    expect(evaluateRiskProfile([null, null, null, null, null])).toEqual({
      level: 'conservative',
      score: 0,
    });
    expect(evaluateRiskProfile([2, null, 2, null, 1]).score).toBe(5);
  });

  it('问卷为 5 题且每题 3 个选项', () => {
    expect(RISK_QUESTIONS).toHaveLength(5);
    for (const q of RISK_QUESTIONS) {
      expect(q.options).toHaveLength(3);
      expect(q.text.length).toBeGreaterThan(0);
    }
  });

  it('询问策略：无档案先问；跳过 7 天内沉默；已有档案不问', () => {
    const now = new Date('2026-09-16T10:00:00Z');
    vi.useFakeTimers();
    vi.setSystemTime(now);
    try {
      expect(shouldAskRiskProfile()).toBe(true);

      markRiskProfileSkipped();
      expect(shouldAskRiskProfile()).toBe(false);

      // 3 天后仍在沉默期
      vi.setSystemTime(new Date('2026-09-19T10:00:00Z'));
      expect(shouldAskRiskProfile()).toBe(false);

      // 8 天后重新询问
      vi.setSystemTime(new Date('2026-09-24T10:00:00Z'));
      expect(shouldAskRiskProfile()).toBe(true);

      saveRiskProfile('balanced', 5);
      expect(shouldAskRiskProfile()).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('档案持久化：保存后可读回（含等级与时间）', () => {
    saveRiskProfile('aggressive', 9);
    const loaded = loadRiskProfile();
    expect(loaded?.level).toBe('aggressive');
    expect(loaded?.score).toBe(9);
    expect(loaded?.takenAt).toBeTruthy();
  });
});

describe('危险动作判定与文案（T-FE-18）', () => {
  it('大额阈值：买+卖总额 ≥ 20 万触发', () => {
    expect(isLargeOrderAmount(100000, 50000)).toBe(false);
    expect(isLargeOrderAmount(150000, 50000)).toBe(true);
    expect(isLargeOrderAmount(LARGE_ORDER_THRESHOLD, 0)).toBe(true);
    expect(isLargeOrderAmount(null, undefined)).toBe(false);
    expect(isLargeOrderAmount(-200000, 0)).toBe(true); // 取绝对值，负数不逃逸
  });

  it('大额文案：普通调仓含买卖金额与撤回说明', () => {
    const s = buildLargeOrderScenario({ buyAmount: 300000, sellAmount: 120000 });
    const lines = scenarioLines(s);
    expect(lines[0]).toContain('大额调仓');
    expect(lines.join('')).toContain('300,000');
    expect(lines.join('')).toContain('120,000');
    expect(lines.join('')).toContain('撤单');
  });

  it('大额文案：纯卖出按清仓风险提醒', () => {
    const s = buildLargeOrderScenario({ buyAmount: 0, sellAmount: 500000 });
    const text = scenarioLines(s).join('');
    expect(s.title).toContain('卖出');
    expect(text).toContain('清仓');
    expect(text).toContain('仓位将归零');
  });

  it('四类危险场景均有后果文案（上实盘/关风控/一键执行）', () => {
    for (const key of ['switch_real', 'disable_stop_loss', 'bulk_execute'] as const) {
      const s = DANGER_SCENARIOS[key];
      expect(s.title.length).toBeGreaterThan(0);
      expect(s.consequences.length).toBeGreaterThanOrEqual(3);
      expect(s.confirmText.length).toBeGreaterThan(0);
    }
    // 上实盘文案必须点明真实资金
    expect(scenarioLines(DANGER_SCENARIOS.switch_real).join('')).toContain('真实资金');
  });

  it('needsTwoStepConfirm 判定写类动作（quantbotIntents 兼容再导出）', () => {
    expect(needsTwoStepConfirm({ label: '查看详情' })).toBe(false);
    expect(needsTwoStepConfirm({ label: '清仓' })).toBe(true);
    expect(needsTwoStepConfirm({ label: '任意动作', variant: 'danger' })).toBe(true);
  });
});

describe('强制分散提示（T-FE-18）', () => {
  it('单票超 15% 触发警告并给出占比', () => {
    const report = checkDiversification([
      { symbol: '600000', side: 'BUY', estimated_amount: 40000 },
      { symbol: '600001', side: 'BUY', estimated_amount: 30000 },
      { symbol: '600002', side: 'BUY', estimated_amount: 30000 },
    ]);
    expect(report.ok).toBe(false);
    expect(report.maxWeightSymbol).toBe('600000');
    expect(report.maxWeight).toBeCloseTo(0.4, 5);
    expect(report.warnings.some((w) => w.includes('40.0%'))).toBe(true);
  });

  it('不足 3 只提示分散不足', () => {
    const report = checkDiversification([
      { symbol: '600000', side: 'BUY', estimated_amount: 5000 },
      { symbol: '600001', side: 'BUY', estimated_amount: 5000 },
    ]);
    expect(report.buyCount).toBe(2);
    expect(report.warnings.some((w) => w.includes('仅买入 2 只'))).toBe(true);
  });

  it('同标的买单合并计算后按权重判定', () => {
    const report = checkDiversification([
      { symbol: '600000', side: 'BUY', estimated_amount: 2500 },
      { symbol: '600000', side: 'BUY', estimated_amount: 2500 },
      { symbol: '600001', side: 'BUY', estimated_amount: 5000 },
      { symbol: '600002', side: 'BUY', estimated_amount: 5000 },
    ]);
    expect(report.buyCount).toBe(3);
    expect(report.totalBuyAmount).toBe(15000);
    expect(report.maxWeight).toBeCloseTo(5000 / 15000, 5);
    expect(report.warnings.some((w) => w.includes('600000'))).toBe(true);
  });

  it('卖出单不计入分散口径；无买单视为通过', () => {
    const report = checkDiversification([
      { symbol: '600000', side: 'SELL', estimated_amount: 999999 },
    ]);
    expect(report.ok).toBe(true);
    expect(report.buyCount).toBe(0);
    expect(report.maxWeight).toBeNull();
  });

  it('分布合理时全部通过', () => {
    const report = checkDiversification(
      Array.from({ length: 8 }, (_, i) => ({
        symbol: `60${String(i).padStart(4, '0')}`,
        side: 'BUY',
        estimated_amount: 10000,
      }))
    );
    expect(report.ok).toBe(true);
    expect(report.warnings).toHaveLength(0);
    expect(report.maxWeight).toBeCloseTo(0.125, 5);
  });
});

describe('合规留痕（T-FE-17）', () => {
  it('记录与读取：按序追加，含时间戳', () => {
    recordComplianceEvent('danger_confirmed', '切换到实盘模式');
    recordComplianceEvent('risk_profile_taken', '平衡型（score=5）');
    const events = listComplianceEvents();
    expect(events).toHaveLength(2);
    expect(events[0].kind).toBe('danger_confirmed');
    expect(events[0].detail).toContain('实盘');
    expect(new Date(events[1].at).getTime()).not.toBeNaN();
  });

  it('环形上限 200 条（最旧的被挤出）', () => {
    for (let i = 0; i < 205; i++) {
      recordComplianceEvent('danger_cancelled', `事件${i}`);
    }
    const events = listComplianceEvents();
    expect(events).toHaveLength(200);
    expect(events[0].detail).toBe('事件5');
    expect(events[199].detail).toBe('事件204');
  });
});

describe('危险确认卡渲染（T-FE-18）', () => {
  it('未确认前回调不触发；确认后 onConfirm 触发且留痕', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <DangerConfirmModal
        open
        scenario={DANGER_SCENARIOS.switch_real}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    expect(screen.getByText('切换到实盘模式')).toBeTruthy();
    expect(screen.getByText(/真实资金/)).toBeTruthy();
    expect(onConfirm).not.toHaveBeenCalled(); // 打开本身绝不触发动作

    await user.click(screen.getByRole('button', { name: /我已知悉/ }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(listComplianceEvents().some((e) => e.kind === 'danger_confirmed')).toBe(true);
  });

  it('取消走 onCancel 并留痕', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <DangerConfirmModal
        open
        scenario={buildLargeOrderScenario({ buyAmount: 0, sellAmount: 500000 })}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    await user.click(screen.getByRole('button', { name: /回去再核对/ }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(listComplianceEvents().some((e) => e.kind === 'danger_cancelled')).toBe(true);
  });
});

describe('收益展示规范（T-FE-17）', () => {
  it('百分比数字附区间与"过往不代表未来"', () => {
    render(<ComplianceReturn value={0.1234} windowText="2026-09-16 快照" />);
    expect(screen.getByText(/12\.34%/)).toBeTruthy();
    expect(screen.getByText(/2026-09-16 快照/)).toBeTruthy();
    expect(screen.getByText(/过往不代表未来/)).toBeTruthy();
  });

  it('空值与 NaN 显示为占位符而非假数字', () => {
    render(<ComplianceReturn value={null} />);
    expect(screen.getByText('—')).toBeTruthy();
  });

  it('免责页脚包含资质边界与风险提示', () => {
    render(<ComplianceFooter />);
    expect(screen.getByText(/不构成投资建议/)).toBeTruthy();
    expect(screen.getByText(/股市有风险/)).toBeTruthy();
    expect(screen.getByText(/不代表未来收益/)).toBeTruthy();
  });
});
