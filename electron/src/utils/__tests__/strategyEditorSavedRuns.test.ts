import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  clearSavedRuns,
  deleteSavedRun,
  loadSavedRuns,
  saveBacktestRun,
} from '../strategyEditorSavedRuns';

function installFakeStorage() {
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
    removeItem: (key: string) => {
      data.delete(key);
    },
    clear: () => data.clear(),
    key: (index: number) => Array.from(data.keys())[index] ?? null,
    get length() {
      return data.size;
    },
  };
  vi.stubGlobal('localStorage', storage);
  return storage;
}

describe('strategyEditorSavedRuns', () => {
  beforeEach(() => {
    installFakeStorage();
    clearSavedRuns();
  });

  it('保存后可按时间倒序加载，并保留 code/params', () => {
    saveBacktestRun({ name: 'A', kind: 'minibt', result: { equity: [] } });
    const runs = saveBacktestRun({
      name: 'B',
      kind: 'qlib',
      result: { backtest_id: 'x' },
      code: 'print(1)',
      params: {
        symbol: '600036',
        startDate: '2024-01-01',
        endDate: '2026-10-26',
        initialCapital: 1000000,
        universe: 'all',
        commission: 0.00025,
        topk: 50,
        nDrop: 10,
        rebalanceDays: 3,
        size: 1000,
        fast: 10,
        slow: 20,
      },
    });
    expect(runs).toHaveLength(2);
    expect(runs[0].name).toBe('B');
    expect(runs[0].code).toBe('print(1)');
    expect(loadSavedRuns()[0].kind).toBe('qlib');
  });

  it('删除只移除指定 id', () => {
    const runs = saveBacktestRun({ name: 'A', kind: 'minibt', result: {} });
    expect(deleteSavedRun(runs[0].id)).toHaveLength(0);
  });

  it('损坏的本地数据返回空数组，不抛异常', () => {
    localStorage.setItem('qm:strategy-editor:saved-backtests', '{bad json');
    expect(loadSavedRuns()).toEqual([]);
  });
});
