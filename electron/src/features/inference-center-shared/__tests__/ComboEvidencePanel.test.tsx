/**
 * 证据与回放面板：只呈现 P1 产物，不现算。
 * 最重要的断言是「空推荐时如实说暂无推荐」，不能把基线包装成最优。
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { BlendEvidenceResponse, EvidenceCandidate } from '../../../services/blendTypes';
import { ComboEvidencePanel } from '../components/combo/ComboEvidencePanel';

function candidate(over: Partial<EvidenceCandidate> = {}): EvidenceCandidate {
  return {
    id: 'C0_full13_T10',
    name: '全13·T+10 等权',
    status: 'evaluated',
    verdict: 'not_recommended',
    horizon: 10,
    method: 'mean',
    members: ['LGB', 'XGB'],
    windows: {
      test: { days: 80, hit_exec_mean: 0.523, hit_theory_mean: 0.55, ret_exec_net_mean: 0.0045, n_entry_mean: 4.9 },
    },
    criteria: { delta_test: 0.0, delta_validation: 0.0 },
    bootstrap: { mean: 0.0, ci_low: -0.012, ci_high: 0.004, ci_width: 0.016, p_value: 1.0 },
    monthly: { win_ratio: 0.5 },
    bh_q: 1.0,
    gate: { performance: { annual_return: 0.1372, max_drawdown: -0.1538 } },
    leave_one_out: { max_delta: 0.0052, member: 'TAB' },
    failed_checks: ['min_hit_lift', 'bh_q'],
    ...over,
  };
}

function evidence(over: Partial<BlendEvidenceResponse> = {}): BlendEvidenceResponse {
  return {
    available: true,
    path: '/data/reports/combo_nextday_20261021/evidence.json',
    generated_at: '2026-10-21T02:00:00Z',
    recommendation: {
      ids: [],
      top: null,
      reason: '无候选同时满足提升幅度、置信区间、验证窗与闸门判据（K-R4）',
    },
    kill_criteria: {
      'K-R1_min_coverage_ratio': { threshold: 0.9, passed: true },
      'K-R2_max_ci_width': { threshold: 0.04, passed: false },
      'K-R4_no_recommendation': { enforced: true, passed: true },
    },
    splits: {
      selection: ['2025-02-05', '2025-12-31'],
      test: ['2026-05-06', '2026-08-26'],
    },
    candidates: [candidate()],
    caveats: ['检验窗仅 2026-05..08'],
    ...over,
  };
}

const baseProps = { loading: false, error: null, onRetry: vi.fn() };

describe('ComboEvidencePanel', () => {
  it('证据未生成时给空态，不显示任何指标', () => {
    render(
      <ComboEvidencePanel
        {...baseProps}
        evidence={{ available: false, path: '/data/reports/x/evidence.json' }}
      />,
    );
    expect(screen.getByText('证据未生成')).toBeInTheDocument();
    expect(screen.getByText(/不现算|不会现算/)).toBeInTheDocument();
    expect(screen.getByText('/data/reports/x/evidence.json')).toBeInTheDocument();
  });

  it('读取失败可重试', () => {
    const onRetry = vi.fn();
    render(<ComboEvidencePanel {...baseProps} evidence={null} error="读取失败" onRetry={onRetry} />);
    fireEvent.click(screen.getByText('重试'));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('无判定时如实展示「暂无达到证据标准的组合」与判据理由', () => {
    render(<ComboEvidencePanel {...baseProps} evidence={evidence()} />);
    expect(screen.getByText('研究结论：暂无达到证据标准的组合')).toBeInTheDocument();
    expect(screen.getAllByText(/K-R4/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/达到证据标准：/)).not.toBeInTheDocument();
  });

  it('有判定时抬出组合 id，不隐藏来源', () => {
    render(
      <ComboEvidencePanel
        {...baseProps}
        evidence={evidence({
          recommendation: { ids: ['C3_tree_t10'], top: 'C3_tree_t10', reason: '检验窗 Δ 显著' },
        })}
      />,
    );
    expect(screen.getByText('达到证据标准：C3_tree_t10')).toBeInTheDocument();
  });

  it('判据标签区分 通过 / 未过 / 已执行', () => {
    render(<ComboEvidencePanel {...baseProps} evidence={evidence()} />);
    expect(screen.getByText('K-R1_min_coverage_ratio 通过')).toBeInTheDocument();
    expect(screen.getByText('K-R2_max_ci_width 未过')).toBeInTheDocument();
    expect(screen.getByText('K-R4_no_recommendation 已执行')).toBeInTheDocument();
  });

  it('候选行按百分比/差值格式显示指标，缺失为 —', () => {
    render(<ComboEvidencePanel {...baseProps} evidence={evidence()} />);
    expect(screen.getByText('全13·T+10 等权')).toBeInTheDocument();
    expect(screen.getByText('未达证据标准')).toBeInTheDocument();
    expect(screen.getByText('52.3%')).toBeInTheDocument();
    expect(screen.getByText('[-1.20 pp, +0.40 pp]')).toBeInTheDocument();
    expect(screen.getByText('13.7% / -15.4%')).toBeInTheDocument();
    expect(screen.getByText(/检验窗仅 2026-05\.\.08/)).toBeInTheDocument();
  });

  it('展开候选显示窗口明细与未通过判据', () => {
    const { container } = render(<ComboEvidencePanel {...baseProps} evidence={evidence()} />);
    fireEvent.click(container.querySelector('.ant-table-row-expand-icon')!);
    expect(screen.getByTestId('combo-evidence-window-detail')).toBeInTheDocument();
    expect(screen.getAllByText('检验窗').length).toBeGreaterThan(0);
    expect(screen.getByText('min_hit_lift')).toBeInTheDocument();
  });
});
