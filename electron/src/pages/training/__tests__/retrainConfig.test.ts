/**
 * 「模型管理 → 重新训练」配置映射测试。
 *
 * 钉住三件事：
 * 1. config.yaml 的字段要原样落到训练页草稿（特征 / 时间切分 / 超参 / 标签 / 上下文）；
 * 2. 缺 config.yaml 时按 metadata 兜底，并给出提示；
 * 3. 产出的配置必须能通过训练页自身的 `parseTrainingConfig` 校验（同一个 schema）。
 */

import { describe, expect, it } from 'vitest';

import { parseTrainingConfig } from '../trainingUtils';
import { buildRetrainTrainingConfig, stripSplitSuffix } from '../retrainConfig';
import { serializeTrainingConfig } from '../trainingUtils';

const DISPLAY_NAME =
  'CN 79特征全量(不筛选) T+10 · 训16-2025.10/验2025.11-2026.04/测2026.05-09_CN';

const CONFIG_PAYLOAD = {
  context: {
    market: 'CN',
    benchmark: 'SH000300',
    commission_rate: 0.00025,
    slippage: 0.0005,
    initial_capital: 1000000,
    deal_price: 'close',
    industry_as_feature: false,
  },
  model: {
    type: 'lightgbm',
    types: null,
    ensemble: 'none',
    prediction_mode: 'point',
    num_boost_round: 1000,
    early_stopping_rounds: 100,
    params: {
      learning_rate: 0.03,
      num_leaves: 31,
      min_data_in_leaf: 200,
      lambda_l2: 1.0,
      feature_fraction: 0.8,
      bagging_fraction: 0.8,
      bagging_freq: 1,
    },
    xgb_params: {},
    catboost_params: {},
    dl_params: {},
  },
  split: {
    train: ['2016-01-04', '2025-10-17'],
    valid: ['2025-11-03', '2026-04-16'],
    test: ['2026-05-06', '2026-09-30'],
  },
  label: {
    target_mode: 'return',
    target_horizon_days: 10,
    execution_price: 'close',
    label_transform: 'rank',
    label_top_pct: 0.2,
    label_drop_extreme_pct: 0,
  },
  data: {
    features: ['mom_ret_5d', 'vol_std_20'],
    factor_source: 'l1_factors',
    factor_catalog_version: 'v-l1-1',
    factor_catalog_versions: { l1_factors: 'v-l1-1' },
  },
  preprocessing: { enabled: true, winsor: true },
};

