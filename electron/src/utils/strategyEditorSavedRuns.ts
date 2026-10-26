/**
 * 策略编辑「保存本次回测」的本地存档（localStorage）。
 *
 * 说明：这是**研究态**的本地快照，不替代 `qlib_backtest_runs` 台账；
 * 正式回测历史仍由后端持久化。这里保存的是前端可直接回放的结果对象
 * （minibt 的 equity/trades 或 qlib 的完整 result），最多保留 20 条。
 */
import type { StrategyEditorBacktestParams } from './strategyEditorParams';

export type SavedBacktestKind = 'minibt' | 'qlib';

export interface SavedBacktestRun {
  id: string;
  name: string;
  savedAt: string;
  kind: SavedBacktestKind;
  result: unknown;
  code?: string;
  params?: StrategyEditorBacktestParams;
}

const STORAGE_KEY = 'qm:strategy-editor:saved-backtests';
const MAX_RUNS = 20;

function storage(): Storage | null {
  try {
    if (typeof localStorage === 'undefined') return null;
    return localStorage;
  } catch {
    return null;
  }
}

export function loadSavedRuns(): SavedBacktestRun[] {
  const store = storage();
  if (!store) return [];
  try {
    const raw = store.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (item): item is SavedBacktestRun =>
        Boolean(item) &&
        typeof item === 'object' &&
        typeof (item as SavedBacktestRun).id === 'string' &&
        typeof (item as SavedBacktestRun).name === 'string' &&
        ((item as SavedBacktestRun).kind === 'minibt' ||
          (item as SavedBacktestRun).kind === 'qlib'),
    );
  } catch {
    return [];
  }
}

function persist(runs: SavedBacktestRun[]): void {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(STORAGE_KEY, JSON.stringify(runs.slice(0, MAX_RUNS)));
  } catch {
    // 容量/隐私模式等失败不阻断回测展示
  }
}

export function saveBacktestRun(input: {
  name: string;
  kind: SavedBacktestKind;
  result: unknown;
  code?: string;
  params?: StrategyEditorBacktestParams;
}): SavedBacktestRun[] {
  const runs = loadSavedRuns();
  const run: SavedBacktestRun = {
    id: `bt_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    name: String(input.name || '未命名回测').trim() || '未命名回测',
    savedAt: new Date().toISOString(),
    kind: input.kind,
    result: input.result,
    code: input.code,
    params: input.params,
  };
  const next = [run, ...runs].slice(0, MAX_RUNS);
  persist(next);
  return next;
}

export function deleteSavedRun(id: string): SavedBacktestRun[] {
  const next = loadSavedRuns().filter((run) => run.id !== id);
  persist(next);
  return next;
}

export function clearSavedRuns(): SavedBacktestRun[] {
  const store = storage();
  if (store) {
    try {
      store.removeItem(STORAGE_KEY);
    } catch {
      // ignore
    }
  }
  return [];
}
