/**
 * 模型治理面板的多选入口（P4）。
 *
 * 钉住：
 * 1) 勾选后工具条才出现；<2 个模型时「一键推理」「组合排名」都禁用（后端契约 min 2）；
 * 2) 组合排名把勾选的 model_id 交给父级（P5 用它切工作区并带种子）；
 * 3) 勾选框不触发「点行 → 单票研判」；
 * 4) 切市场清空选择，避免跨市场种子被带进补推/组合。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

vi.mock('../../../services/modelTrainingService', () => ({
  modelTrainingService: {
    previewBackfillJobs: vi.fn(),
    createBackfillJob: vi.fn(),
    getBackfillJob: vi.fn(),
    cancelBackfillJob: vi.fn(),
  },
}));

import { ModelGovernancePanel } from '../components/ModelGovernancePanel';
import { modelTrainingService } from '../../../services/modelTrainingService';
import type { UserModelRecord } from '../../../services/modelTrainingService';

const previewBackfillJobs = vi.mocked(modelTrainingService.previewBackfillJobs);

function record(modelId: string, displayName: string, horizon = 10): UserModelRecord {
  return {
    tenant_id: 't1',
    user_id: 'u1',
    model_id: modelId,
    source_run_id: `run-${modelId}`,
    status: 'ready',
    storage_path: `/models/${modelId}`,
    model_file: `${modelId}.txt`,
    market: 'CN',
    metadata_json: {
      model_type: 'lightgbm',
      display_name: displayName,
      target_horizon_days: horizon,
      target_mode: 'return',
      feature_count: 300,
      data_source: 'quantdb_factors',
      framework: 'lightgbm',
    },
    metrics_json: { test_rank_ic: 0.05 },
    is_default: false,
    asset_gaps: [],
    updated_at: '2026-10-20T00:00:00Z',
  };
}

const MODELS = [record('m1', '模型一'), record('m2', '模型二')];

/** 只取数据行的勾选框（表头「全选」不在 tr.ant-table-row 里） */
function rowCheckboxes(): HTMLInputElement[] {
  return Array.from(
    document.querySelectorAll<HTMLInputElement>('tr.ant-table-row input[type="checkbox"]'),
  );
}

async function selectRows(count: number, startAt = 0) {
  const boxes = rowCheckboxes();
  expect(boxes.length).toBeGreaterThanOrEqual(startAt + count);
  for (let i = startAt; i < startAt + count; i += 1) {
    // antd 的 input 被包在 label 里且命中层由 span 承担，userEvent 的
    // pointer-events 断言在 jsdom 下会误判；勾选语义只依赖 change 事件。
    fireEvent.click(boxes[i]);
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  previewBackfillJobs.mockResolvedValue({
    models: [],
    excluded: [],
    totals: { models: 0, eligible_models: 0, days: 0, estimated_seconds: 0 },
    warnings: [],
  });
});

describe('ModelGovernancePanel 多选', () => {
  it('未勾选不显示工具条；勾选 2 个后可一键推理与组合排名', async () => {
    const onOpenCombo = vi.fn();
    render(
      <ModelGovernancePanel
        models={MODELS}
        loading={false}
        marketLabel="A股"
        onOpenCombo={onOpenCombo}
      />,
    );

    expect(screen.queryByTestId('governance-selection-bar')).toBeNull();

    await selectRows(1);
    expect(screen.getByTestId('governance-selection-bar').textContent).toContain('已选 1 个模型');
    expect(screen.getByRole('button', { name: /一键推理至最新/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /组合排名/ })).toBeDisabled();

    await selectRows(1, 1);
    expect(screen.getByTestId('governance-selection-bar').textContent).toContain('已选 2 个模型');
    expect(screen.getByRole('button', { name: /一键推理至最新/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: /组合排名/ })).toBeEnabled();

    await userEvent.click(screen.getByRole('button', { name: /组合排名/ }));
    expect(onOpenCombo).toHaveBeenCalledWith(['m1', 'm2']);
  });

  it('勾选框不触发点行研判；点模型名仍进单票研判', async () => {
    const onInspect = vi.fn();
    render(
      <ModelGovernancePanel models={MODELS} loading={false} marketLabel="A股" onInspect={onInspect} />,
    );

    await selectRows(1);
    expect(onInspect).not.toHaveBeenCalled();

    await userEvent.click(screen.getByText('LightGBM - 模型一'));
    expect(onInspect).toHaveBeenCalledTimes(1);
    expect(onInspect.mock.calls[0][0].model_id).toBe('m1');
  });

  it('清除后工具条消失、选择归零', async () => {
    render(<ModelGovernancePanel models={MODELS} loading={false} marketLabel="A股" />);

    await selectRows(2);
    await userEvent.click(screen.getByRole('button', { name: /清除/ }));

    expect(screen.queryByTestId('governance-selection-bar')).toBeNull();
  });

  it('切换市场清空旧选择', async () => {
    const { rerender } = render(
      <ModelGovernancePanel models={MODELS} loading={false} marketLabel="A股" />,
    );
    await selectRows(2);
    expect(screen.getByTestId('governance-selection-bar')).toBeTruthy();

    rerender(<ModelGovernancePanel models={MODELS} loading={false} marketLabel="港股" />);
    await waitFor(() => expect(screen.queryByTestId('governance-selection-bar')).toBeNull());
  });

  it('一键推理打开补推弹窗并带上勾选模型', async () => {
    render(<ModelGovernancePanel models={MODELS} loading={false} marketLabel="A股" />);

    await selectRows(2);
    await userEvent.click(screen.getByRole('button', { name: /一键推理至最新/ }));

    await waitFor(() => expect(previewBackfillJobs).toHaveBeenCalledWith(['m1', 'm2']));
    expect(await screen.findByTestId('backfill-preview')).toBeTruthy();
  });
});
