/**
 * 训练页「自定义数据(CUSTOM)」市场 —— 页面级覆盖的口径与导出/导入闭环。
 *
 * 为什么值得钉住：
 * 1) CUSTOM 不是全局市场（不进 AppMarket），只允许经训练页的页面级覆盖进入；
 *    解析函数一旦写反（全局市场被当成页面级、或非法值不回落），训练会带着
 *    错误的 qlib region / 基准静默跑偏。
 * 2) 自定义数据的特征名由上传者定义（如 rd_mined 的挖掘因子列），任何硬编码
 *    预设都是在捏造特征。三个防线必须同时有效：`getDefaultFeaturesForMarket`
 *    对 CUSTOM 返回空、`resolveDefaultSelectedFeatures` 在无目录 flag 时不落
 *    PRESET 兜底、catalog 拉取失败时训练页清空特征而不是填 A 股预设。
 *    fixture 里刻意混入了 PRESET 键（liq_turnover_os/mom_kdj_k），任何一处
 *    防线退回「PRESET 过滤」，断言立刻非空失败——不允许空对空的假通过。
 * 3) 导出的配置文件是跨设备交接协议：CUSTOM 必须能原样往返（market、
 *    factor_source、特征、分段），否则 rd_mined 模型在另一台机器上导不进来。
 */

import { describe, expect, it } from 'vitest';

import {
  buildTrainingConfigFile,
  DEFAULT_CONTEXT,
  DEFAULT_PARAMS,
  DEFAULT_TARGET,
  getDefaultFeaturesForMarket,
  parseTrainingConfig,
  PRESET_DEFAULT_FEATURES,
  resolveDefaultSelectedFeatures,
  resolveTrainingMarket,
  serializeTrainingConfig,
  TRAINING_MARKET_OPTIONS,
  type FeatureCategory,
  type TrainingConfigFile,
  type TrainingDraft,
  type TrainingMarket,
} from '../trainingUtils';
import { CUSTOM_DATA_MARKET_CONFIG, getMarketConfig } from '../../../config/marketConfig';

// ─── 页面级市场解析 ──────────────────────────────────────────────────────────

describe('resolveTrainingMarket', () => {
  it('页面级覆盖(CUSTOM)优先于全局市场', () => {
    expect(resolveTrainingMarket('CUSTOM', 'US')).toBe('CUSTOM');
    expect(resolveTrainingMarket('CUSTOM', undefined)).toBe('CUSTOM');
  });

  it('无覆盖时跟随全局市场（含大小写归一）', () => {
    expect(resolveTrainingMarket(null, 'HK')).toBe('HK');
    expect(resolveTrainingMarket(null, 'custom')).toBe('CUSTOM');
    expect(resolveTrainingMarket(undefined, 'CN')).toBe('CN');
  });

  it('非法值一律回落：覆盖非法回落全局，全局非法回落 CN', () => {
    expect(resolveTrainingMarket('MARS' as unknown as TrainingMarket, 'US')).toBe('US');
    expect(resolveTrainingMarket(null, 'MARS')).toBe('CN');
    expect(resolveTrainingMarket(null, undefined)).toBe('CN');
    expect(resolveTrainingMarket(null, '')).toBe('CN');
  });

  it('CUSTOM 在训练页市场词表里且只出现一次', () => {
    const hits = TRAINING_MARKET_OPTIONS.filter((option) => option.value === 'CUSTOM');
    expect(hits).toHaveLength(1);
  });
});

describe('getMarketConfig(CUSTOM)', () => {
  it('语义字段镜像 A 股口径（adapter/日历/基准），因为行情一律走 A 股供给', () => {
    expect(getMarketConfig('CUSTOM')).toEqual(CUSTOM_DATA_MARKET_CONFIG);
    expect(CUSTOM_DATA_MARKET_CONFIG.adapterId).toBe('a_share');
    expect(CUSTOM_DATA_MARKET_CONFIG.calendar).toBe('SSE');
    expect(CUSTOM_DATA_MARKET_CONFIG.benchmark).toBe('SH000300');
  });

  it('是独立配置对象，不与 CN 共享引用', () => {
    expect(getMarketConfig('CUSTOM')).not.toBe(getMarketConfig('CN'));
  });
});

// ─── 默认特征：自定义数据不许落任何预设 ──────────────────────────────────────

