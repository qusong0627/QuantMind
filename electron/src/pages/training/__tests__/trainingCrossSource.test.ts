/**
 * 训练页「跨源因子库」（锚库 + 附加库）—— 纯函数层。
 *
 * 为什么值得钉住：
 * 1) 后端 `_resolve_quantdb_factor_payload` 会把 features 重写成**裸 feature_key**
 *    再去重，同一个 key 从两个库被请求直接 422
 *    （`Feature 'X' is requested from two different sources`）。所以「哪个库拥有
 *    这个 key」必须在前端单选定死，不能等提交时才发现。
 * 2) 裸名（不带库前缀）的解析顺序是「锚库 → 声明序首个命中者胜」。一旦归属算错，
 *    最危险的失败形态不是报错而是**静默换源**：用户以为在训练 L2 的 ALPHA_001，
 *    实际训练的是锚库同名因子——列名相同、日志无痕，模型换了一列只能靠 IC 才发现。
 *    因此「锚库发裸名、附加库发 `库:feature_key`」是必须钉住的不变量。
 * 3) 副库版本必须显式 pin 且只发用到的库：多发的库会白拉一次目录、并在该库目录
 *    未发布时把整次训练打成 422。
 * 4) 导出的 YAML 是跨设备交接协议，附加库清单丢一截，另一台机器就会静默训练成
 *    单库模型（特征数少一截，没人报错）。
 */

import { describe, expect, it } from 'vitest';

import {
  buildBackendTrainingPayload,
  buildCrossSourceFeaturePlan,
  buildExtraFactorSourceOptions,
  buildFeatureOwnershipMap,
  buildTrainingConfigFile,
  buildTrainingRequest,
  DEFAULT_CONTEXT,
  DEFAULT_PARAMS,
  DEFAULT_TARGET,
  DEFAULT_TIME_PERIODS,
  findCrossSourceFeatureConflicts,
  formatCrossSourceConflictMessage,
  mergeSourceFeatureCategories,
  parseTrainingConfig,
  sanitizeExtraFactorSources,
  serializeTrainingConfig,
  summarizeFeatureCategories,
  type FeatureCategory,
  type TrainingConfigFile,
  type TrainingDraft,
} from '../trainingUtils';
import type { QuantDBTrainingSource } from '../../../features/admin/types';

// ─── fixtures ────────────────────────────────────────────────────────────────

const cat = (id: string, name: string, keys: string[]): FeatureCategory => ({
  id,
  name,
  icon: null,
  features: keys.map((key) => ({ key, label: key })),
});

const ANCHOR = 'l1_factors';
const EXTRA_A = 'l2_factors';
const EXTRA_B = 'l3_factors';
const EXTRA_UNUSED = 'l4_factors';

// 锚库与 EXTRA_A 共有 MOM_5；EXTRA_A 与 EXTRA_B 共有 ALPHA_001
const ANCHOR_CATS = [cat('momentum', '动量', ['VOLUME48', 'MOM_5', 'LIQ_AMT'])];
const EXTRA_A_CATS = [cat('alpha101', 'Alpha101', ['ALPHA_001', 'MOM_5'])];
const EXTRA_B_CATS = [cat('gtja', '国泰君安', ['ALPHA_001', 'GTJA_042'])];
const EXTRA_UNUSED_CATS = [cat('misc', '杂项', ['UNUSED_001'])];

const VERSIONS: Record<string, string> = {
  [EXTRA_A]: 'v-a',
  [EXTRA_B]: 'v-b',
  [EXTRA_UNUSED]: 'v-unused',
};

// ─── 同名冲突检测 ─────────────────────────────────────────────────────────────

