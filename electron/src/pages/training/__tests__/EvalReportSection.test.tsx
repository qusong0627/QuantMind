import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { EvalReportSection } from '../EvalReportSection';
import type { EvalReport } from '../trainingUtils';

/** 真实收益口径的最小报告：分层/多空/分段/分年齐全。 */
function realReturnReport(): EvalReport {
  return {
    return_basis: 'label_return',
    n_days: 1200,
    n_rows: 500000,
    groups: {
      n_groups: 10,
      mean_returns: [0.01, 0.02, 0.01, 0.02, 0.03, 0.02, 0.03, 0.04, 0.03, 0.05],
      monotonicity: 0.92,
    },
    long_short: {
      sharpe: 1.2,
      ann_return: 0.15,
      curve: [['2020-01-02', 0], ['2021-01-04', 0.12], ['2022-01-04', 0.35]],
    },
    by_split: {
      test: { mean: 0.105, icir: 1.303, win_rate: 0.887, t_stat: 3.2 },
      train: { mean: 0.1532, icir: 1.2039, win_rate: 0.83, t_stat: 5.1 },
      valid: { mean: 0.121, icir: 0.985, win_rate: 0.78, t_stat: 2.2 },
    },
    yearly: [
      { year: 2025, mean: 0.099, icir: 0.9 },
      { year: 2026, mean: 0.07, icir: 0.5 },
    ],
    tradability: { applied: true, excluded_rows: 42 },
  } as EvalReport;
}

/** 秩标签口径：曲线是秩空间累计，不能按净值展示。 */
function rankReport(): EvalReport {
  return {
    return_basis: 'label_rank',
    n_days: 300,
    n_rows: 12000,
    groups: { n_groups: 10, mean_returns: [0.02, 0.04, 0.03, 0.05, 0.06, 0.05, 0.07, 0.08, 0.07, 0.09] },
    long_short: { curve: [['2025-01-02', 0.05], ['2025-02-03', 368864560.29]] },
    by_split: { test: { mean: 0.09, icir: 0.7, win_rate: 0.6, t_stat: 1.4 } },
    yearly: [{ year: 2025, mean: 0.09, icir: 0.7 }],
    tradability: { applied: false, reason: 'label_rank basis' },
  } as EvalReport;
}

describe('EvalReportSection 模型评估报告（训练溯源专用）', () => {
  test('真实收益口径：展示分段对比、分年图与多空净值', () => {
    render(<EvalReportSection report={realReturnReport()} />);
    expect(screen.getByText('模型评估报告')).toBeInTheDocument();
    expect(screen.getByText('分段对比（IC / ICIR / 胜率）')).toBeInTheDocument();
    expect(screen.getByText('分年 IC（柱，%）/ ICIR（线，右轴）')).toBeInTheDocument();
    expect(screen.getByText(/多空组合累计净值/)).toBeInTheDocument();
    expect(screen.getByText(/已剔除次日涨跌停 42 行/)).toBeInTheDocument();
  });

  test('秩标签口径：不展示多空净值，给出秩口径说明', () => {
    render(<EvalReportSection report={rankReport()} />);
    expect(screen.getByText('秩标签口径')).toBeInTheDocument();
    expect(screen.queryByText(/多空组合累计净值/)).not.toBeInTheDocument();
    expect(screen.getByText(/十分位分层平均秩/)).toBeInTheDocument();
  });

  test('报告缺失或带 error 时不渲染', () => {
    const { container: emptyOne } = render(<EvalReportSection report={undefined} />);
    expect(emptyOne).toBeEmptyDOMElement();
    const { container: errorOne } = render(
      <EvalReportSection report={{ version: 1, error: 'empty predictions' } as EvalReport} />,
    );
    expect(errorOne).toBeEmptyDOMElement();
  });
});
