import { describe, expect, test } from 'vitest';
import {
  applyTargetSpecToTarget,
  buildTargetOverrides,
  capabilityModelTypes,
  findTargetByKey,
  intersectModelCapabilities,
  supportedTargetModes,
  targetFormLocks,
  targetModeUnsupportedReason,
  targetRefKey,
  type LabelTargetSummary,
  type TrainingCapabilities,
} from '../training/trainingTargetCapabilities';
import { DEFAULT_TARGET, type TrainingTarget } from '../training/trainingUtils';

const CAPS: TrainingCapabilities = {
  matrix_version: 1,
  models: [
    {
      model_type: 'lightgbm', family: 'gbdt', regression: true, classification: true,
      quantile: true, rank_loss: true, sample_weight: true, group_by_date: true, sequence: false,
    },
    {
      model_type: 'gru', family: 'qlib_dl_ts', regression: true, classification: false,
      quantile: false, rank_loss: false, sample_weight: false, group_by_date: false, sequence: true,
    },
    {
      model_type: 'mlp', family: 'sklearn_neural', regression: true, classification: true,
      quantile: false, rank_loss: false, sample_weight: false, group_by_date: false, sequence: false,
    },
  ],
  target_modes: {
    return: ['lightgbm', 'gru', 'mlp'],
    classification: ['lightgbm', 'mlp'],
    quantile: ['lightgbm'],
  },
};

const TARGET: LabelTargetSummary = {
  id: 'cn_ret_5d_open_rank',
  version: 1,
  market: 'CN',
  family: 'forward_return',
  mode: 'regression',
  status: 'active',
  spec_hash: 'abc123',
  spec: {
    target_id: 'cn_ret_5d_open_rank',
    version: 1,
    market: 'CN',
    family: 'forward_return',
    mode: 'regression',
    horizon_days: 5,
    entry: { lag_days: 1, price: 'open' },
    exit: { price: 'close' },
    transform: { type: 'rank', top_pct: 0.2, drop_extreme_pct: 0 },
  },
};

describe('training target capabilities', () => {
  test('intersection disables classification for GRU', () => {
    const view = intersectModelCapabilities(['lightgbm', 'gru'], CAPS);
    expect(view.regression).toBe(true);
    expect(view.classification).toBe(false);
    expect(view.sample_weight).toBe(false);
  });

  test('supported modes follow intersection', () => {
    expect(supportedTargetModes(['lightgbm'], CAPS)).toEqual(['return', 'classification']);
    expect(supportedTargetModes(['gru'], CAPS)).toEqual(['return']);
    expect(supportedTargetModes(['lightgbm', 'mlp'], CAPS)).toEqual(['return', 'classification']);
  });

  test('unsupported reason names the offending model', () => {
    expect(targetModeUnsupportedReason(['lightgbm', 'gru'], 'classification', CAPS)).toContain('gru');
    expect(targetModeUnsupportedReason(['lightgbm'], 'classification', CAPS)).toBeNull();
  });

  test('missing capabilities fall back to all modes', () => {
    expect(supportedTargetModes(['gru'], null)).toEqual(['return', 'classification']);
  });

  test('applyTargetSpecToTarget copies frozen fields', () => {
    const applied = applyTargetSpecToTarget(DEFAULT_TARGET, TARGET.spec);
    expect(applied.mode).toBe('return');
    expect(applied.horizonDays).toBe(5);
    expect(applied.executionPrice).toBe('open');
    expect(applied.labelTransform).toBe('rank');
    expect(applied.labelDropExtremePct).toBe(0);
  });

  test('buildTargetOverrides only sends real diffs', () => {
    const unchanged: TrainingTarget = {
      ...DEFAULT_TARGET,
      horizonDays: 5,
      executionPrice: 'open',
      labelTransform: 'rank',
      labelTopPct: 0.2,
      labelDropExtremePct: 0,
    };
    expect(buildTargetOverrides(unchanged, TARGET.spec)).toBeUndefined();
    const changed: TrainingTarget = { ...unchanged, horizonDays: 10, labelTransform: 'zscore' };
    expect(buildTargetOverrides(changed, TARGET.spec)).toEqual({
      horizon_days: 10,
      label_transform: 'zscore',
    });
  });

  test('quantile targets allow no overrides', () => {
    const quantileSpec = {
      ...TARGET.spec,
      family: 'quantile_return',
      mode: 'quantile',
      transform: { type: 'raw', top_pct: 0.2, drop_extreme_pct: 0 },
    };
    expect(buildTargetOverrides({ ...DEFAULT_TARGET, horizonDays: 10 }, quantileSpec)).toBeUndefined();
    expect(targetFormLocks(quantileSpec)).toMatchObject({
      lockHorizon: true,
      lockTransform: true,
      hideTopPct: true,
    });
  });

  test('classification targets only allow horizon overrides', () => {
    const clsSpec = {
      ...TARGET.spec,
      family: 'classification_binary',
      mode: 'classification',
      transform: { type: 'rank', top_pct: 0.2, drop_extreme_pct: 0 },
    };
    expect(buildTargetOverrides({ ...DEFAULT_TARGET, labelTransform: 'zscore' }, clsSpec)).toBeUndefined();
    expect(buildTargetOverrides({ ...DEFAULT_TARGET, horizonDays: 3 }, clsSpec)).toEqual({
      horizon_days: 3,
    });
    expect(targetFormLocks(clsSpec).hideDropExtreme).toBe(true);
  });

  test('target ref helpers', () => {
    expect(targetRefKey(null)).toBe('legacy');
    expect(targetRefKey({ id: 'a', version: 2 })).toBe('a@2');
    expect(findTargetByKey([TARGET], 'cn_ret_5d_open_rank@1')).toBe(TARGET);
    expect(findTargetByKey([TARGET], 'legacy')).toBeNull();
  });

  test('capabilityModelTypes handles single and multi', () => {
    expect(capabilityModelTypes({ model_type: 'gru' })).toEqual(['gru']);
    expect(capabilityModelTypes({ model_type: 'gru', model_types: ['lightgbm', 'gru'] })).toEqual([
      'lightgbm',
      'gru',
    ]);
  });
});