describe('findCrossSourceFeatureConflicts', () => {
  it('锚库与附加库同名：报出该 key 与两个库（锚库在前）', () => {
    const conflicts = findCrossSourceFeatureConflicts(ANCHOR, ANCHOR_CATS, {
      [EXTRA_A]: EXTRA_A_CATS,
    });

    expect(conflicts).toEqual([{ featureKey: 'MOM_5', sources: [ANCHOR, EXTRA_A] }]);
  });

  it('两个附加库之间同名（锚库没有该 key）：同样要拦住', () => {
    // 用一份与附加库无交集的锚库目录，证明冲突与锚库无关
    const disjointAnchor = [cat('momentum', '动量', ['VOLUME48'])];

    const conflicts = findCrossSourceFeatureConflicts(ANCHOR, disjointAnchor, {
      [EXTRA_A]: EXTRA_A_CATS,
      [EXTRA_B]: EXTRA_B_CATS,
    });

    expect(conflicts).toEqual([{ featureKey: 'ALPHA_001', sources: [EXTRA_A, EXTRA_B] }]);
  });

  it('三个库同名：三个库全部列出，一个都不能漏', () => {
    const share = [cat('x', 'X', ['SHARED_01'])];
    const conflicts = findCrossSourceFeatureConflicts(ANCHOR, share, {
      [EXTRA_A]: share,
      [EXTRA_B]: share,
    });

    expect(conflicts).toEqual([
      { featureKey: 'SHARED_01', sources: [ANCHOR, EXTRA_A, EXTRA_B] },
    ]);
  });

  it('无同名：返回空数组（不是「宁杀错」的全量清单）', () => {
    const disjointAnchor = [cat('momentum', '动量', ['VOLUME48'])];
    const disjointExtra = [cat('alpha101', 'Alpha101', ['ALPHA_001'])];

    expect(findCrossSourceFeatureConflicts(ANCHOR, disjointAnchor, { [EXTRA_A]: disjointExtra })).toEqual([]);
    expect(findCrossSourceFeatureConflicts(ANCHOR, disjointAnchor, {})).toEqual([]);
  });

  it('同一个库内跨分类重名不算跨源冲突（后端同来源重复是静默去重）', () => {
    const duplicated = [cat('a', 'A', ['DUP_01']), cat('b', 'B', ['DUP_01'])];

    // 只在锚库内部重复：不是跨源冲突
    expect(findCrossSourceFeatureConflicts(ANCHOR, duplicated, {})).toEqual([]);
    // 跨到副库才算冲突，且每个库只列一次（不会因库内重复出现两遍）
    expect(findCrossSourceFeatureConflicts(ANCHOR, duplicated, { [EXTRA_A]: duplicated }))
      .toEqual([{ featureKey: 'DUP_01', sources: [ANCHOR, EXTRA_A] }]);
  });
});

// ─── 归属表（谁拥有这个 feature_key） ─────────────────────────────────────────

describe('buildFeatureOwnershipMap', () => {
  it('锚库优先：同名 key 归锚库，与后端裸名「首个命中者胜」同序', () => {
    const ownership = buildFeatureOwnershipMap(ANCHOR, ANCHOR_CATS, { [EXTRA_A]: EXTRA_A_CATS });

    expect(ownership['VOLUME48']).toBe(ANCHOR);
    expect(ownership['MOM_5']).toBe(ANCHOR);
    expect(ownership['ALPHA_001']).toBe(EXTRA_A);
  });

  it('两个附加库同名：按声明序归先声明的那个库', () => {
    const disjointAnchor = [cat('momentum', '动量', ['VOLUME48'])];

    const ownership = buildFeatureOwnershipMap(ANCHOR, disjointAnchor, {
      [EXTRA_A]: EXTRA_A_CATS,
      [EXTRA_B]: EXTRA_B_CATS,
    });

    expect(ownership['ALPHA_001']).toBe(EXTRA_A);
    expect(ownership['GTJA_042']).toBe(EXTRA_B);
  });

  it('没有附加库时只含锚库特征', () => {
    const ownership = buildFeatureOwnershipMap(ANCHOR, ANCHOR_CATS, {});

    expect(Object.keys(ownership).sort()).toEqual(['LIQ_AMT', 'MOM_5', 'VOLUME48']);
  });
});

// ─── 载荷组装（裸名 / 限定名 / 版本） ─────────────────────────────────────────

