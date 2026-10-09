import { describe, expect, test } from 'vitest';
import {
  classifyVerdict,
  describeDiagnosticReason,
  describeFusionWarning,
  ensembleMemberIds,
  formatMetric,
  formatWeightPct,
  isEnsembleModel,
} from '../fusionUtils';
import type { UserModelRecord } from '../../services/modelTrainingService';

function modelWithMeta(meta: Record<string, unknown>): UserModelRecord {
  return {
    tenant_id: 'default',
    user_id: '1',
    model_id: 'mdl_x',
    source_run_id: '',
    status: 'ready',
    storage_path: '',
    model_file: '',
    metadata_json: meta,
    metrics_json: {},
    is_default: false,
  };
}

describe('describeFusionWarning', () => {
  test('映射后端已知码为机构用语', () => {
    expect(describeFusionWarning('all_insufficient_days_fallback_equal')).toContain('等权');
    expect(describeFusionWarning('all_icir_nonpositive_fallback_equal')).toContain('ICIR');
    expect(describeFusionWarning('horizon_mismatch: [3, 5]')).toContain('[3, 5]');
    expect(describeFusionWarning('evidence_unavailable: boom')).toContain('boom');
    expect(describeFusionWarning('replay_failed: boom')).toContain('boom');
  });

  test('中文自由文本与未知码原样透出，绝不编造', () => {
    expect(describeFusionWarning('成员 mdl_a 窗口内无分数桶数据')).toBe(
      '成员 mdl_a 窗口内无分数桶数据',
    );
    expect(describeFusionWarning('some_new_code')).toBe('some_new_code');
    expect(describeFusionWarning('')).toBe('');
  });
});

describe('describeDiagnosticReason', () => {
  test('三类剔除/降权原因有名有姓，空串无徽标', () => {
    expect(describeDiagnosticReason('insufficient_days')).toBe('样本不足');
    expect(describeDiagnosticReason('nonpositive_icir')).toBe('ICIR≤0');
    expect(describeDiagnosticReason('duplicate')).toBe('近似重复');
    expect(describeDiagnosticReason('')).toBe('');
    expect(describeDiagnosticReason('mystery')).toBe('');
  });
});

describe('数值格式化：缺失一律 —，绝不显示成 0', () => {
  test('formatWeightPct', () => {
    expect(formatWeightPct(0.5)).toBe('50.0%');
    expect(formatWeightPct(0)).toBe('0.0%');
    expect(formatWeightPct(null)).toBe('—');
    expect(formatWeightPct(undefined)).toBe('—');
    expect(formatWeightPct(Number.NaN)).toBe('—');
  });

  test('formatMetric', () => {
    expect(formatMetric(0.61, 3)).toBe('0.610');
    expect(formatMetric(0)).toBe('0.000');
    expect(formatMetric(null)).toBe('—');
    expect(formatMetric(Number.POSITIVE_INFINITY)).toBe('—');
  });
});

describe('classifyVerdict', () => {
  test('分级：优于最优 / 仅优于中位 / 低于中位 / 证据不足', () => {
    const base = {
      fused_icir: 0.6,
      fused_ic_days: 30,
      best_member_icir: 0.5,
      median_member_icir: 0.4,
    };
    expect(classifyVerdict({ ...base, beats_best: true, beats_median: true })).toBe('beats_best');
    expect(classifyVerdict({ ...base, beats_best: false, beats_median: true })).toBe('beats_median');
    expect(classifyVerdict({ ...base, beats_best: false, beats_median: false })).toBe('below_median');
    expect(classifyVerdict(null)).toBe('unknown');
  });
});

describe('ensemble 识别', () => {
  test('is_ensemble 标志与 model_type=ensemble 双口径', () => {
    expect(isEnsembleModel(modelWithMeta({ is_ensemble: true }))).toBe(true);
    expect(isEnsembleModel(modelWithMeta({ model_type: 'ensemble' }))).toBe(true);
    expect(isEnsembleModel(modelWithMeta({ model_type: 'lightgbm' }))).toBe(false);
  });

  test('ensembleMemberIds 容错非数组', () => {
    expect(ensembleMemberIds(modelWithMeta({ source_model_ids: ['a', 'b'] }))).toEqual(['a', 'b']);
    expect(ensembleMemberIds(modelWithMeta({ source_model_ids: 'x' }))).toEqual([]);
    expect(ensembleMemberIds(modelWithMeta({}))).toEqual([]);
  });
});
