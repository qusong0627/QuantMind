/**
 * 模型资产「文件夹」：模型多起来后按用途 / 周期 / 市场归档。
 *
 * 存储挂在模型列表偏好（每 用户 × 市场 一份）里：
 *   folders      —— 文件夹定义，数组顺序即左栏展示顺序
 *   modelFolders —— 模型 → 文件夹的单归属映射（一个模型最多在一个文件夹）
 *
 * 本模块只放纯函数（清洗 / 增删改 / 筛选 / 计数），页面负责渲染与持久化。
 * 后端 `_normalize_model_list_prefs` 有同口径清洗，两侧上限保持一致。
 */
import type { UserModelRecord } from '../services/modelTrainingService';

export interface ModelFolder {
  id: string;
  name: string;
}

/** 左栏「全部模型」用 null；「未分组」用哨兵值，避免和真实文件夹 ID 撞键。 */
export const UNFILED_FOLDER_KEY = '__unfiled__';
export const FOLDER_NAME_MAX_LENGTH = 40;
export const MAX_FOLDERS = 200;
export const MAX_FOLDER_ASSIGNMENTS = 2000;
const FOLDER_ID_MAX_LENGTH = 64;
const MODEL_ID_MAX_LENGTH = 128;
const FOLDER_ID_PREFIX = 'mf_';

export interface FolderState {
  folders: ModelFolder[];
  modelFolders: Record<string, string>;
}

export const EMPTY_FOLDER_STATE: FolderState = { folders: [], modelFolders: {} };

/** 清洗文件夹定义：trim、按 id 去重、长度上限、数量截断（与后端一致）。 */
export function normalizeFolders(value: unknown): ModelFolder[] {
  if (!Array.isArray(value)) return [];
  const out: ModelFolder[] = [];
  const seenIds = new Set<string>();
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') continue;
    const record = raw as Record<string, unknown>;
    const id = typeof record.id === 'string' ? record.id.trim() : '';
    const name = typeof record.name === 'string' ? record.name.trim() : '';
    if (!id || !name) continue;
    if (id.length > FOLDER_ID_MAX_LENGTH || name.length > FOLDER_NAME_MAX_LENGTH) continue;
    if (seenIds.has(id)) continue;
    seenIds.add(id);
    out.push({ id, name });
    if (out.length >= MAX_FOLDERS) break;
  }
  return out;
}

/** 清洗模型→文件夹分配：只保留指向已存在文件夹的条目（丢弃悬空分配）。 */
export function normalizeModelFolders(
  value: unknown,
  folders: ModelFolder[],
): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const validIds = new Set(folders.map((folder) => folder.id));
  const out: Record<string, string> = {};
  let count = 0;
  for (const [rawModelId, rawFolderId] of Object.entries(
    value as Record<string, unknown>,
  )) {
    if (typeof rawFolderId !== 'string') continue;
    const modelId = rawModelId.trim();
    const folderId = rawFolderId.trim();
    if (!modelId || modelId.length > MODEL_ID_MAX_LENGTH) continue;
    if (!validIds.has(folderId)) continue;
    out[modelId] = folderId;
    count += 1;
    if (count >= MAX_FOLDER_ASSIGNMENTS) break;
  }
  return out;
}

/** 生成文件夹 ID：优先 randomUUID，环境不支持时退化为时间戳 + 随机串。 */
export function generateFolderId(existing: ModelFolder[] = []): string {
  const existingIds = new Set(existing.map((folder) => folder.id));
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) {
    const id = `${FOLDER_ID_PREFIX}${uuid.replace(/-/g, '')}`;
    if (!existingIds.has(id)) return id;
  }
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const random = Math.random().toString(36).slice(2, 10);
    const id = `${FOLDER_ID_PREFIX}${Date.now().toString(36)}_${random}`;
    if (!existingIds.has(id)) return id;
  }
  return `${FOLDER_ID_PREFIX}${Date.now().toString(36)}_${existing.length}`;
}

/** 文件夹名合法性：非空、≤40 字、同名拒绝（忽略大小写与首尾空格，重命名时排除自身）。 */
export function validateFolderName(
  name: string,
  folders: ModelFolder[],
  exceptId?: string,
): string | null {
  const trimmed = name.trim();
  if (!trimmed) return '文件夹名不能为空';
  if (trimmed.length > FOLDER_NAME_MAX_LENGTH) {
    return `文件夹名过长（≤${FOLDER_NAME_MAX_LENGTH} 字符）`;
  }
  const key = trimmed.toLowerCase();
  const duplicate = folders.some(
    (folder) => folder.id !== exceptId && folder.name.trim().toLowerCase() === key,
  );
  return duplicate ? '已存在同名文件夹' : null;
}

