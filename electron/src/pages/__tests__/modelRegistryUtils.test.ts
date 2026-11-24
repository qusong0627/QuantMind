import { describe, expect, test } from 'vitest';
import type { UserModelRecord } from '../../services/modelTrainingService';
import { buildFallbackModelName, modelDisplayName, resolveSplitMetrics } from '../modelRegistryUtils';

function model(metadata: Record<string, unknown>, modelId = 'mdl_us_train_20260912134006_566f6da2_8d0709f2_mlp_84b33501'): UserModelRecord {
  return {
    tenant_id: 'default',
    user_id: '10000001',
    model_id: modelId,
    source_run_id: 'train_20260912134006_566f6da2',
    status: 'ready',
    storage_path: '',
    model_file: '',
    is_default: false,
    created_at: null,
    updated_at: null,
    activated_at: null,
    metadata_json: metadata,
    metrics_json: {},
  } as UserModelRecord;
}

describe('模型名兜底', () => {
  test('无 display_name 时用市场/算法/周期/特征拼可读名', () => {
    const m = model({ market: 'US', model_type: 'mlp', target_horizon_days: 5, feature_count: 167 });
    const name = modelDisplayName(m);
    expect(name).toContain('美股');
    expect(name).toContain('MLP');
    expect(name).toContain('T+5');
    expect(name).toContain('167特征');
    expect(name).not.toContain('mdl_us_train');
  });

  test('job_name=unnamed 视为无名字', () => {
    const m = model({ market: 'US', model_type: 'lightgbm', target_horizon_days: 5, feature_count: 167, job_name: 'unnamed' });
    expect(buildFallbackModelName(m)).toContain('LightGBM');
  });

  test('显式 display_name 保持原样（仅补算法前缀）', () => {
    const m = model({ market: 'CN', model_type: 'lightgbm', display_name: 'LightGBM T+5 全量' });
    expect(modelDisplayName(m)).toBe('LightGBM T+5 全量');
  });

  test('reversed 模型的原始 IC 展示时翻正', () => {
    const m = model({
      market: 'US',
      model_type: 'mlp',
      metrics: { val_ic: -0.02, test_ic: 0.04, val_rank_ic: -0.01, test_rank_ic: 0.05, score_direction: 'reversed' },
    });
    const splits = resolveSplitMetrics(m.metadata_json as Record<string, any>);
    expect(splits.valid.ic).toBeCloseTo(0.02);
    expect(splits.test.ic).toBeCloseTo(-0.04);
    expect(splits.test.rankIc).toBeCloseTo(-0.05);
  });
});