describe('buildCrossSourceFeaturePlan', () => {
  const ownership = buildFeatureOwnershipMap(ANCHOR, ANCHOR_CATS, {
    [EXTRA_A]: EXTRA_A_CATS,
    [EXTRA_B]: EXTRA_B_CATS,
  });

  it('锚库特征发裸名，附加库特征发「库:feature_key」', () => {
    const plan = buildCrossSourceFeaturePlan(
      ['VOLUME48', 'ALPHA_001', 'GTJA_042', 'MOM_5'],
      ANCHOR,
      ownership,
      VERSIONS,
    );

    expect(plan.features).toEqual([
      'VOLUME48',
      `l2_factors:ALPHA_001`,
      `l3_factors:GTJA_042`,
      'MOM_5',
    ]);
    expect(plan.unresolved).toEqual([]);
    expect(plan.unversionedSources).toEqual([]);
  });

  it('factor_catalog_versions 只放被选中的附加库（多发的库会白拉目录、白报 422）', () => {
    const plan = buildCrossSourceFeaturePlan(
      ['VOLUME48', 'ALPHA_001'],
      ANCHOR,
      ownership,
      VERSIONS,
    );

    expect(plan.factorCatalogVersions).toEqual({ [EXTRA_A]: 'v-a' });
    expect(plan.factorCatalogVersions[EXTRA_UNUSED]).toBeUndefined();
  });

  it('选中顺序保留、重复去重（同一特征写两遍不该发两遍）', () => {
    const plan = buildCrossSourceFeaturePlan(
      ['ALPHA_001', 'VOLUME48', 'ALPHA_001'],
      ANCHOR,
      ownership,
      VERSIONS,
    );

    expect(plan.features).toEqual(['l2_factors:ALPHA_001', 'VOLUME48']);
  });

  it('归属未知的键必须单列，绝不当锚库裸名发出去（裸名会被静默解析成别的库的同名因子）', () => {
    const plan = buildCrossSourceFeaturePlan(['VOLUME48', 'GHOST_01'], ANCHOR, ownership, VERSIONS);

    expect(plan.features).toEqual(['VOLUME48']);
    expect(plan.unresolved).toEqual(['GHOST_01']);
  });

  it('副库有特征被选中但没 pin 版本：仍按限定名发出，同时单独标记以便阻断提交', () => {
    const plan = buildCrossSourceFeaturePlan(['ALPHA_001'], ANCHOR, ownership, {});

    expect(plan.features).toEqual([`l2_factors:ALPHA_001`]);
    expect(plan.unversionedSources).toEqual([EXTRA_A]);
    expect(plan.factorCatalogVersions).toEqual({});
  });

  it('没有附加库时行为与单库完全一致：全部裸名、不带版本映射', () => {
    const anchorOnly = buildFeatureOwnershipMap(ANCHOR, ANCHOR_CATS, {});
    const plan = buildCrossSourceFeaturePlan(['VOLUME48', 'MOM_5'], ANCHOR, anchorOnly, VERSIONS);

    expect(plan.features).toEqual(['VOLUME48', 'MOM_5']);
    expect(plan.factorCatalogVersions).toEqual({});
  });
});

// ─── 提交载荷（页面真正发给后端的那一份） ────────────────────────────────────