// 增删改都用泛型保留调用方的完整偏好类型（ModelListPrefs extends FolderState）。
export function addFolder<T extends FolderState>(state: T, folder: ModelFolder): T {
  const id = folder.id.trim();
  const name = folder.name.trim();
  if (!id || !name) return state;
  if (state.folders.some((item) => item.id === id)) return state;
  return { ...state, folders: [...state.folders, { id, name }] };
}

export function renameFolder<T extends FolderState>(
  state: T,
  folderId: string,
  name: string,
): T {
  const trimmed = name.trim();
  if (!trimmed) return state;
  if (!state.folders.some((folder) => folder.id === folderId)) return state;
  return {
    ...state,
    folders: state.folders.map((folder) =>
      folder.id === folderId ? { ...folder, name: trimmed } : folder,
    ),
  };
}

/** 删除文件夹；其中模型回到「未分组」，模型本身不动。 */
export function deleteFolder<T extends FolderState>(state: T, folderId: string): T {
  if (!state.folders.some((folder) => folder.id === folderId)) return state;
  const modelFolders: Record<string, string> = {};
  for (const [modelId, assigned] of Object.entries(state.modelFolders)) {
    if (assigned !== folderId) modelFolders[modelId] = assigned;
  }
  return {
    ...state,
    folders: state.folders.filter((folder) => folder.id !== folderId),
    modelFolders,
  };
}

/** 批量分配；folderId=null 表示移出分组。未知文件夹视为无效，原状态返回。 */
export function assignModelsToFolder<T extends FolderState>(
  state: T,
  modelIds: string[],
  folderId: string | null,
): T {
  if (folderId !== null && !state.folders.some((folder) => folder.id === folderId)) {
    return state;
  }
  const modelFolders = { ...state.modelFolders };
  for (const rawId of modelIds) {
    const modelId = typeof rawId === 'string' ? rawId.trim() : '';
    if (!modelId) continue;
    if (folderId === null) delete modelFolders[modelId];
    else modelFolders[modelId] = folderId;
  }
  return { ...state, modelFolders };
}

function isKnownFolder(state: FolderState, folderId: string | undefined): boolean {
  return Boolean(folderId) && state.folders.some((folder) => folder.id === folderId);
}

/** 模型所属文件夹；未分配或悬空分配（文件夹已删）返回 null。 */
export function folderOfModel(
  modelId: string,
  state: FolderState,
): ModelFolder | null {
  const folderId = state.modelFolders[modelId];
  if (!isKnownFolder(state, folderId)) return null;
  return state.folders.find((folder) => folder.id === folderId) ?? null;
}

/**
 * 按左栏选择筛选：null=全部；UNFILED_FOLDER_KEY=未分组（含悬空分配）；
 * 其余为文件夹 ID，文件夹已不存在时返回空列表。
 */
export function filterModelsByFolder<T extends Pick<UserModelRecord, 'model_id'>>(
  models: T[],
  filter: string | null,
  state: FolderState,
): T[] {
  if (filter === null) return [...models];
  if (filter === UNFILED_FOLDER_KEY) {
    return models.filter((model) => folderOfModel(model.model_id, state) === null);
  }
  if (!isKnownFolder(state, filter)) return [];
  return models.filter((model) => state.modelFolders[model.model_id] === filter);
}

export interface FolderCounts {
  total: number;
  unfiled: number;
  byFolder: Record<string, number>;
}

/** 左栏计数；口径与 filterModelsByFolder 一致（悬空分配计入未分组）。 */
export function countModelsByFolder<T extends Pick<UserModelRecord, 'model_id'>>(
  models: T[],
  state: FolderState,
): FolderCounts {
  const byFolder: Record<string, number> = {};
  for (const folder of state.folders) byFolder[folder.id] = 0;
  let unfiled = 0;
  for (const model of models) {
    const folder = folderOfModel(model.model_id, state);
    if (!folder) unfiled += 1;
    else byFolder[folder.id] = (byFolder[folder.id] ?? 0) + 1;
  }
  return { total: models.length, unfiled, byFolder };
}
