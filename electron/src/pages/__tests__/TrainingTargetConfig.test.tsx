import { describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import dayjs from 'dayjs';
import { TrainingTargetConfig } from '../training/TrainingTargetConfig';
import { DEFAULT_TARGET, type TrainingTarget, type TimePeriodMap } from '../training/trainingUtils';
import type {
  LabelTargetSummary,
  TrainingCapabilities,
} from '../training/trainingTargetCapabilities';

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
  ],
  target_modes: {
    return: ['lightgbm', 'gru'],
    classification: ['lightgbm'],
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
  spec_hash: 'abc12345',
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

const PERIODS: TimePeriodMap = {
  train: [dayjs('2023-01-01'), dayjs('2023-12-31')],
  val: [dayjs('2024-01-01'), dayjs('2024-06-30')],
  test: [dayjs('2024-07-01'), dayjs('2024-12-31')],
};

function renderConfig(
  props: Partial<React.ComponentProps<typeof TrainingTargetConfig>> = {},
) {
  const onTargetChange = vi.fn();
  render(
    <TrainingTargetConfig
      target={DEFAULT_TARGET}
      timePeriods={PERIODS}
      onTargetChange={onTargetChange}
      onTimeChange={vi.fn()}
      modelTypes={['lightgbm']}
      capabilities={CAPS}
      availableTargets={[TARGET]}
      {...props}
    />,
  );
  return { onTargetChange };
}

describe('TrainingTargetConfig target contract', () => {
  test('GRU 选择时分类目标不可用并显示原因', () => {
    renderConfig({ modelTypes: ['gru'] });
    expect(screen.getByText(/gru 不支持分类目标/)).toBeInTheDocument();
  });

  test('选择注册表目标后回传冻结 ref/spec/horizon', async () => {
    const user = userEvent.setup();
    const { onTargetChange } = renderConfig();
    await user.click(screen.getByText('自定义（legacy 平铺口径）'));
    await user.click(screen.getByText('cn_ret_5d_open_rank @v1 · 收益回归'));
    expect(onTargetChange).toHaveBeenCalledWith(
      expect.objectContaining({
        targetRef: { id: 'cn_ret_5d_open_rank', version: 1 },
        targetSpec: TARGET.spec,
        horizonDays: 5,
      }),
    );
  });

  test('分类目标隐藏截面变换与极端清洗', () => {
    const target: TrainingTarget = {
      ...DEFAULT_TARGET,
      mode: 'classification',
      labelTransform: 'rank',
    };
    renderConfig({ target });
    expect(screen.queryByText('标签截面变换')).not.toBeInTheDocument();
    expect(screen.getByText(/分类目标不做截面变换/)).toBeInTheDocument();
    expect(screen.queryByText('极端标签清洗（仅训练段）')).not.toBeInTheDocument();
  });

  test('size_excess 变换提示 total_cap 前置条件', () => {
    const target: TrainingTarget = {
      ...DEFAULT_TARGET,
      labelTransform: 'size_excess_rank',
    };
    renderConfig({ target });
    expect(screen.getByText(/需要特征列表包含 total_cap/)).toBeInTheDocument();
  });

  test('注册表目标展示 hash 与冻结口径提示', () => {
    const target: TrainingTarget = {
      ...DEFAULT_TARGET,
      targetRef: { id: TARGET.id, version: TARGET.version },
      targetSpec: TARGET.spec,
      horizonDays: 5,
    };
    renderConfig({ target });
    expect(screen.getByText(/hash abc12345/)).toBeInTheDocument();
    expect(screen.getByText(/冻结口径/)).toBeInTheDocument();
  });
});
