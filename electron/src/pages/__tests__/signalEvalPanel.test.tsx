import { beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { modelTrainingService } from '../../services/modelTrainingService';
import type {
  SignalEvalAdmission,
  SignalEvalReport,
} from '../../services/modelSignalEvalTypes';
import { SignalEvalPanel } from '../SignalEvalPanel';

vi.mock('../../services/modelTrainingService', () => ({
  modelTrainingService: {
    getModelSignalEval: vi.fn(),
    refreshModelSignalEval: vi.fn(),
  },
}));

const mockedGet = vi.mocked(modelTrainingService.getModelSignalEval);
const mockedRefresh = vi.mocked(modelTrainingService.refreshModelSignalEval);

function sampleAdmission(): SignalEvalAdmission {
  return {
    grade: 'yellow',
    checks: { ranking: true, neutral: true, decile: true, long: false, stability: true },
    reasons: ['TopN 长多超额'],
    window_end: '2026-11-13',
    checked_at: '2026-11-24 10:00:00',
    stale: false,
    eval_version: 2,
  };
}

function sampleReport(): SignalEvalReport {
  return {
    status: 'ready',
    model_id: 'mdl_signal',
    eval_version: 2,
    computed_at: '2026-11-24 10:00:00',
    window: {
      label: '近 63 个交易日',
      start: '2026-08-11',
      end: '2026-11-13',
      signal_days: 63,
      pred_end: '2026-11-20',
      pred_lag_days: 5,
      horizon: 5,
      independent_periods: 12,
    },
    sample: { rows: 306185, symbols: 5027, avg_names: 4860 },
    coverage: { avg: 0.94, min: 0.71, days: 63, days_below_50: 0, low_days: [] },
    metrics: {
      rank_ic: { n: 63, mean: 0.1355, std: 0.16, icir: 0.81, win_rate: 0.78, t: 6.4, hac_t: 3.4 },
      neutral_ic: { n: 63, mean: 0.1039, std: 0.12, icir: 0.84, win_rate: 0.81, t: 6.6, hac_t: 3.57 },
      decay: [{ horizon: 1, n_days: 63, mean_ic: 0.08, t: 5 }],
      quantiles: {
        buckets: Array.from({ length: 10 }, (_, i) => -0.005 + i * 0.002),
        monotonicity: 0.78,
        spread_mean: 0.0238,
        spread_t: 4,
        spread_pos_rate: 0.7,
      },
      stability: {
        rank_autocorr_daily: 0.9,
        rank_autocorr_n: 62,
        top_overlap: { '20': 0.19, '50': 0.24, '100': 0.3 },
      },
    },
    series: { weekly_ic: [{ week: '08-11', ic: 0.12, ic_neutral: 0.1 }] },
    topn: [
      {
        n: 20,
        total_return: -0.31017,
        annualized: -0.8,
        mdd: -0.3553,
        sharpe: -0.5,
        excess: -0.10137,
        excess_t: -0.62,
        excess_hs300: -0.12,
        excess_sse: -0.11,
        excess_szse: -0.1,
        excess_zz1000: -0.08,
        market_return: -0.2,
        avg_names: 19.25,
        capacity_p5: 21200750,
        capacity_p10: 42401500,
        cohorts: 63,
      },
      {
        n: 50,
        total_return: -0.28657,
        annualized: -0.75,
        mdd: -0.3358,
        sharpe: -0.45,
        excess: -0.0777,
        excess_t: -0.5,
        market_return: -0.2,
        avg_names: 48.1,
        capacity_p5: 50363425,
        capacity_p10: 100000000,
        cohorts: 63,
      },
      {
        n: 100,
        total_return: -0.2816,
        annualized: -0.7,
        mdd: -0.3238,
        sharpe: -0.4,
        excess: -0.0728,
        excess_t: -0.54,
        market_return: -0.2,
        avg_names: 96.6,
        capacity_p5: 99609840,
        capacity_p10: 199000000,
        cohorts: 63,
      },
    ],
    verdict: {
      ranking_pass: true,
      neutral_pass: true,
      decile_pass: true,
      long_pass: false,
      stability_pass: true,
      summary: '排序有效，但长多没跑赢打分池：先当选股输入/风险分层，不直接当多头。',
    },
    admission: sampleAdmission(),
  };
}

beforeEach(() => {
  mockedGet.mockReset();
  mockedRefresh.mockReset();
  mockedRefresh.mockResolvedValue({ status: 'running' });
});

describe('SignalEvalPanel 测试集实盘力', () => {
  test('ready 态展示排序、分位、TopN 与判读', async () => {
    mockedGet.mockResolvedValue({
      status: 'ready',
      report: sampleReport(),
      admission: sampleAdmission(),
      history: [],
      stale: false,
    });
    render(<SignalEvalPanel modelId="mdl_signal" />);

    expect(await screen.findByText('排序有效 · 长多未过线')).toBeInTheDocument();
    expect(screen.getByText('0.1355')).toBeInTheDocument();
    expect(screen.getByText('0.1039')).toBeInTheDocument();
    expect(screen.getByText('+2.38%')).toBeInTheDocument();
    expect(screen.getAllByText('Top20').length).toBeGreaterThan(0);
    expect(screen.getAllByText('-31.02%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('-10.14%').length).toBeGreaterThan(0);
    expect(screen.getByText('-8.00%')).toBeInTheDocument();
    expect(screen.getByText('沪深300')).toBeInTheDocument();
    expect(screen.getByText('上证')).toBeInTheDocument();
    expect(screen.getByText('深证')).toBeInTheDocument();
    expect(screen.getByText('中证1000')).toBeInTheDocument();
    expect(screen.getByText('-12.00%')).toBeInTheDocument();
    expect(screen.getByText('-11.00%')).toBeInTheDocument();
    expect(screen.getByText('-10.00%')).toBeInTheDocument();
    expect(screen.getByText(/长多没跑赢打分池/)).toBeInTheDocument();
    expect(screen.getByText('评估黄色预警')).toBeInTheDocument();
    expect(screen.getByText(/历史不足（当前 0 次）/)).toBeInTheDocument();
  });

  test('有历史快照时展示滚动趋势', async () => {
    const history = [
      {
        computed_at: '2026-11-21 16:50:00',
        window_start: '2026-08-10',
        window_end: '2026-11-12',
        signal_days: 63,
        rank_ic: 0.12,
        neutral_ic: 0.09,
        icir: 0.8,
        spread: 0.01,
        monotonicity: 0.8,
        top20_return: -0.2,
        top20_excess: -0.05,
        top50_excess: -0.04,
        stability: 0.9,
        grade: 'yellow' as const,
        long_pass: false,
      },
      {
        computed_at: '2026-11-22 16:50:00',
        window_start: '2026-08-11',
        window_end: '2026-11-13',
        signal_days: 63,
        rank_ic: 0.1355,
        neutral_ic: 0.1039,
        icir: 0.81,
        spread: 0.0238,
        monotonicity: 0.78,
        top20_return: -0.31,
        top20_excess: -0.1014,
        top50_excess: -0.0778,
        stability: 0.9,
        grade: 'yellow' as const,
        long_pass: false,
      },
    ];
    mockedGet.mockResolvedValue({
      status: 'ready',
      report: sampleReport(),
      admission: sampleAdmission(),
      history,
      stale: false,
    });
    render(<SignalEvalPanel modelId="mdl_signal" />);

    expect(await screen.findByText('最近 2 次快照')).toBeInTheDocument();
  });

  test('缺缓存时自动发起刷新并展示结果', async () => {
    let calls = 0;
    mockedGet.mockImplementation(async () => {
      calls += 1;
      return calls === 1
        ? {
            status: 'missing',
            stale: true,
            admission: {
              grade: 'unknown',
              checks: {},
              reasons: ['未完成标准化评估'],
              window_end: null,
              checked_at: null,
              stale: true,
              eval_version: 2,
            },
            history: [],
          }
        : {
            status: 'ready',
            report: sampleReport(),
            admission: sampleAdmission(),
            history: [],
            stale: false,
          };
    });
    render(<SignalEvalPanel modelId="mdl_signal" />);

    await waitFor(() => expect(mockedRefresh).toHaveBeenCalledWith('mdl_signal'));
    expect(await screen.findByText('排序有效 · 长多未过线')).toBeInTheDocument();
  });

  test('running 态展示进度', async () => {
    mockedGet.mockResolvedValue({
      status: 'running',
      progress: 42,
      step: 'ic',
      message: '',
      admission: sampleAdmission(),
      history: [],
    });
    render(<SignalEvalPanel modelId="mdl_signal" />);

    expect(await screen.findByText('刷新中 42%')).toBeInTheDocument();
    expect(screen.getByText(/截面 IC · 42%/)).toBeInTheDocument();
  });

  test('不可评估时说明原因', async () => {
    mockedGet.mockResolvedValue({
      status: 'unavailable',
      reason: '当前仅支持 A 股模型（market=HK）',
    });
    render(<SignalEvalPanel modelId="mdl_signal" />);

    expect(await screen.findByText('当前仅支持 A 股模型（market=HK）')).toBeInTheDocument();
    expect(mockedRefresh).not.toHaveBeenCalled();
  });
});
