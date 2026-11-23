import { beforeEach, describe, expect, test } from 'vitest';
import type { UserModelRecord } from '../../services/modelTrainingService';
import {
  DEFAULT_MODEL_LIST_PREFS,
  MODEL_SORT_OPTIONS,
  loadModelListPrefs,
  modelListPrefsKey,
  prefsFromServer,
  prefsToServer,
  saveModelListPrefs,
  sortModelRecords,
  toggleId,
} from '../modelRegistryListPrefs';

function model(
  id: string,
  overrides: Partial<UserModelRecord> = {},
): UserModelRecord {
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
    ...overrides,
  };
}

const ids = (list: UserModelRecord[]) => list.map((m) => m.model_id);

describe('模型列表偏好存储（置顶/收藏/排序）', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  test('无存储时返回默认偏好', () => {
    expect(loadModelListPrefs('CN')).toEqual(DEFAULT_MODEL_LIST_PREFS);
  });

  test('读写往返并按市场隔离（含文件夹与分配）', () => {
    saveModelListPrefs('CN', {
      pinned: ['mdl_a'],
      favorites: ['mdl_b'],
      sortKey: 'test_ic_desc',
      folders: [{ id: 'f1', name: 'T+5' }],
      modelFolders: { mdl_a: 'f1' },
    });
    expect(loadModelListPrefs('CN')).toEqual({
      pinned: ['mdl_a'],
      favorites: ['mdl_b'],
      sortKey: 'test_ic_desc',
      folders: [{ id: 'f1', name: 'T+5' }],
      modelFolders: { mdl_a: 'f1' },
    });
    expect(loadModelListPrefs('HK')).toEqual(DEFAULT_MODEL_LIST_PREFS);
    expect(modelListPrefsKey('CN')).not.toBe(modelListPrefsKey('HK'));
  });

  test('存储损坏或字段非法时降级为默认，不抛出', () => {
    localStorage.setItem(modelListPrefsKey('CN'), '{bad json');
    expect(loadModelListPrefs('CN')).toEqual(DEFAULT_MODEL_LIST_PREFS);

    localStorage.setItem(
      modelListPrefsKey('CN'),
      JSON.stringify({ pinned: 'mdl_a', favorites: [1, 'mdl_b', null], sortKey: 'nonsense' }),
    );
    expect(loadModelListPrefs('CN')).toEqual({
      pinned: [],
      favorites: ['mdl_b'],
      sortKey: 'default',
      folders: [],
      modelFolders: {},
    });
  });

  test('读取时丢弃指向不存在文件夹的悬空分配', () => {
    localStorage.setItem(
      modelListPrefsKey('CN'),
      JSON.stringify({
        folders: [{ id: 'f1', name: 'T+5' }],
        modelFolders: { mdl_a: 'f1', mdl_b: 'gone' },
      }),
    );
    expect(loadModelListPrefs('CN').modelFolders).toEqual({ mdl_a: 'f1' });
  });
});

describe('前后端偏好转换', () => {
  test('服务端 snake_case → 前端 camelCase，并丢弃悬空文件夹分配', () => {
    expect(
      prefsFromServer({
        pinned: ['mdl_a'],
        favorites: ['mdl_b'],
        sort_key: 'name_asc',
        folders: [{ id: 'f1', name: 'T+5' }],
        model_folders: { mdl_a: 'f1', mdl_b: 'gone' },
      }),
    ).toEqual({
      pinned: ['mdl_a'],
      favorites: ['mdl_b'],
      sortKey: 'name_asc',
      folders: [{ id: 'f1', name: 'T+5' }],
      modelFolders: { mdl_a: 'f1' },
    });
  });

  test('前端 camelCase → 服务端 snake_case', () => {
    expect(
      prefsToServer({
        pinned: [],
        favorites: [],
        sortKey: 'default',
        folders: [{ id: 'f1', name: 'T+5' }],
        modelFolders: { mdl_a: 'f1' },
      }),
    ).toEqual({
      pinned: [],
      favorites: [],
      sort_key: 'default',
      folders: [{ id: 'f1', name: 'T+5' }],
      model_folders: { mdl_a: 'f1' },
    });
  });
});

describe('toggleId', () => {  test('切换不修改原数组，置顶模式新项排最前', () => {
    const original = ['a', 'b'];
    expect(toggleId(original, 'c', true)).toEqual(['c', 'a', 'b']);
    expect(toggleId(original, 'a')).toEqual(['b']);
    expect(original).toEqual(['a', 'b']);
  });
});

describe('sortModelRecords', () => {
  test('置顶模型恒在最前，按置顶顺序排列；其余保持原顺序（默认排序）', () => {
    const models = [model('a'), model('b'), model('c'), model('d')];
    expect(ids(sortModelRecords(models, { pinned: ['c', 'a'], sortKey: 'default' }))).toEqual([
      'c',
      'a',
      'b',
      'd',
    ]);
  });

  test('创建时间新→旧，缺失时间排最后', () => {
    const models = [
      model('old', { created_at: '2026-01-01T00:00:00Z' }),
      model('none'),
      model('new', { created_at: '2026-10-01T00:00:00Z' }),
    ];
    expect(ids(sortModelRecords(models, { sortKey: 'created_desc' }))).toEqual([
      'new',
      'old',
      'none',
    ]);
  });

  test('创建时间旧→新，缺失时间仍排最后', () => {
    const models = [
      model('new', { created_at: '2026-10-01T00:00:00Z' }),
      model('none'),
      model('old', { created_at: '2026-01-01T00:00:00Z' }),
    ];
    expect(ids(sortModelRecords(models, { sortKey: 'created_asc' }))).toEqual([
      'old',
      'new',
      'none',
    ]);
  });

  test('名称 A→Z 使用展示名，而不是模型 ID', () => {
    const models = [
      model('mdl_1', { metadata_json: { display_name: 'Beta' } }),
      model('mdl_2', { metadata_json: { display_name: 'Alpha' } }),
    ];
    expect(ids(sortModelRecords(models, { sortKey: 'name_asc' }))).toEqual([
      'mdl_2',
      'mdl_1',
    ]);
  });

  test('测试 IC 高→低，缺失一律排最后（不当作 0）', () => {
    const models = [
      model('low', { metadata_json: { display_name: 'low', metrics: { test_ic: 0.05 } } }),
      model('missing'),
      model('high', { metadata_json: { display_name: 'high', metrics: { test_ic: 0.21 } } }),
    ];
    expect(ids(sortModelRecords(models, { sortKey: 'test_ic_desc' }))).toEqual([
      'high',
      'low',
      'missing',
    ]);
  });

  test('预测周期短→长，缺失排最后', () => {
    const models = [
      model('t20', { metadata_json: { display_name: 't20', target_horizon_days: 20 } }),
      model('t5', { metadata_json: { display_name: 't5', target_horizon_days: 5 } }),
      model('none'),
    ];
    expect(ids(sortModelRecords(models, { sortKey: 'horizon_asc' }))).toEqual([
      't5',
      't20',
      'none',
    ]);
  });

  test('排序选项覆盖默认/时间/名称/IC/周期，且默认项唯一', () => {
    const keys = MODEL_SORT_OPTIONS.map((o) => o.key);
    expect(keys[0]).toBe('default');
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain('created_desc');
    expect(keys).toContain('created_asc');
    expect(keys).toContain('name_asc');
    expect(keys).toContain('test_ic_desc');
    expect(keys).toContain('horizon_asc');
  });
});