describe('提交载荷组装 buildBackendTrainingPayload', () => {
  const ownership = buildFeatureOwnershipMap(ANCHOR, ANCHOR_CATS, {
    [EXTRA_A]: EXTRA_A_CATS,
    [EXTRA_B]: EXTRA_B_CATS,
  });

  const buildPayload = (selected: string[], crossSource = true) => {
    const request = buildTrainingRequest(
      selected,
      ANCHOR_CATS,
      DEFAULT_TIME_PERIODS,
      DEFAULT_TARGET,
      DEFAULT_PARAMS,
      DEFAULT_CONTEXT,
      '跨源载荷',
      'CN',
    );
    return buildBackendTrainingPayload(request, DEFAULT_TIME_PERIODS, {
      crossSource: crossSource
        ? { anchorSource: ANCHOR, ownership, extraCatalogVersions: VERSIONS }
        : undefined,
    }) as { features: string[]; factor_catalog_versions?: Record<string, string> };
  };

  it('副库特征带 "库:feature_key" 前缀、锚库保持裸名（后端就靠这个区分同名）', () => {
    const payload = buildPayload(['VOLUME48', 'ALPHA_001', 'GTJA_042']);

    expect(payload.features).toEqual(['VOLUME48', 'l2_factors:ALPHA_001', 'l3_factors:GTJA_042']);
  });

  it('factor_catalog_versions 只 pin 本次用到的副库，没选中的库一个都不发', () => {
    const payload = buildPayload(['VOLUME48', 'ALPHA_001']);

    expect(payload.factor_catalog_versions).toEqual({ [EXTRA_A]: 'v-a' });
  });

  it('全是锚库特征时不带 factor_catalog_versions 键（空映射是噪声，不是「没有」）', () => {
    const payload = buildPayload(['VOLUME48', 'MOM_5']);

    expect(payload.features).toEqual(['VOLUME48', 'MOM_5']);
    expect('factor_catalog_versions' in payload).toBe(false);
  });

  it('对照组：没有跨源选项时退回全裸名的旧行为，证明前缀不是无条件加的', () => {
    const payload = buildPayload(['VOLUME48', 'ALPHA_001'], false);

    expect(payload.features).toEqual(['VOLUME48', 'ALPHA_001']);
    expect('factor_catalog_versions' in payload).toBe(false);
  });

  it('归属未知的键绝不以裸名混进 features（宁可少发一个，也不能静默换源）', () => {
    const payload = buildPayload(['VOLUME48', 'GHOST_01']);

    expect(payload.features).toEqual(['VOLUME48']);
  });
});

// ─── 来源标注（feature_categories 不能凭空多一个库） ─────────────────────────

describe('summarizeFeatureCategories', () => {
  const merged = mergeSourceFeatureCategories(
    ANCHOR,
    [cat('momentum', '动量', ['VOLUME48', 'MOM_5'])],
    { [EXTRA_A]: [cat('momentum', '动量', ['ALPHA_001', 'MOM_5'])] },
    { [ANCHOR]: 'L1 基础因子', [EXTRA_A]: 'L2 资金流' },
  );

  it('纯锚库特征不把副库同名分类算进来（影子副本不算归属）', () => {
    expect(summarizeFeatureCategories(['VOLUME48', 'MOM_5'], merged)).toEqual(['动量']);
  });

  it('副库特征列出带来源后缀的分类名，用户能从载荷里看出多了一个库', () => {
    expect(summarizeFeatureCategories(['VOLUME48', 'ALPHA_001'], merged)).toEqual(['动量', '动量 · L2 资金流']);
  });
});

// ─── 冲突文案 ────────────────────────────────────────────────────────────────

describe('formatCrossSourceConflictMessage', () => {
  it('文案点名两个库的显示名与特征，不用裸 id 让人猜', () => {
    const message = formatCrossSourceConflictMessage(
      { featureKey: 'MOM_5', sources: [ANCHOR, EXTRA_A] },
      { [ANCHOR]: 'L1 基础因子', [EXTRA_A]: 'L2 资金流' },
    );

    expect(message).toContain('MOM_5');
    expect(message).toContain('L1 基础因子');
    expect(message).toContain('L2 资金流');
    expect(message).not.toContain(ANCHOR);
  });

  it('库名缺失时回落库 id，不给出半个句子', () => {
    const message = formatCrossSourceConflictMessage(
      { featureKey: 'MOM_5', sources: [ANCHOR, EXTRA_A] },
      { [ANCHOR]: 'L1 基础因子' },
    );

    expect(message).toContain('L1 基础因子');
    expect(message).toContain(EXTRA_A);
  });
});

// ─── 附加库下拉选项 ───────────────────────────────────────────────────────────

