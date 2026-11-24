import { describe, expect, test } from 'vitest';
import {
  DEFAULT_CONTEXT,
  DEFAULT_PARAMS,
  DEFAULT_TARGET,
  DEFAULT_TIME_PERIODS,
  buildBackendTrainingPayload,
  buildTrainingRequest,
  type TrainingTarget,
} from '../training/trainingUtils';

const SPEC = {
  target_id: 'cn_ret_5d_open_rank',
  version: 1,
  market: 'CN',
  family: 'forward_return',
  mode: 'regression',
  horizon_days: 5,
  entry: { lag_days: 1, price: 'open' },
  exit: { price: 'close' },
  transform: { type: 'rank', top_pct: 0.2, drop_extreme_pct: 0 },
};

function payloadFor(target: TrainingTarget) {
  const request = buildTrainingRequest(
    [],
    [],
    DEFAULT_TIME_PERIODS,
    target,
    { ...DEFAULT_PARAMS, model_type: 'lightgbm', model_types: ['lightgbm'] },
    { ...DEFAULT_CONTEXT, market: 'CN' },
    'target-payload-test',
    'CN',
  );
  return buildBackendTrainingPayload(request, DEFAULT_TIME_PERIODS);
}

describe('training payload target contract', () => {
  test('legacy target sends no target_ref/overrides', () => {
    const payload = payloadFor({ ...DEFAULT_TARGET, targetRef: null, targetSpec: null });
    expect(payload.target_ref).toBeUndefined();
    expect(payload.label_overrides).toBeUndefined();
    expect(payload.target_horizon_days).toBe(DEFAULT_TARGET.horizonDays);
  });

  test('registry target sends ref and only diffs as overrides', () => {
    const unchanged: TrainingTarget = {
      ...DEFAULT_TARGET,
      horizonDays: 5,
      executionPrice: 'open',
      labelTransform: 'rank',
      labelTopPct: 0.2,
      labelDropExtremePct: 0,
      targetRef: { id: 'cn_ret_5d_open_rank', version: 1 },
      targetSpec: SPEC,
    };
    let payload = payloadFor(unchanged);
    expect(payload.target_ref).toEqual({ id: 'cn_ret_5d_open_rank', version: 1 });
    expect(payload.label_overrides).toBeUndefined();

    payload = payloadFor({ ...unchanged, horizonDays: 10 });
    expect(payload.label_overrides).toEqual({ horizon_days: 10 });

    payload = payloadFor({ ...unchanged, expectedTargetHash: 'a'.repeat(64) });
    expect(payload.expected_target_hash).toBe('a'.repeat(64));
  });
});
