/**
 * 模型资产列表的本地偏好：置顶 / 收藏 / 排序 / 文件夹。
 *
 * 模型一多，左栏列表就需要「把常用模型钉在最上面、把好用的标出来、按需重排」，
 * 并把模型收进文件夹分组。偏好按市场隔离（各市场模型集合不同，ID 空间也不同）；
 * 读写坏数据一律当空、写失败静默——偏好是便利功能，不该因为一条手改过的
 * localStorage 把整页打崩。
 */
import type { UserModelRecord } from '../services/modelTrainingService';
import {
  getMeta,
  getMetrics,
  modelDisplayName,
  resolveMetricNumber,
} from './modelRegistryUtils';
import {
  normalizeFolders,
  normalizeModelFolders,
  type FolderState,
  type ModelFolder,
} from './modelRegistryFolders';

export type ModelSortKey =
  | 'default'
  | 'created_desc'
  | 'created_asc'
  | 'name_asc'
  | 'test_ic_desc'
  | 'horizon_asc';

export interface ModelListPrefs extends FolderState {
  /** 置顶模型 ID，数组顺序即展示顺序（最近置顶的排最前）。 */
  pinned: string[];
  /** 收藏模型 ID，仅作标记与「收藏」页签筛选，不参与排序。 */
  favorites: string[];
  sortKey: ModelSortKey;
}

export interface ModelSortOption {
  key: ModelSortKey;
  /** 下拉菜单里的完整文案。 */
  label: string;
  /** 左栏窄按钮上的短文案。 */
  shortLabel: string;
}

export const MODEL_SORT_OPTIONS: ModelSortOption[] = [
  { key: 'default', label: '默认排序（置顶优先）', shortLabel: '默认' },
  { key: 'created_desc', label: '创建时间 新→旧', shortLabel: '最新' },
  { key: 'created_asc', label: '创建时间 旧→新', shortLabel: '最早' },
  { key: 'name_asc', label: '名称 A→Z', shortLabel: '名称' },
  { key: 'test_ic_desc', label: '测试 IC 高→低', shortLabel: 'IC' },
  { key: 'horizon_asc', label: '预测周期 短→长', shortLabel: '周期' },
];

const SORT_KEYS: readonly ModelSortKey[] = MODEL_SORT_OPTIONS.map((o) => o.key);
const STORAGE_PREFIX = 'qm.model-registry.prefs.v1';

/** 后端/旧缓存传来的排序键一律过白名单，非法值退 default。 */
export function normalizeModelSortKey(value: unknown): ModelSortKey {
  return SORT_KEYS.includes(value as ModelSortKey) ? (value as ModelSortKey) : 'default';
}

export const DEFAULT_MODEL_LIST_PREFS: ModelListPrefs = {
  pinned: [],
  favorites: [],
  sortKey: 'default',
  folders: [],
  modelFolders: {},
};

/** 偏好键（读写共用；市场必须进键，避免跨市场串号）。 */
export function modelListPrefsKey(market: string): string {
  return `${STORAGE_PREFIX}:${String(market || 'CN').toUpperCase()}`;
}

function toStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(value.filter((item): item is string => typeof item === 'string' && item !== '')),
  );
}

export function loadModelListPrefs(market: string): ModelListPrefs {
  try {
    const raw = localStorage.getItem(modelListPrefsKey(market));
    if (!raw) return { ...DEFAULT_MODEL_LIST_PREFS };
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object') return { ...DEFAULT_MODEL_LIST_PREFS };
    const record = parsed as Record<string, unknown>;
    const sortKey = normalizeModelSortKey(record.sortKey);
    const folders = normalizeFolders(record.folders);
    return {
      pinned: toStringList(record.pinned),
      favorites: toStringList(record.favorites),
      sortKey,
      folders,
      modelFolders: normalizeModelFolders(record.modelFolders, folders),
    };
  } catch {
    return { ...DEFAULT_MODEL_LIST_PREFS };
  }
}

export function saveModelListPrefs(market: string, prefs: ModelListPrefs): void {
  try {
    localStorage.setItem(modelListPrefsKey(market), JSON.stringify(prefs));
  } catch {
    /* 配额满 / 隐私模式：忽略 */
  }
}

