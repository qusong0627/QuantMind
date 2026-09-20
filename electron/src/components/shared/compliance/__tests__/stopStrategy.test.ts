/**
 * 停止策略二次确认（T-RC-19）：用户硬要求「停止必须二次确认并留痕」。
 *
 * 这里锁的是**文案的事实性**：模拟盘与实盘的停止后果不同（是否撤单、钱在哪），
 * 弹窗必须说准；说错会直接误导用户对自己的仓位判断。
 */

import { describe, it, expect } from 'vitest';
import { buildStopStrategyScenario, STOP_REASONS } from '../dangerAction';

describe('停止策略二次确认（T-RC-19）', () => {
  it('模拟盘：说清不产生新委托、持仓台账保留、可随时重启', () => {
    const s = buildStopStrategyScenario({ mode: 'SIMULATION', strategyName: '动量A' });
    const text = s.consequences.join('\n');
    expect(s.title).toContain('动量A');
    expect(text).toContain('模拟');
    // 三件事必须说清：不新增委托（及在途单处理）/ 持仓保留 / 如何恢复
    expect(text).toMatch(/不再产生|不再新增/);
    expect(text).toMatch(/持仓|台账/);
    expect(text).toMatch(/重新启动|重启/);
    // 模拟盘不涉及撤单，不得出现「券商」
    expect(text).not.toContain('券商');
  });

  it('实盘：说清在途委托不自动撤、持仓留在券商账户、恢复需要重新启动', () => {
    const s = buildStopStrategyScenario({ mode: 'REAL', strategyName: '动量A' });
    const text = s.consequences.join('\n');
    expect(s.title).toContain('实盘');
    // 实盘最容易误解的一点：停止 ≠ 撤单
    expect(text).toMatch(/不自动撤|不会自动撤回/);
    expect(text).toContain('券商');
    expect(text).toMatch(/持仓/);
    expect(text).toMatch(/重新启动|重启/);
  });

  it('模式未知时按模拟口径（与后端 Form 默认值一致，不把模拟说成实盘）', () => {
    const s = buildStopStrategyScenario({ strategyName: 'X' });
    expect(s.consequences.join('\n')).toContain('模拟');
  });

  it('有持仓时给出笔数，无持仓时不编造', () => {
    const withPos = buildStopStrategyScenario({ mode: 'REAL', positionCount: 7 });
    expect(withPos.consequences.join('\n')).toContain('7');
    const noPos = buildStopStrategyScenario({ mode: 'REAL', positionCount: 0 });
    expect(noPos.consequences.join('\n')).toMatch(/无持仓|当前无持仓/);
  });

  it('确认按钮用动词，不是含糊的「确定」；取消按钮不诱导继续运行', () => {
    const s = buildStopStrategyScenario({ mode: 'REAL' });
    expect(s.confirmText).toContain('停止');
    expect(s.cancelText).not.toContain('停止');
    expect(s.confirmText).not.toBe('确定');
  });

  it('停止原因选项覆盖人工/换策略/风控/调试四类，且带稳定 value', () => {
    expect(STOP_REASONS.map((r) => r.value)).toEqual(['manual', 'switch', 'risk', 'debug']);
    for (const r of STOP_REASONS) {
      expect(r.label.length).toBeGreaterThan(0);
    }
  });
});
