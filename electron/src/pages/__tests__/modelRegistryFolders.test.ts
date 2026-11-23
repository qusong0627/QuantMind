import { describe, expect, test } from 'vitest';
import type { UserModelRecord } from '../../services/modelTrainingService';
import {
  FOLDER_NAME_MAX_LENGTH,
  MAX_FOLDER_ASSIGNMENTS,
  MAX_FOLDERS,
  UNFILED_FOLDER_KEY,
  addFolder,
  assignModelsToFolder,
  countModelsByFolder,
  deleteFolder,
  filterModelsByFolder,
  folderOfModel,
  normalizeFolders,
  normalizeModelFolders,
  renameFolder,
  validateFolderName,
  type FolderState,
} from '../modelRegistryFolders';

function model(id: string): UserModelRecord {
  return {
    tenant_id: 'default',
    user_id: '1',
    model_id: id,
    source_run_id: '',
    status: 'ready',
    storage_path: '',
    model_file: '',
    metadata_json: { display_name: id },
    metrics_json: {},
    is_default: false,
  };
}

const state = (
  folders: Array<{ id: string; name: string }> = [],
  modelFolders: Record<string, string> = {},
): FolderState => ({ folders, modelFolders });

const ids = (list: UserModelRecord[]) => list.map((m) => m.model_id);

describe('文件夹定义清洗', () => {
  test('trim、按 id 去重、丢弃坏形状与超长字段', () => {
    expect(
      normalizeFolders([
        { id: ' f1 ', name: ' T+5 ' },
        { id: 'f1', name: '重复 ID' },
        { id: '', name: '空 ID' },
        { id: 'f2', name: '   ' },
        { id: 'f3' },
        'not-a-dict',
        { id: 'f'.repeat(65), name: 'ID 过长' },
        { id: 'f4', name: '名'.repeat(FOLDER_NAME_MAX_LENGTH + 1) },
      ]),
    ).toEqual([{ id: 'f1', name: 'T+5' }]);
    expect(normalizeFolders('nope')).toEqual([]);
    expect(normalizeFolders(null)).toEqual([]);
  });

  test('数量上限', () => {
    const out = normalizeFolders(
      Array.from({ length: MAX_FOLDERS + 50 }, (_, i) => ({
        id: `f${i}`,
        name: `文件夹${i}`,
      })),
    );
    expect(out).toHaveLength(MAX_FOLDERS);
  });
});

describe('模型→文件夹分配清洗', () => {
  const folders = [
    { id: 'f1', name: 'A' },
    { id: 'f2', name: 'B' },
  ];

  test('只保留指向存在文件夹的条目，trim 并丢弃坏形状', () => {
    expect(
      normalizeModelFolders(
        {
          ' mdl_1 ': ' f1 ',
          mdl_2: 'f2',
          mdl_3: 'gone',
          '': 'f1',
          mdl_4: 3,
          ['x'.repeat(129)]: 'f1',
        },
        folders,
      ),
    ).toEqual({ mdl_1: 'f1', mdl_2: 'f2' });
    expect(normalizeModelFolders(['nope'], folders)).toEqual({});
  });

  test('数量上限', () => {
    const raw: Record<string, string> = {};
    for (let i = 0; i < MAX_FOLDER_ASSIGNMENTS + 100; i++) raw[`m${i}`] = 'f1';
    expect(Object.keys(normalizeModelFolders(raw, folders))).toHaveLength(
      MAX_FOLDER_ASSIGNMENTS,
    );
  });
});

