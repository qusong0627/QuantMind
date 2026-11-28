import { beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { modelTrainingService, type UserModelRecord } from '../../services/modelTrainingService';
import type {
  SignalEvalAdmission,
  SignalEvalReport,
  SignalEvalState,
} from '../../services/modelSignalEvalTypes';
import { ModelCompareModal } from '../ModelCompareModal';

vi.mock('../../services/modelTrainingService', () => ({
  modelTrainingService: {
    getModelSignalEval: vi.fn(),
  },
}));

const mockedGet = vi.mocked(modelTrainingService.getModelSignalEval);

const admission: SignalEvalAdmission = {
  grade: 'green',
  checks: {},
  reasons: [],
  window_end: '2026-11-27',
  checked_at: '2026-11-28 10:00:00',
  stale: false,
  eval_version: 2,
};

function report(mean: number, excess: number): SignalEvalReport {
  return {
    status: 'ready',
    model_id: 'm',
    eval_version: 2,
    computed_at: '2026-11-28 10:00:00',
    window: {
      label: '近 63 个交易日',
      start: '2026-09-01',
      end: '2026-11-27',
      signal_days: 58,
      pred_end: '2026-11-27',
      horizon: 5,
    },
    sample: { rows: 1, symbols: 1, avg_names: 1 },
    metrics: {
      rank_ic: { n: 58, mean, std: 0.1, icir: 0.83, win_rate: 0.76, t: 5, hac_t: 3.91 },
      neutral_ic: {
        n: 58,
        mean: mean - 0.05,
        std: 0.1,
        icir: 0.7,
        win_rate: 0.7,
        t: 4,
        hac_t: 3.96,
      },
      decay: [],
      quantiles: {
        buckets: [],
        monotonicity: 1,
        spread_mean: 0.0344,
        spread_t: 4.69,
        spread_pos_rate: 0.8,
      },
      stability: { rank_autocorr_daily: 0.9, rank_autocorr_n: 58, top_overlap: {} },
    },
    series: { weekly_ic: [] },
    topn: [
      {
        n: 20,
        total_return: 0.0397,
        annualized: 0.1,
        mdd: -0.0846,
        sharpe: 1,
        excess,
        excess_t: 2,
        market_return: 0,
        avg_names: 20,
        capacity_p5: 1,
        capacity_p10: 2,
        cohorts: 58,
      },
    ],
    verdict: {
      ranking_pass: true,
      neutral_pass: true,
      decile_pass: true,
      long_pass: true,
      stability_pass: true,
      summary: '',
    },
    admission,
  };
}

function state(mean: number, excess: number): SignalEvalState {
  return {
    status: 'ready',
    report: report(mean, excess),
    admission,
    history: [],
    stale: false,
  };
}

function model(modelId: string, name: string, testRank: number): UserModelRecord {
  return {
    tenant_id: 'default',
    user_id: 'u1',
    model_id: modelId,
    source_run_id: '',
    status: 'ready',
    storage_path: '/tmp',
    model_file: 'model.lgb',
    metadata_json: {
      display_name: name,
      model_type: 'lightgbm',
      target_horizon_days: 5,
      market: 'CN',
      metrics: { test_rank_ic: testRank, test_rank_icir: 0.7 },
    },
    metrics_json: {},
    is_default: false,
  } as unknown as UserModelRecord;
}

beforeEach(() => {
  mockedGet.mockReset();
});

describe('ModelCompareModal 模型横向对比', () => {
  test('展示测试集 IC 与近 3 个月指标，并标出最佳值', async () => {
    const models = [
      model('m-a', '模型 A', 0.12),
      model('m-b', '模型 B', 0.15),
    ];
    mockedGet.mockImplementation((modelId) =>
      Promise.resolve(modelId === 'm-a' ? state(0.1355, 0.0805) : state(0.2, 0.12)),
    );

    render(
      <ModelCompareModal
        open
        models={models}
        favorites={[]}
        pinned={[]}
        onClose={() => undefined}
        onToggleFavorite={() => undefined}
        onTogglePin={() => undefined}
      />,
    );

    expect(await screen.findByText('0.1355')).toBeTruthy();
    expect(await screen.findByText('0.2000')).toBeTruthy();
    expect(screen.getAllByText('最佳').length).toBeGreaterThan(0);
    expect(screen.getByText('测试集 RankIC')).toBeTruthy();
    expect(screen.getByText('Top20 收益（近3月）')).toBeTruthy();
  });

  test('收藏按钮回调携带模型 ID', async () => {
    const onToggleFavorite = vi.fn();
    mockedGet.mockResolvedValue(state(0.1, 0.05));

    render(
      <ModelCompareModal
        open
        models={[model('m-a', '模型 A', 0.12)]}
        favorites={[]}
        pinned={[]}
        onClose={() => undefined}
        onToggleFavorite={onToggleFavorite}
        onTogglePin={() => undefined}
      />,
    );

    fireEvent.click(screen.getByLabelText('收藏'));
    expect(onToggleFavorite).toHaveBeenCalledWith('m-a');
  });

  test('默认窗口按模型列数放大，滚动区域可键盘聚焦', async () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1600 });
    try {
      const models = Array.from({ length: 5 }, (_, index) =>
        model(`m-${index}`, `模型 ${index}`, 0.1 + index * 0.01),
      );
      mockedGet.mockResolvedValue(state(0.1, 0.05));

      render(
        <ModelCompareModal
          open
          models={models}
          favorites={[]}
          pinned={[]}
          onClose={() => undefined}
          onToggleFavorite={() => undefined}
          onTogglePin={() => undefined}
        />,
      );

      await screen.findByText('测试集 RankIC');
      const win = screen.getByTestId('model-compare-window');
      // 208(指标列) + 5 × 236(模型列) + 32 = 1420，小于视口 94%（1504）
      expect(win.style.width).toBe('1420px');
      expect(document.querySelectorAll('th[data-compare-col]').length).toBe(5);

      const region = screen.getByRole('region', { name: /模型对比数据表/ });
      expect(region.getAttribute('tabindex')).toBe('0');
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
    }
  });

  test('冻结模型列后置顶显示并固定在指标列右侧', async () => {
    mockedGet.mockResolvedValue(state(0.1, 0.05));
    render(
      <ModelCompareModal
        open
        models={[model('m-a', '模型 A', 0.12), model('m-b', '模型 B', 0.15), model('m-c', '模型 C', 0.1)]}
        favorites={[]}
        pinned={[]}
        onClose={() => undefined}
        onToggleFavorite={() => undefined}
        onTogglePin={() => undefined}
      />,
    );

    await screen.findByText('测试集 RankIC');
    fireEvent.click(screen.getByLabelText(/^冻结 .*模型 B/));

    const headers = document.querySelectorAll('th[data-compare-col]');
    expect(headers[0]?.getAttribute('data-compare-col')).toBe('m-b');
    const frozenHeader = headers[0] as HTMLElement;
    expect(frozenHeader.className).toContain('sticky');
    // 指标列宽 208px，第一个冻结列就贴在它右边
    expect(frozenHeader.style.left).toBe('208px');

    fireEvent.click(screen.getByLabelText(/^取消冻结 .*模型 B/));
    expect(screen.queryByLabelText(/^取消冻结 .*模型 B/)).toBeNull();
    const firstAfterUnfreeze = document.querySelectorAll('th[data-compare-col]')[0] as HTMLElement;
    expect(firstAfterUnfreeze.style.left).toBe('');
  });

  test('最大化 / 还原窗口，缩放热区支持方向键调整', async () => {
    const originalWidth = window.innerWidth;
    const originalHeight = window.innerHeight;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1600 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
    try {
      mockedGet.mockResolvedValue(state(0.1, 0.05));
      render(
        <ModelCompareModal
          open
          models={[model('m-a', '模型 A', 0.12), model('m-b', '模型 B', 0.15)]}
          favorites={[]}
          pinned={[]}
          onClose={() => undefined}
          onToggleFavorite={() => undefined}
          onTogglePin={() => undefined}
        />,
      );

      await screen.findByText('测试集 RankIC');
      const win = screen.getByTestId('model-compare-window');
      expect(win.style.width).toBe('960px');
      expect(win.style.height).toBe('774px');

      fireEvent.click(screen.getByLabelText('最大化窗口'));
      expect(win.style.width).toBe('1576px');
      expect(win.style.height).toBe('876px');

      fireEvent.click(screen.getByLabelText('还原窗口'));
      expect(win.style.width).toBe('960px');

      fireEvent.keyDown(screen.getByLabelText(/调整对比窗口大小/), { key: 'ArrowRight' });
      expect(win.style.width).toBe('976px');
      fireEvent.keyDown(screen.getByLabelText(/调整对比窗口大小/), { key: 'ArrowDown' });
      expect(win.style.height).toBe('790px');
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalHeight });
    }
  });
});