describe('buildRetrainTrainingConfig', () => {
  it('config.yaml 优先：特征/切分/超参/标签/上下文全部带入', () => {
    const { config, warnings } = buildRetrainTrainingConfig({
      model_id: 'm1',
      display_name: DISPLAY_NAME,
      market: 'CN',
      config: CONFIG_PAYLOAD,
      metadata: { model_type: 'lightgbm', market: 'CN' },
    });

    expect(warnings).toEqual([]);
    expect(config.market).toBe('CN');
    expect(config.factor_source).toBe('l1_factors');
    expect(config.factor_catalog_version).toBe('v-l1-1');
    expect(config.configuration.selectedFeatures).toEqual(['mom_ret_5d', 'vol_std_20']);
    expect(config.configuration.timePeriods.train).toEqual(['2016-01-04', '2025-10-17']);
    expect(config.configuration.timePeriods.val).toEqual(['2025-11-03', '2026-04-16']);
    expect(config.configuration.timePeriods.test).toEqual(['2026-05-06', '2026-09-30']);
    expect(config.configuration.params.model_type).toBe('lightgbm');
    expect(config.configuration.params.learning_rate).toBe(0.03);
    expect(config.configuration.params.num_leaves).toBe(31);
    expect(config.configuration.params.num_boost_round).toBe(1000);
    expect(config.configuration.params.preprocessingEnabled).toBe(true);
    expect(config.configuration.target.horizonDays).toBe(10);
    expect(config.configuration.target.executionPrice).toBe('close');
    expect(config.configuration.context.dealPrice).toBe('close');
    expect(config.configuration.context.benchmark).toBe('SH000300');
    // 旧名称里的时间切分描述会被剥掉，避免新模型顶着旧日期
    expect(config.configuration.displayName).toBe('CN 79特征全量(不筛选) T+10');
  });

  it('优先使用模型实际使用的特征（metadata.features），而不是提交候选集', () => {
    const { config, warnings } = buildRetrainTrainingConfig({
      model_id: 'm4',
      display_name: 'L1L2_ICIR优选',
      config: {
        ...CONFIG_PAYLOAD,
        data: { ...CONFIG_PAYLOAD.data, features: ['a', 'b', 'c', 'd'] },
      },
      metadata: {
        model_type: 'lightgbm',
        market: 'CN',
        features: ['a', 'b', 'c'],
        feature_columns: ['a', 'b', 'c'],
        requested_features: ['a', 'b', 'c', 'd'],
      },
    });

    expect(config.configuration.selectedFeatures).toEqual(['a', 'b', 'c']);
    expect(warnings.join('')).toContain('实际使用 3 个特征');
    expect(warnings.join('')).toContain('提交 4 个');
  });

  it('跨库模型：按 factor_field_sources 的首次出现顺序带回附加库与版本', () => {
    const { config, warnings } = buildRetrainTrainingConfig({
      model_id: 'cross',
      display_name: '跨库模型',
      config: {
        ...CONFIG_PAYLOAD,
        data: {
          ...CONFIG_PAYLOAD.data,
          features: ['a', 'b', 'c'],
          factor_source: 'anchor',
          factor_catalog_versions: { anchor: 'v0', libB: 'v2', libA: 'v1' },
          factor_field_sources: {
            a: 'anchor:a',
            b: 'libB:b',
            c: 'libA:c',
          },
        },
      },
      metadata: { features: ['a', 'b', 'c'] },
    });

    // 附加库顺序 = field_sources 首次出现顺序（同名特征归属依赖这个顺序）
    expect(config.extra_factor_sources).toEqual(['libB', 'libA']);
    expect(config.extra_factor_catalog_versions).toEqual({ libB: 'v2', libA: 'v1' });
    expect(config.factor_source).toBe('anchor');
    expect(config.configuration.selectedFeatures).toEqual(['a', 'b', 'c']);
    expect(warnings.join('')).toContain('跨库模型');
  });

  it('缺 config.yaml：按 metadata 兜底，并提示', () => {
    const { config, warnings } = buildRetrainTrainingConfig({
      model_id: 'm2',
      display_name: 'Fallback Model',
      config: null,
      metadata: {
        model_type: 'xgboost',
        market: 'CN',
        requested_features: ['f1', 'f2', 'f3'],
        train_start: '2018-01-02',
        train_end: '2024-12-31',
        val_start: '2025-01-02',
        val_end: '2025-06-30',
        test_start: '2025-07-01',
        test_end: '2025-12-31',
        target_horizon_days: 5,
        target_mode: 'return',
      },
      warnings: ['模型目录缺少 config.yaml，超参数按模型类型默认值带入'],
    });

    expect(config.configuration.selectedFeatures).toEqual(['f1', 'f2', 'f3']);
    expect(config.configuration.params.model_type).toBe('xgboost');
    expect(config.configuration.timePeriods.train).toEqual(['2018-01-02', '2024-12-31']);
    expect(config.configuration.target.horizonDays).toBe(5);
    expect(warnings.length).toBeGreaterThan(0);
  });

  it('产出必须能通过训练页 parseTrainingConfig 的校验', () => {
    const { config } = buildRetrainTrainingConfig({
      model_id: 'm1',
      display_name: DISPLAY_NAME,
      config: CONFIG_PAYLOAD,
    });

    const parsed = parseTrainingConfig(serializeTrainingConfig(config));

    expect(parsed.market).toBe('CN');
    expect(parsed.factorSource).toBe('l1_factors');
    expect(parsed.draft.selectedFeatures).toEqual(['mom_ret_5d', 'vol_std_20']);
    expect(parsed.draft.timePeriods.train).toEqual(['2016-01-04', '2025-10-17']);
    expect(parsed.draft.params.learning_rate).toBe(0.03);
    expect(parsed.draft.target.horizonDays).toBe(10);
  });

  it('不支持或缺失模型类型时明确报错', () => {
    expect(() => buildRetrainTrainingConfig({
      model_id: 'm3',
      config: { model: { type: 'hybrid_gru_tree' } },
    })).toThrow('模型类型不受支持');
  });

  it('stripSplitSuffix 只剥「 · 训…」段', () => {
    expect(stripSplitSuffix(DISPLAY_NAME)).toBe('CN 79特征全量(不筛选) T+10');
    expect(stripSplitSuffix('普通模型名')).toBe('普通模型名');
  });
});
