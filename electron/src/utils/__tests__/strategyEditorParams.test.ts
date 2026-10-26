import { describe, expect, it } from 'vitest';
import {
  DEFAULT_STRATEGY_EDITOR_PARAMS,
  applyBacktestParamsToCode,
  isMinibtStrategyCode,
} from '../strategyEditorParams';

describe('strategyEditorParams', () => {
  it('识别 minibt 代码只认真实 import，不误判注释与同前缀模块', () => {
    expect(isMinibtStrategyCode('from minibt import Bt\n')).toBe(true);
    expect(isMinibtStrategyCode('import minibt\n')).toBe(true);
    expect(isMinibtStrategyCode('# import minibt\n')).toBe(false);
    expect(isMinibtStrategyCode('import minibt_qdb\n')).toBe(false);
  });

  it('minibt：改写顶层常量与 params 快慢线', () => {
    const code = [
      'from minibt import Bt, Strategy',
      "SYMBOL = '600036'",
      "START = '20240101'",
      "END = '20250630'",
      'SIZE = 1000',
      'COMMISSION = 0.00025',
      '',
      'class DualMA(Strategy):',
      '    params = dict(fast=10, slow=20)',
      '',
    ].join('\n');
    const out = applyBacktestParamsToCode(code, {
      ...DEFAULT_STRATEGY_EDITOR_PARAMS,
      symbol: '000001',
      startDate: '2025-01-02',
      endDate: '2025-06-30',
      size: 200,
      commission: 0.0003,
      fast: 5,
      slow: 30,
    });
    expect(out).toContain("SYMBOL = '000001'");
    expect(out).toContain("START = '20250102'");
    expect(out).toContain("END = '20250630'");
    expect(out).toContain('SIZE = 200');
    expect(out).toContain('COMMISSION = 0.0003');
    expect(out).toContain('params = dict(fast=5, slow=30)');
  });

  it('minibt：支持 dict 字面量 params', () => {
    const code = "from minibt import Strategy\nparams = {'fast': 10, 'slow': 20}\n";
    const out = applyBacktestParamsToCode(code, {
      ...DEFAULT_STRATEGY_EDITOR_PARAMS,
      fast: 8,
      slow: 34,
    });
    expect(out).toContain("'fast': 8");
    expect(out).toContain("'slow': 34");
  });

  it('STRATEGY_CONFIG：只改已存在的 topk/n_drop/rebalance_days', () => {
    const code = [
      'STRATEGY_CONFIG = {',
      '  "class": "RedisTopkStrategy",',
      '  "kwargs": {"topk": 50, "n_drop": 10, "rebalance_days": 3},',
      '}',
    ].join('\n');
    const out = applyBacktestParamsToCode(code, {
      ...DEFAULT_STRATEGY_EDITOR_PARAMS,
      topk: 30,
      nDrop: 6,
      rebalanceDays: 5,
    });
    expect(out).toContain('"topk": 30');
    expect(out).toContain('"n_drop": 6');
    expect(out).toContain('"rebalance_days": 5');
  });

  it('普通脚本原样返回，不误改', () => {
    const code = 'def main():\n    print("hello")\n';
    expect(applyBacktestParamsToCode(code, DEFAULT_STRATEGY_EDITOR_PARAMS)).toBe(code);
  });
});