describe('文件夹纯操作（不可变）', () => {
  test('addFolder 追加且不修改原状态，重复 id 忽略', () => {
    const origin = state([{ id: 'f1', name: 'A' }]);
    const next = addFolder(origin, { id: 'f2', name: ' B ' });
    expect(next.folders).toEqual([
      { id: 'f1', name: 'A' },
      { id: 'f2', name: 'B' },
    ]);
    expect(origin.folders).toHaveLength(1);
    expect(addFolder(origin, { id: 'f1', name: '重复' }).folders).toHaveLength(1);
  });

  test('renameFolder 只改名，trim 后为空则不变，未知 id 不变', () => {
    const origin = state([{ id: 'f1', name: 'A' }]);
    expect(renameFolder(origin, 'f1', ' 新名 ').folders).toEqual([
      { id: 'f1', name: '新名' },
    ]);
    expect(renameFolder(origin, 'f1', '   ').folders).toEqual(origin.folders);
    expect(renameFolder(origin, 'gone', '新名').folders).toEqual(origin.folders);
  });

  test('deleteFolder 删除文件夹并清空其分配，其它分配保留', () => {
    const origin = state(
      [
        { id: 'f1', name: 'A' },
        { id: 'f2', name: 'B' },
      ],
      { mdl_a: 'f1', mdl_b: 'f2' },
    );
    const next = deleteFolder(origin, 'f1');
    expect(next.folders).toEqual([{ id: 'f2', name: 'B' }]);
    expect(next.modelFolders).toEqual({ mdl_b: 'f2' });
    expect(origin.modelFolders).toEqual({ mdl_a: 'f1', mdl_b: 'f2' });
  });

  test('assignModelsToFolder 批量移动；folderId=null 移出分组；未知文件夹不变', () => {
    const origin = state([{ id: 'f1', name: 'A' }], { mdl_a: 'f1' });
    const moved = assignModelsToFolder(origin, ['mdl_a', 'mdl_b', ' '], 'f1');
    expect(moved.modelFolders).toEqual({ mdl_a: 'f1', mdl_b: 'f1' });
    expect(origin.modelFolders).toEqual({ mdl_a: 'f1' });

    expect(assignModelsToFolder(moved, ['mdl_a'], null).modelFolders).toEqual({
      mdl_b: 'f1',
    });
    expect(assignModelsToFolder(origin, ['mdl_b'], 'gone')).toBe(origin);
  });
});

describe('文件夹筛选与计数', () => {
  const models = ['a', 'b', 'c', 'd'].map(model);
  const s = state(
    [
      { id: 'f1', name: 'T+5' },
      { id: 'f2', name: '港股' },
    ],
    { a: 'f1', b: 'f2', c: 'gone' },
  );

  test('全部 / 未分组 / 指定文件夹；悬空分配按未分组', () => {
    expect(ids(filterModelsByFolder(models, null, s))).toEqual(['a', 'b', 'c', 'd']);
    expect(ids(filterModelsByFolder(models, UNFILED_FOLDER_KEY, s))).toEqual([
      'c',
      'd',
    ]);
    expect(ids(filterModelsByFolder(models, 'f1', s))).toEqual(['a']);
    expect(ids(filterModelsByFolder(models, 'gone', s))).toEqual([]);
  });

  test('计数与当前查看用的判定一致', () => {
    expect(countModelsByFolder(models, s)).toEqual({
      total: 4,
      unfiled: 2,
      byFolder: { f1: 1, f2: 1 },
    });
  });

  test('folderOfModel 返回文件夹对象，悬空/未分配返回 null', () => {
    expect(folderOfModel('a', s)).toEqual({ id: 'f1', name: 'T+5' });
    expect(folderOfModel('c', s)).toBeNull();
    expect(folderOfModel('d', s)).toBeNull();
  });
});

describe('文件夹名校验', () => {
  const folders = [{ id: 'f1', name: 'T+5' }];

  test('空名/超长/同名（忽略大小写与首尾空格）拒绝', () => {
    expect(validateFolderName('  ', folders)).toBeTruthy();
    expect(validateFolderName('名'.repeat(FOLDER_NAME_MAX_LENGTH + 1), folders)).toBeTruthy();
    expect(validateFolderName(' t+5 ', folders)).toBeTruthy();
    expect(validateFolderName('T+5', folders, 'f1')).toBeNull();
  });

  test('合法名通过', () => {
    expect(validateFolderName(' 港股 ', folders)).toBeNull();
  });
});