describe('buildExtraFactorSourceOptions', () => {
  const sources: QuantDBTrainingSource[] = [
    { id: ANCHOR, name: 'L1 基础因子', default: true, ready: true, published: true, trainable: true, feature_count: 120, catalog_version: 'v1', schema_hash: 'h1', reason: null },
    { id: EXTRA_A, name: 'L2 资金流', default: false, ready: true, published: true, trainable: true, feature_count: 60, catalog_version: 'v2', schema_hash: 'h2', reason: null },
    { id: EXTRA_B, name: 'L3 待发布', default: false, ready: false, published: false, trainable: false, feature_count: 0, catalog_version: null, schema_hash: 'h3', reason: '尚未发布因子目录' },
  ];

  it('排除锚库自身（否则会出现「自己配自己」的重复源）', () => {
    const options = buildExtraFactorSourceOptions(sources, ANCHOR);

    expect(options.map((option) => option.value)).toEqual([EXTRA_A, EXTRA_B]);
  });

  it('未发布 / 没有目录版本的库保留在列表里但 disabled，并带上原因', () => {
    const options = buildExtraFactorSourceOptions(sources, ANCHOR);
    const blocked = options.find((option) => option.value === EXTRA_B);

    expect(blocked?.disabled).toBe(true);
    expect(blocked?.reason).toBe('尚未发布因子目录');
  });

  it('可用库可选中且无原因', () => {
    const options = buildExtraFactorSourceOptions(sources, ANCHOR);
    const usable = options.find((option) => option.value === EXTRA_A);

    expect(usable?.disabled).toBe(false);
    expect(usable?.reason).toBeNull();
    expect(usable?.label).toContain('L2 资金流');
  });
});

// ─── 附加库清单归一 ───────────────────────────────────────────────────────────

describe('sanitizeExtraFactorSources', () => {
  it('去重、剔除空值与非字符串，并剔除锚库自身', () => {
    expect(sanitizeExtraFactorSources([EXTRA_A, EXTRA_A, '', ANCHOR, 42, null], ANCHOR)).toEqual([EXTRA_A]);
  });

  it('非数组输入（旧草稿 / 脏 YAML）返回空数组而不是抛错', () => {
    expect(sanitizeExtraFactorSources(undefined, ANCHOR)).toEqual([]);
    expect(sanitizeExtraFactorSources('l2_factors', ANCHOR)).toEqual([]);
  });
});

// ─── 合并渲染（用户必须能看出特征来自哪个库） ────────────────────────────────

describe('mergeSourceFeatureCategories', () => {
  const labels = { [ANCHOR]: 'L1 基础因子', [EXTRA_A]: 'L2 资金流' };
  // 锚库与附加库各有一个同名分类 momentum，id 撞车是必然的
  const sameIdAnchor = [cat('momentum', '动量', ['VOLUME48', 'MOM_5'])];
  const sameIdExtra = [cat('momentum', '动量', ['ALPHA_001', 'MOM_5'])];

  it('附加库分类名后缀来源库、id 加库前缀（否则与锚库同名分类撞 React key）', () => {
    const merged = mergeSourceFeatureCategories(ANCHOR, sameIdAnchor, { [EXTRA_A]: sameIdExtra }, labels);

    expect(merged.map((category) => category.id)).toEqual(['momentum', `${EXTRA_A}::momentum`]);
    expect(merged[0].name).toBe('动量');
    expect(merged[1].name).toBe('动量 · L2 资金流');
  });

  it('每个特征都带来源标注，附加库特征一眼能看出归属', () => {
    const merged = mergeSourceFeatureCategories(ANCHOR, sameIdAnchor, { [EXTRA_A]: sameIdExtra }, labels);

    expect(merged[0].features.every((feature) => feature.sourceName === 'L1 基础因子')).toBe(true);
    expect(merged[1].features.every((feature) => feature.sourceId === EXTRA_A)).toBe(true);
  });

  it('跨库同名的副本被禁用并点名归属库，归属库那一份仍可勾选', () => {
    const merged = mergeSourceFeatureCategories(ANCHOR, sameIdAnchor, { [EXTRA_A]: sameIdExtra }, labels);

    const anchorCopy = merged[0].features.find((feature) => feature.key === 'MOM_5');
    const extraCopy = merged[1].features.find((feature) => feature.key === 'MOM_5');
    const extraOnly = merged[1].features.find((feature) => feature.key === 'ALPHA_001');

    expect(anchorCopy?.disabled).toBeUndefined();
    expect(extraCopy?.disabled).toBe(true);
    expect(extraCopy?.disabledReason).toContain('L1 基础因子');
    expect(extraOnly?.disabled).toBeUndefined();
  });

  it('没有附加库时保持原样：不加前缀、不加后缀、不标来源', () => {
    const merged = mergeSourceFeatureCategories(ANCHOR, sameIdAnchor, {}, labels);

    expect(merged.map((category) => category.id)).toEqual(['momentum']);
    expect(merged[0].name).toBe('动量');
    expect(merged[0].features[0].disabled).toBeUndefined();
  });
});

