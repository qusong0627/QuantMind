import { describe, it, expect } from 'vitest';
import {
  isMinibtStrategy,
  isMinibtStrategyCode,
  isMinibtStrategyType,
} from '../minibt';

describe('isMinibtStrategyType', () => {
  it('应该识别 minibt 策略库元数据标记', () => {
    expect(isMinibtStrategyType('minibt_dual_ma')).toBe(true);
    expect(isMinibtStrategyType('MINIBT_RSI_REVERSAL')).toBe(true);
  });

  it('不应把 qlib/普通策略误判为 minibt', () => {
    expect(isMinibtStrategyType('CUSTOM')).toBe(false);
    expect(isMinibtStrategyType('long_short_topk')).toBe(false);
    expect(isMinibtStrategyType('')).toBe(false);
    expect(isMinibtStrategyType(undefined)).toBe(false);
  });
});

describe('isMinibtStrategyCode', () => {
  it('应该识别真实的 minibt import', () => {
    expect(isMinibtStrategyCode('import minibt\n')).toBe(true);
    expect(isMinibtStrategyCode('from minibt import Bt, Strategy\n')).toBe(true);
    // 缩进在函数体内同样命中
    expect(isMinibtStrategyCode('def f():\n    from minibt import Bt\n')).toBe(true);
  });

  it('不应把注释/字符串里的提及当成 minibt 依赖', () => {
    expect(isMinibtStrategyCode('# 本策略不是 minibt 框架\nclass S:\n    pass\n')).toBe(false);
    expect(isMinibtStrategyCode("NOTE = 'from minibt import Bt'\n")).toBe(false);
  });

  it('不应把 minibt_ 前缀的同名后端模块当成 minibt 框架', () => {
    expect(
      isMinibtStrategyCode('from backend.shared.minibt_qdb import load_daily\n'),
    ).toBe(false);
    expect(isMinibtStrategyCode('import minibt_qdb\n')).toBe(false);
  });

  it('空代码应返回 false', () => {
    expect(isMinibtStrategyCode('')).toBe(false);
    expect(isMinibtStrategyCode(undefined)).toBe(false);
  });
});

describe('isMinibtStrategy', () => {
  it('元数据标记即命中（列表接口不返回 code 的场景）', () => {
    expect(
      isMinibtStrategy({ code: '', parameters: { strategy_type: 'minibt_keltner' } }),
    ).toBe(true);
  });

  it('代码 import 即命中（历史数据缺元数据的场景）', () => {
    expect(isMinibtStrategy({ code: 'from minibt import Bt\n' })).toBe(true);
  });

  it('普通 qlib 策略不命中', () => {
    expect(
      isMinibtStrategy({
        code: 'from qlib.strategy.base import BaseStrategy\n',
        parameters: { strategy_type: 'CUSTOM' },
      }),
    ).toBe(false);
  });

  it('空对象/空值不命中', () => {
    expect(isMinibtStrategy({})).toBe(false);
    expect(isMinibtStrategy(null)).toBe(false);
    expect(isMinibtStrategy(undefined)).toBe(false);
  });
});
