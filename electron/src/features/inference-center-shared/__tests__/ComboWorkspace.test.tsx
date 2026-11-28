/**
 * 组合选股工作区：状态编排的边界都在这里——
 * 空列表自动建 C0、模板只能预览、滚动/计划需要已保存组合、无评分日不给假榜单。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  BlendPeriodResponse,
  BlendRankingResponse,
  BlendRecord,
  BlendRollingResponse,
  ComboAutoCandidatesResponse,
  ComboTemplate,
} from '../../../services/blendTypes';

const mocks = vi.hoisted(() => ({
  listBlends: vi.fn(),
  createBlend: vi.fn(),
  patchBlend: vi.fn(),
  archiveBlend: vi.fn(),
  getEligible: vi.fn(),
  getAutoCandidates: vi.fn(),
  preview: vi.fn(),
  getDates: vi.fn(),
  getBounds: vi.fn(),
  getRanking: vi.fn(),
  getRolling: vi.fn(),
  getPeriod: vi.fn(),
  createPlan: vi.fn(),
  listPlans: vi.fn(),
  confirmPlan: vi.fn(),
  getEvidence: vi.fn(),
}));

vi.mock('../../../services/blendService', () => ({
  blendService: mocks,
  blendErrorMessage: (err: unknown) => (err instanceof Error ? err.message : '请求失败'),
}));

import { ComboWorkspace } from '../components/combo/ComboWorkspace';

function blend(over: Partial<BlendRecord> = {}): BlendRecord {
  return {
    blend_id: 'blend-1',
    tenant_id: 'default',
    user_id: 'u1',
    name: '全13·T+10 等权',
    market: 'CN',
    purpose: 'rebalance',
    source: 'system',
    template_id: 'tpl-base',
    horizon_days: 10,
    weighting: 'equal',
    method: 'mean',
    trim_count: 2,
    min_coverage: 2,
    consensus_min_count: 0,
    entry_per_day: 5,
    rolling_window: 10,
    target_positions: 50,
    feature_signature: 'sig',
    data_source: 'quantdb_factors',
    status: 'active',
    created_at: null,
    updated_at: null,
    members: [
      { model_id: 'm1', model_source: 'manual', weight: 1 },
      { model_id: 'm2', model_source: 'manual', weight: 1 },
    ],
    ...over,
  };
}

function template(over: Partial<ComboTemplate> = {}): ComboTemplate {
  return {
    template_id: 'tpl-base',
    kind: 'baseline',
    name: '全13·T+10 等权',
    market: 'CN',
    horizon_days: 10,
    method: 'mean',
    trim_count: 2,
    min_coverage: 2,
    consensus_min_count: 0,
    members: [
      { model_id: 'm1', model_source: 'manual', weight: 1, model_type: 'lightgbm', display_name: 'LGB' },
      { model_id: 'm2', model_source: 'manual', weight: 1, model_type: 'xgboost', display_name: 'XGB' },
    ],
    evidence: { grade: 'watch', candidate_id: null },
    baseline: true,
    ...over,
  };
}

function autoCandidates(): ComboAutoCandidatesResponse {
  return {
    market: 'CN',
    items: [template()],
    baseline_template_id: 'tpl-base',
    evidence_available: true,
    evidence_generated_at: '2026-10-21T02:00:00Z',
    excluded: [],
    warnings: [],
  };
}

function ranking(): BlendRankingResponse {
  return {
    data_trade_date: '2026-10-20',
    members: [],
    warnings: [],
    ranking: [
      {
        code: 'SH600000', symbol: 'SH600000', name: '浦发银行', S: 0.9, S_pct: 0.95, score: 0.9, pct: 0.95,
        rank: 1, research_score: 95, position: 'BUY', coverage: 13, dispersion: 0.01,
        dispersion_band: 'low', contributions: {}, missing_members: [],
      },
    ],
  };
}

function rolling(): BlendRollingResponse {
  return {
    blend_id: 'blend-1',
    data_trade_date: '2026-10-20',
    target_date: '2026-10-21',
    window: 10,
    per_day: 5,
    days: ['2026-10-20'],
    columns: [
      {
        date: '2026-10-20',
        picks: [
          { symbol: 'SH600001', code: 'SH600001', name: '甲', score: 0.5, rank: 1, row_rank: 1, coverage: 13, pct: 0.9, position: 'BUY', held_days: 0, is_new: true },
        ],
        exits: [],
      },
    ],
    members: [
      { model_id: 'm1', display_name: 'LGB', model_source: 'manual', model_type: 'lightgbm', horizon_days: 10, weight: 1, available: true },
    ],
    summary: { unique_symbols: 1, new_today: 1, exited_today: 0 },
    warnings: [],
  };
}

function period(): BlendPeriodResponse {
  return {
    blend_id: 'blend-1',
    requested: { start: '2026-04-22', end: '2026-10-19', capital: 10000000, positions: 50 },
    effective: {
      start: '2026-08-27',
      end: '2026-10-19',
      first_scoring_day: '2026-08-27',
      last_scoring_day: '2026-10-19',
    },
    template: { entry_per_day: 5, hold_days: 10, target_positions: 50, method: 'mean' },
    summary: {
      initial_capital: 10000000,
      final_equity: 11234000,
      total_return_pct: 12.34,
      profit_amount: 1234000,
      realized_pnl_amount: 900000,
      unrealized_pnl_amount: 334000,
      max_drawdown_pct: -3.21,
      trading_days: 32,
      scoring_days: 32,
      entries: 160,
      exits: 110,
      closed_trades: 110,
      open_positions: 50,
      wins: 60,
      losses: 50,
      flats: 0,
      win_rate_pct: 54.55,
      avg_trade_return_pct: 0.88,
      avg_positions: 42.97,
      entry_convention: 'close',
      fee_convention: 'none',
      data_quality: 'complete',
      scoring_coverage_pct: 100,
    },
    months: [
      {
        month: '2026-09',
        start_date: '2026-09-01',
        end_date: '2026-09-30',
        start_equity: 10000000,
        end_equity: 11234000,
        return_pct: 12.34,
        profit_amount: 1234000,
        entries: 105,
        exits: 70,
        closed_trades: 70,
        month_end_positions: 50,
        win_rate_pct: 54.55,
        partial: false,
      },
    ],
    daily: [
      { date: '2026-09-01', equity: 10000000, cash: 5000000, positions: 40, entries: 5, exits: 5, return_pct: null },
      { date: '2026-09-30', equity: 11234000, cash: 4000000, positions: 50, entries: 3, exits: 2, return_pct: 1.2 },
    ],
    trades: [
      {
        symbol: 'SZ002790', code: 'SZ002790', name: '瑞尔特',
        entry_date: '2026-08-27', exit_date: '2026-09-10',
        entry_price: 5.99, exit_price: 8.46, shares: 33389.99, notional: 200000,
        return_pct: 41.24, pnl_amount: 82470.78, held_days: 10,
        exit_reason: '到期', deferred: false,
      },
    ],
    open: [],
    warnings: [],
    members: [],
  };
}

function renderWorkspace() {
  return render(<ComboWorkspace market="CN" marketLabel="A股市场" />);
}

beforeEach(() => {
  Object.values(mocks).forEach((fn) => fn.mockReset());
  mocks.listBlends.mockResolvedValue([blend()]);
  mocks.getAutoCandidates.mockResolvedValue(autoCandidates());
  mocks.createBlend.mockResolvedValue(blend({ blend_id: 'blend-new' }));
  mocks.getDates.mockResolvedValue({ blend_id: 'blend-1', dates: ['2026-10-20', '2026-10-19'] });
  mocks.getBounds.mockResolvedValue({
    blend_id: 'blend-1',
    source: 'pred',
    pred: {
      start: '2025-01-02',
      end: '2026-10-19',
      trading_days: 430,
      members_ready: 2,
      members_total: 2,
    },
    runs: { start: '2026-08-27', end: '2026-10-20', trading_days: 33 },
    effective: { start: '2025-01-02', end: '2026-10-19', trading_days: 430 },
    warnings: [],
  });
  mocks.getRanking.mockResolvedValue(ranking());
  mocks.preview.mockResolvedValue(ranking());
  mocks.getRolling.mockResolvedValue(rolling());
  mocks.getPeriod.mockResolvedValue(period());
  mocks.listPlans.mockResolvedValue({ items: [] });
  mocks.createPlan.mockResolvedValue({});
  mocks.confirmPlan.mockResolvedValue({});
  mocks.getEvidence.mockResolvedValue({ available: false, path: '/data/x/evidence.json' });
});

describe('ComboWorkspace', () => {
  it('首次进入空列表：自动把基线模板建成默认组合，并拉当日榜单', async () => {
    mocks.listBlends.mockResolvedValue([]);
    renderWorkspace();

    await screen.findByText('浦发银行');
    expect(mocks.createBlend).toHaveBeenCalledTimes(1);
    expect(mocks.createBlend.mock.calls[0][0]).toMatchObject({
      name: '全13·T+10 等权',
      market: 'CN',
      model_ids: ['m1', 'm2'],
      source: 'system',
      template_id: 'tpl-base',
      weights: null,
    });
    expect(mocks.getDates).toHaveBeenCalledWith('blend-new', 365);
    await waitFor(() =>
      expect(mocks.getRanking).toHaveBeenCalledWith('blend-new', { date: '2026-10-20', excludeBlacklist: false }),
    );
  });

  it('清单读取失败：展示原因与重试入口，重试后自动补建默认组合', async () => {
    mocks.listBlends.mockRejectedValueOnce(new Error('Model not found')).mockResolvedValueOnce([]);
    mocks.getAutoCandidates.mockResolvedValue(autoCandidates());
    renderWorkspace();

    expect(await screen.findByText('Model not found')).toBeInTheDocument();
    // 失败态不得再渲染「加载中」骨架，也不得谎称「暂无已保存组合」
    expect(screen.queryByText(/暂无已保存组合/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /重新加载/ }));
    await waitFor(() => expect(mocks.listBlends).toHaveBeenCalledTimes(2));
    // 重试成功后 autoCreate 守卫必须已复位，否则默认组合永远建不出来
    await waitFor(() => expect(mocks.createBlend).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('浦发银行')).toBeInTheDocument();
  });

  it('已有组合时不再建 C0，榜单带数据日', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');
    expect(mocks.createBlend).not.toHaveBeenCalled();
    expect(mocks.getRanking).toHaveBeenCalledWith('blend-1', { date: '2026-10-20', excludeBlacklist: false });
    expect(screen.getAllByText('全13·T+10 等权').length).toBeGreaterThan(0);
  });

  it('切到模板只走 /preview，滚动 Tab 提示先保存组合', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');

    fireEvent.click(screen.getByTestId('combo-item-template-tpl-base'));
    await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(1));
    expect(mocks.preview.mock.calls[0][0]).toMatchObject({
      market: 'CN',
      model_ids: ['m1', 'm2'],
      method: 'mean',
    });

    fireEvent.click(screen.getByRole('tab', { name: '滚动建仓' }));
    expect(await screen.findByText(/保存为组合后可查看滚动建仓/)).toBeInTheDocument();
    expect(mocks.getRolling).not.toHaveBeenCalled();
  });

  it('模板标注无推理成员数（K2：只列有 run 的成员）', async () => {
    mocks.getAutoCandidates.mockResolvedValue({
      ...autoCandidates(),
      items: [template({ missing_members: 11 })],
    });
    renderWorkspace();
    await screen.findByText('浦发银行');
    expect(screen.getByText('11 只无推理')).toBeInTheDocument();
  });

  it('已保存组合进入滚动 Tab 才请求，渲染新进色块', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');
    fireEvent.click(screen.getByRole('tab', { name: '滚动建仓' }));
    await waitFor(() => expect(mocks.getRolling).toHaveBeenCalled());
    expect(await screen.findByTestId('combo-rolling-pick-2026-10-20-SH600001')).toHaveTextContent('新进');
  });

  it('周期收益 Tab 懒加载：首进按默认参数算一次并渲染总收益', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');
    expect(mocks.getPeriod).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: '周期收益' }));
    await waitFor(() => expect(mocks.getPeriod).toHaveBeenCalledWith('blend-1', {}));
    expect(await screen.findByTestId('combo-period-total-return')).toHaveTextContent('+12.34%');
    expect(screen.getByTestId('combo-period-summary')).toHaveTextContent('已了结');
    // 展示面合规：不得出现方向性交易字眼
    expect(screen.queryByText(/买入|卖出/)).toBeNull();
  });

  it('切到模板时周期收益 Tab 显示先保存组合的空态', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');
    fireEvent.click(screen.getByTestId('combo-item-template-tpl-base'));
    await waitFor(() => expect(mocks.preview).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('tab', { name: '周期收益' }));
    expect(await screen.findByText(/保存为组合后可计算周期收益/)).toBeInTheDocument();
    expect(mocks.getPeriod).not.toHaveBeenCalled();
  });

  it('组合没有任何完整评分日：榜单给明确空因，不发无日期请求', async () => {
    mocks.getDates.mockResolvedValue({ blend_id: 'blend-1', dates: [] });
    renderWorkspace();
    expect(await screen.findByText('该组合暂无完整评分的交易日')).toBeInTheDocument();
    expect(mocks.getRanking).not.toHaveBeenCalled();
  });

  it('证据 Tab 懒加载证据包并经面板呈现空态', async () => {
    renderWorkspace();
    await screen.findByText('浦发银行');
    expect(mocks.getEvidence).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('tab', { name: '证据与回放' }));
    expect(await screen.findByText('证据未生成')).toBeInTheDocument();
    expect(mocks.getEvidence).toHaveBeenCalledTimes(1);
  });

  it('调仓计划 Tab：读取未确认计划，二次确认后落到 confirmPlan', async () => {
    const plan = {
      plan_id: 'plan-1',
      blend_id: 'blend-1',
      data_trade_date: '2026-10-20',
      target_date: '2026-10-21',
      params: {
        capital: 10_000_000, top_k: 50, hold_days: 10, entry_per_day: 5, target_positions: 50,
        cash_buffer: 0.02, adv_participation: 0.05, exit_delta: 0.05, lookback: 20, min_lookback_days: 10,
        min_adv_yi: 1, exclude_st: true, adv_lookback_days: 20, min_adv_days: 15,
      },
      summary: {
        capital: 10_000_000, target_positions: 50, entry_per_day: 5, hold_days: 10, top_k: 50,
        cash_buffer: 0.02, n_entries: 1, n_holds: 0, n_exits: 0, n_open_after: 1,
      },
      entries: [{ symbol: 'SH600001', mean20: 0.6, exit_line: 0.5 }],
      holds: [],
      exits: [],
      warnings: [],
      confirmed_at: null,
    };
    mocks.listPlans.mockResolvedValue({ items: [plan] });
    mocks.confirmPlan.mockResolvedValue({ ...plan, confirmed_at: '2026-10-20T15:10:00Z' });
    mocks.createPlan.mockResolvedValue({ ...plan, plan_id: 'plan-2' });

    renderWorkspace();
    await screen.findByText('浦发银行');
    fireEvent.click(screen.getByRole('tab', { name: '调仓计划' }));
    await waitFor(() => expect(mocks.listPlans).toHaveBeenCalledWith('blend-1', { date: '2026-10-20', limit: 1 }));
    expect(await screen.findByText('SH600001')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('combo-plan-confirm'));
    fireEvent.click(await screen.findByRole('button', { name: '确认（仅纸面）' }));
    await waitFor(() => expect(mocks.confirmPlan).toHaveBeenCalledWith('blend-1', 'plan-1'));

    // 参数沿用计划自身配置；重新生成必须持久化（persist: true）
    fireEvent.click(screen.getByRole('button', { name: '重新生成' }));
    await waitFor(() =>
      expect(mocks.createPlan).toHaveBeenCalledWith('blend-1', {
        data_trade_date: '2026-10-20',
        persist: true,
        capital: 10_000_000,
        top_k: 50,
        hold_days: 10,
        entry_per_day: 5,
        min_adv_yi: 1,
        exclude_st: true,
      }),
    );
  });

  it('新建按钮与治理面板带入的模型都能打开构建器（预填同组模型）', async () => {
    mocks.getEligible.mockResolvedValue({
      market: 'CN',
      total_eligible: 2,
      excluded: [],
      groups: [
        {
          key: 'g1',
          signature: { target_horizon_days: 10, feature_count: 300, data_source: 'quantdb_factors' },
          models: [
            { model_id: 'm1', display_name: 'LGB', model_type: 'lightgbm', model_source: 'manual', horizon_days: 10, feature_count: 300, status: 'ready' },
            { model_id: 'm2', display_name: 'XGB', model_type: 'xgboost', model_source: 'manual', horizon_days: 10, feature_count: 300, status: 'ready' },
          ],
        },
      ],
    });

    const { unmount } = renderWorkspace();
    await screen.findByText('浦发银行');
    fireEvent.click(screen.getByRole('button', { name: /新\s*建/ }));
    await waitFor(() => expect(mocks.getEligible).toHaveBeenCalledWith('CN'));
    unmount();

    mocks.getEligible.mockClear();
    render(
      <ComboWorkspace market="CN" marketLabel="A股市场" seedModelIds={['m1', 'm2']} seedNonce={1} />,
    );
    await waitFor(() => expect(mocks.getEligible).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/2 只成员 · T\+10 · 300 维/)).toBeInTheDocument();
  });
});