describe('CUSTOM 的默认特征防线', () => {
  it('getDefaultFeaturesForMarket 对 CUSTOM 返回空（空数组是显式答案，不是落兜底）', () => {
    expect(getDefaultFeaturesForMarket('CUSTOM')).toEqual([]);
    expect(getDefaultFeaturesForMarket('custom')).toEqual([]);
  });

  it('对照组：CN/未知市场的预设非空，证明空不是「函数永远返回空」', () => {
    expect(getDefaultFeaturesForMarket('CN').length).toBeGreaterThan(0);
    expect(PRESET_DEFAULT_FEATURES.length).toBeGreaterThan(0);
    // 未登记的 key 会落到 PRESET 兜底——说明兜底路径是活的，
    // CUSTOM 的空结果确实来自它那条显式空数组，而不是兜底恰好也空。
    expect(getDefaultFeaturesForMarket('ZZ')).toEqual(PRESET_DEFAULT_FEATURES);
  });

  // fixture 刻意包含 PRESET 里的键：若实现退回 PRESET 过滤，结果将非空
  const NO_FLAG_CATEGORIES: FeatureCategory[] = [
    {
      id: 'rd_mined',
      name: '挖掘因子',
      icon: null,
      features: [
        { key: 'liq_turnover_os', label: 'liq_turnover_os' },
        { key: 'mom_kdj_k', label: 'mom_kdj_k' },
        { key: 'fac_rdx_01', label: 'fac_rdx_01' },
      ],
    },
  ];

  it('catalog 无 default_selected 时 CUSTOM 返回空，不拿 PRESET 猜特征', () => {
    expect(resolveDefaultSelectedFeatures(NO_FLAG_CATEGORIES, 'CUSTOM')).toEqual([]);
  });

  it('规则与 CN 同侧：CN 无 flag 同样返回空', () => {
    expect(resolveDefaultSelectedFeatures(NO_FLAG_CATEGORIES, 'CN')).toEqual([]);
  });

  it('对照组：catalog 下发 default_selected 时以 flag 为准（证明上一条不是恒空）', () => {
    const flagged: FeatureCategory[] = [
      {
        id: 'rd_mined',
        name: '挖掘因子',
        icon: null,
        features: [
          { key: 'liq_turnover_os', label: 'liq_turnover_os', defaultSelected: false },
          { key: 'fac_rdx_01', label: 'fac_rdx_01', defaultSelected: true },
        ],
      },
    ];
    expect(resolveDefaultSelectedFeatures(flagged, 'CUSTOM')).toEqual(['fac_rdx_01']);
  });
});

// ─── 配置文件导出/导入闭环 ───────────────────────────────────────────────────

const CUSTOM_DRAFT: Omit<TrainingDraft, 'lastSavedAt'> = {
  displayName: '自定义数据 · RD 挖掘因子',
  displayNameMode: 'manual',
  selectedFeatures: ['fac_rdx_01', 'fac_rdx_02'],
  timePeriods: {
    train: ['2018-01-01T00:00:00.000Z', '2023-12-31T00:00:00.000Z'],
    val: ['2024-01-01T00:00:00.000Z', '2024-12-31T00:00:00.000Z'],
    test: ['2025-01-01T00:00:00.000Z', '2025-12-31T00:00:00.000Z'],
  },
  target: DEFAULT_TARGET,
  params: DEFAULT_PARAMS,
  context: { ...DEFAULT_CONTEXT, market: 'CUSTOM' },
  wfa: { enabled: true, strategy: 'rolling', nWindows: 6, trainYears: 4, valMonths: 6, stepMonths: 3 },
};

describe('CUSTOM 配置文件的导出/导入往返', () => {
  const buildFile = () =>
    buildTrainingConfigFile(CUSTOM_DRAFT, {
      market: 'CUSTOM',
      factor_source: 'rd_mined',
      factor_catalog_version: 'custom-v1',
    });

  it('导出时顶层 market 与 factor_source 如实写入', () => {
    const file = buildFile();
    expect(file.market).toBe('CUSTOM');
    expect(file.factor_source).toBe('rd_mined');
    expect(typeof file.exported_at).toBe('string');
  });

  it('往返后 market/factor_source/特征/分段/上下文原样保留', () => {
    const parsed = parseTrainingConfig(serializeTrainingConfig(buildFile()));

    expect(parsed.market).toBe('CUSTOM');
    expect(parsed.factorSource).toBe('rd_mined');
    expect(parsed.factorCatalogVersion).toBe('custom-v1');

    expect(parsed.draft.selectedFeatures).toEqual(['fac_rdx_01', 'fac_rdx_02']);
    expect(parsed.draft.timePeriods.train).toEqual(CUSTOM_DRAFT.timePeriods.train);
    expect(parsed.draft.timePeriods.test).toEqual(CUSTOM_DRAFT.timePeriods.test);
    expect(parsed.draft.context.market).toBe('CUSTOM');
    expect(parsed.draft.displayName).toBe(CUSTOM_DRAFT.displayName);

    // context 的 A 股口径（基准）必须随文件走，否则别处导入会换口径
    expect(parsed.draft.context.benchmark).toBe(DEFAULT_CONTEXT.benchmark);
    expect(parsed.draft.params.model_type).toBe('lightgbm');
    expect(parsed.draft.params.model_types).toEqual(['lightgbm']);
    expect(parsed.draft.params.learning_rate).toBe(DEFAULT_PARAMS.learning_rate);
    expect(parsed.draft.wfa).toEqual(CUSTOM_DRAFT.wfa);
  });

  it('反例：市场不在训练词表内（含大小写漂移）必须拒绝，而不是照单全收', () => {
    const file = buildFile();

    const unknown: TrainingConfigFile = {
      ...file,
      market: 'MARS' as unknown as TrainingConfigFile['market'],
    };
    expect(() => parseTrainingConfig(serializeTrainingConfig(unknown))).toThrow('配置中的市场标识无效');

    const caseDrift: TrainingConfigFile = {
      ...file,
      market: 'custom' as unknown as TrainingConfigFile['market'],
    };
    expect(() => parseTrainingConfig(serializeTrainingConfig(caseDrift))).toThrow('配置中的市场标识无效');
  });
});