/** 服务端（snake_case）→ 前端（camelCase）；坏数据一律降级为空。 */
export function prefsFromServer(remote: {
  pinned?: unknown;
  favorites?: unknown;
  sort_key?: unknown;
  folders?: unknown;
  model_folders?: unknown;
}): ModelListPrefs {
  const folders: ModelFolder[] = normalizeFolders(remote?.folders);
  return {
    pinned: toStringList(remote?.pinned),
    favorites: toStringList(remote?.favorites),
    sortKey: normalizeModelSortKey(remote?.sort_key),
    folders,
    modelFolders: normalizeModelFolders(remote?.model_folders, folders),
  };
}

/** 前端偏好 → 服务端载荷（camelCase → snake_case）。 */
export function prefsToServer(prefs: ModelListPrefs): {
  pinned: string[];
  favorites: string[];
  sort_key: ModelSortKey;
  folders: ModelFolder[];
  model_folders: Record<string, string>;
} {
  return {
    pinned: prefs.pinned,
    favorites: prefs.favorites,
    sort_key: prefs.sortKey,
    folders: prefs.folders,
    model_folders: prefs.modelFolders,
  };
}

/** 切换集合中的 ID（纯函数，不改原数组）；prepend=true 用于置顶，最近置顶的排最前。 */
export function toggleId(arr: string[], id: string, prepend = false): string[] {
  if (arr.includes(id)) return arr.filter((item) => item !== id);
  return prepend ? [id, ...arr] : [...arr, id];
}

function timestampOf(value?: string | null): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

function horizonOf(m: UserModelRecord): number | null {
  const meta = getMeta(m);
  const raw = meta.target_horizon_days ?? meta.horizon_days;
  const num = Number(raw);
  return Number.isFinite(num) && num > 0 ? num : null;
}

function testIcOf(m: UserModelRecord): number | null {
  return resolveMetricNumber(getMetrics(m), ['test_ic', 'test_rank_ic', 'test_mean_ic']);
}

/** 缺失值一律排最后（不当作 0）；dir=1 升序，dir=-1 降序。 */
function compareNullable(a: number | null, b: number | null, dir: 1 | -1): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return (a - b) * dir;
}

function createdAtOf(m: UserModelRecord): number | null {
  return timestampOf(m.created_at);
}

function compareBySortKey(a: UserModelRecord, b: UserModelRecord, sortKey: ModelSortKey): number {
  switch (sortKey) {
    case 'created_desc':
      return compareNullable(createdAtOf(a), createdAtOf(b), -1);
    case 'created_asc':
      return compareNullable(createdAtOf(a), createdAtOf(b), 1);
    case 'name_asc':
      return modelDisplayName(a).localeCompare(modelDisplayName(b), 'zh-Hans-CN');
    case 'test_ic_desc':
      return compareNullable(testIcOf(a), testIcOf(b), -1);
    case 'horizon_asc':
      return compareNullable(horizonOf(a), horizonOf(b), 1);
    case 'default':
    default:
      return 0;
  }
}

/**
 * 置顶模型恒在最前（按置顶顺序），其余模型按所选排序键排列。
 * Array.prototype.sort 是稳定排序：`default` 保留后端返回的原始顺序。
 */
export function sortModelRecords(
  models: UserModelRecord[],
  options: { pinned?: string[]; sortKey?: ModelSortKey } = {},
): UserModelRecord[] {
  const pinned = options.pinned ?? [];
  const sortKey = options.sortKey ?? 'default';
  if (pinned.length === 0 && sortKey === 'default') return [...models];

  const pinnedRank = new Map(pinned.map((id, index) => [id, index]));
  const pinnedModels = models
    .filter((m) => pinnedRank.has(m.model_id))
    .sort((a, b) => pinnedRank.get(a.model_id)! - pinnedRank.get(b.model_id)!);
  const rest = models
    .filter((m) => !pinnedRank.has(m.model_id))
    .sort((a, b) => compareBySortKey(a, b, sortKey));

  return [...pinnedModels, ...rest];
}