// ─── 配置文件导出/导入往返 ───────────────────────────────────────────────────

const CROSS_SOURCE_DRAFT: Omit<TrainingDraft, 'lastSavedAt'> = {
  displayName: '跨源训练 · L1+L2',
  displayNameMode: 'manual',
  selectedFeatures: ['VOLUME48', 'ALPHA_001'],
  timePeriods: {
    train: ['2018-01-01T00:00:00.000Z', '2023-12-31T00:00:00.000Z'],
    val: ['2024-01-01T00:00:00.000Z', '2024-12-31T00:00:00.000Z'],
    test: ['2025-01-01T00:00:00.000Z', '2025-12-31T00:00:00.000Z'],
  },
  target: DEFAULT_TARGET,
  params: DEFAULT_PARAMS,
  context: DEFAULT_CONTEXT,
  extraFactorSources: [EXTRA_A],
};

describe('跨源配置文件的导出/导入往返', () => {
  const buildFile = () =>
    buildTrainingConfigFile(CROSS_SOURCE_DRAFT, {
      market: 'CN',
      factor_source: ANCHOR,
      factor_catalog_version: 'v1',
      extra_factor_catalog_versions: { [EXTRA_A]: 'v2' },
    });

  it('导出：附加库清单与其版本 pin 写在顶层，且 configuration 里不重复一份', () => {
    const file = buildFile();

    expect(file.extra_factor_sources).toEqual([EXTRA_A]);
    expect(file.extra_factor_catalog_versions).toEqual({ [EXTRA_A]: 'v2' });
    // 顶层是唯一出处：两处都写迟早不一致
    expect((file.configuration as { extraFactorSources?: unknown }).extraFactorSources).toBeUndefined();
  });

  it('往返后附加库清单与版本原样保留（否则另一台机器静默训练成单库模型）', () => {
    const parsed = parseTrainingConfig(serializeTrainingConfig(buildFile()));

    expect(parsed.extraFactorSources).toEqual([EXTRA_A]);
    expect(parsed.extraFactorCatalogVersions).toEqual({ [EXTRA_A]: 'v2' });
    expect(parsed.draft.selectedFeatures).toEqual(['VOLUME48', 'ALPHA_001']);
  });

  it('旧配置（没有附加库字段）仍可导入，附加库为空而不是报错', () => {
    const legacy = buildTrainingConfigFile(
      { ...CROSS_SOURCE_DRAFT, extraFactorSources: [] },
      { market: 'CN', factor_source: ANCHOR, factor_catalog_version: 'v1' },
    );
    const parsed = parseTrainingConfig(serializeTrainingConfig(legacy));

    expect(parsed.extraFactorSources).toEqual([]);
    expect(parsed.extraFactorCatalogVersions).toEqual({});
  });

  it('反例：附加库清单写成非字符串数组必须拒绝，而不是照单全收', () => {
    const broken: TrainingConfigFile = {
      ...buildFile(),
      extra_factor_sources: 'l2_factors' as unknown as string[],
    };

    expect(() => parseTrainingConfig(serializeTrainingConfig(broken)))
      .toThrow('extra_factor_sources 必须是字符串数组');
  });

  it('反例：版本映射里出现非字符串值必须拒绝', () => {
    const broken: TrainingConfigFile = {
      ...buildFile(),
      extra_factor_catalog_versions: { [EXTRA_A]: 7 as unknown as string },
    };

    expect(() => parseTrainingConfig(serializeTrainingConfig(broken)))
      .toThrow('extra_factor_catalog_versions');
  });
});
