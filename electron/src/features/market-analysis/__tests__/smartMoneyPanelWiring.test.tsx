/**
 * 聪明钱面板接线 v2.1（一框六面重排：取数 → 布局 → 分面交互 → B 系列修复验收）。
 *
 * 为什么值得钉住：
 * - 榜单行点击必须打开个股档案抽屉，抽屉内「打开个股终端」把 symbol 交给页面；
 * - B1：筛选空态与基础空态分区文案，绝不落入误导性「事件池为空」；
 * - B8：服务端分页（下一页 → offset=50），绝不 300 行全渲染；
 * - B6：date prop 驱动取数并在头部/榜单披露「请求日 → 实际快照日」回退；
 * - B2：partial 横幅用「任一面上报」语义（overview.partial=true 而榜单 false 也要披露）；
 * - B4：席位窗口切换失败必须进错误条，不能伪装成「无记录」；
 * - B9：北向卡增持/减持双向展示；缺失条目仍渲染「—」而不是 0 分；
 * - B5：证据台 h5/h10 视野切换 + 分年表 + IC 衰减图存在。
 * - v2.1：仅激活面挂载（切走即卸，ECharts 不进 display:none 空转）；战绩/事件
 *   研究/证据台/营业部榜切到该面才取数（懒加载）；面内失败只走显式重试，绝不
 *   自动风暴（useFaceData 的 loaded/error 闸）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-mock" />,
}));

vi.mock('../services/smartMoneyService', () => {
  class SmartMoneyError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
    get notBuilt(): boolean {
      return this.status === 404;
    }
  }
  return {
    SmartMoneyError,
    getSmartMoneyOverview: vi.fn(),
    getSmartMoneyStocks: vi.fn(),
    getSmartMoneyBranches: vi.fn(),
    getSmartMoneyEvidence: vi.fn(),
    getSmartMoneyTrackRecord: vi.fn(),
    getSmartMoneyEventStudy: vi.fn(),
    getSmartMoneySeats: vi.fn(),
    getSmartMoneyDates: vi.fn(),
    getSmartMoneyStock: vi.fn(),
  };
});

import { SmartMoneyPanel } from '../components/smart-money/SmartMoneyPanel';
import {
  SmartMoneyError,
  getSmartMoneyBranches,
  getSmartMoneyDates,
  getSmartMoneyEventStudy,
  getSmartMoneyEvidence,
  getSmartMoneyOverview,
  getSmartMoneySeats,
  getSmartMoneyStock,
  getSmartMoneyStocks,
  getSmartMoneyTrackRecord,
  type SmartEvidenceSeg,
  type SmartStocksQuery,
} from '../services/smartMoneyService';

const mockOverview = vi.mocked(getSmartMoneyOverview);
const mockStocks = vi.mocked(getSmartMoneyStocks);
const mockBranches = vi.mocked(getSmartMoneyBranches);
const mockEvidence = vi.mocked(getSmartMoneyEvidence);
const mockTrack = vi.mocked(getSmartMoneyTrackRecord);
const mockEventStudy = vi.mocked(getSmartMoneyEventStudy);
const mockSeats = vi.mocked(getSmartMoneySeats);
const mockDates = vi.mocked(getSmartMoneyDates);
const mockStock = vi.mocked(getSmartMoneyStock);

/** 切到某个分面（标签栏按钮；role=tab） */
const openFace = async (name: string) => {
  await userEvent.click(screen.getByRole('tab', { name }));
};

const PILLARS = [
  { name: 'inst_net_buy_5d_z', label: '机构净买额·5日z20', unit: 'z', direction: 1 },
  { name: 'lhb_net_ratio_5d', label: '龙虎榜净买占比·5日', unit: 'ratio', direction: -1 },
  { name: 'margin_net_5d_z', label: '融资净买入·5日z20', unit: 'z', direction: -1 },
  { name: 'margin_bal_chg_20d', label: '融资余额变化·20日', unit: 'ratio', direction: -1 },
];

const ITEM_FULL = {
  symbol: 'SH603668',
  name: '天马科技',
  industry: '农牧饲渔',
  score: 82.47,
  covered: 3,
  n_pillars: 4,
  pct: { inst_net_buy_5d_z: 94.3, lhb_net_ratio_5d: 97.2, margin_net_5d_z: 88.4, margin_bal_chg_20d: null },
  val: { inst_net_buy_5d_z: 2.1, lhb_net_ratio_5d: 0.02, margin_net_5d_z: 1.8, margin_bal_chg_20d: null },
  inst_net_5d: 2e8,
  lhb_net_5d: 1e8,
  margin_net_5d: 15000,
  north_q_delta_pp: 1.23,
  latest_event: { type: 'lhb', date: '2026-10-14' },
};

const ITEM_MISSING = {
  symbol: 'SZ000001',
  name: '平安银行',
  industry: null,
  score: null,
  covered: 0,
  n_pillars: 4,
  pct: { inst_net_buy_5d_z: null, lhb_net_ratio_5d: null, margin_net_5d_z: null, margin_bal_chg_20d: null },
  val: { inst_net_buy_5d_z: null, lhb_net_ratio_5d: null, margin_net_5d_z: null, margin_bal_chg_20d: null },
  inst_net_5d: null,
  lhb_net_5d: null,
  margin_net_5d: null,
  north_q_delta_pp: null,
  latest_event: null,
};

const seg = (ic: number, q: number): SmartEvidenceSeg => ({
  n_days: 661,
  rank_ic: ic,
  icir: ic * 5,
  ic_pos_rate: 0.53,
  q1_bp: -20,
  q2_bp: -8,
  q3_bp: 0,
  q4_bp: 10,
  q5_bp: 35,
  q5_minus_q1_bp: q,
  tail_ok: true,
});

const STOCKS_BASE = {
  as_of: '2026-10-14',
  generated_at: '2026-10-15T15:30:00Z',
  partial: false,
  admitted: PILLARS.map((p) => p.name),
  pillars: PILLARS,
  summary: { pool_size: 725, covered_mean: 2.1 },
  total: 2,
  offset: 0,
  limit: 50,
  requested_date: null as string | null,
  items: [ITEM_FULL, ITEM_MISSING],
};

const OVERVIEW = {
  as_of: '2026-10-14',
  generated_at: '2026-10-15T15:30:00Z',
  partial: false,
  admitted: PILLARS.map((p) => p.name),
  pillars: PILLARS,
  summary: { pool_size: 725, covered_mean: 2.1 },
  overview: {
    lhb: { date: '2026-10-14', n_stocks: 61, net_buy_yi: 12.3, n20d_avg: 55.2, net20d_sum_yi: -30.5 },
    inst: { date: '2026-10-14', n_stocks: 21, net_buy_yi: 4.2 },
    dzjy: { date: '2026-10-14', amount_yi: 18.6, premium_share: 0.42 },
    survey: { date: '2026-10-14', n_stocks: 33, n_inst: 210 },
    margin: { date: '2026-10-13', net_5d_yi: -8.9, balance_yi: 16500.3 },
    north: {
      quarter: '2026Q2',
      report_date: '2026-06-30',
      n_total: 2318,
      n_inc: 1024,
      n_dec: 1198,
      top_inc: [
        { symbol: 'SH600036', stock_name: '招商银行', delta_pp: 1.21, pct: 4.5 },
        { symbol: 'SH601318', stock_name: '中国平安', delta_pp: 0.94, pct: 3.2 },
      ],
      top_dec: [
        { symbol: 'SZ000858', stock_name: '五粮液', delta_pp: -0.88, pct: 2.1 },
        { symbol: 'SH600519', stock_name: '贵州茅台', delta_pp: -0.52, pct: 1.8 },
      ],
    },
    branches_top: [],
    industry: [
      { industry: '电子元件', lhb_net_yi: 5.2, inst_net_yi: 8.1, dzjy_inst_net_yi: 2.4, n_stocks: 21, avg_score: 68.2 },
      { industry: '医药制造', lhb_net_yi: -3.1, inst_net_yi: -4.4, dzjy_inst_net_yi: null, n_stocks: 12, avg_score: 55.1 },
    ],
  },
  trends: {
    lhb: [
      { date: '2026-10-10', lhb_n_stocks: 50, lhb_net_buy_yi: 3.2 },
      { date: '2026-10-13', lhb_n_stocks: 55, lhb_net_buy_yi: null },
      { date: '2026-10-14', lhb_n_stocks: 61, lhb_net_buy_yi: 12.3 },
    ],
    inst: [{ date: '2026-10-14', inst_n_stocks: 21, inst_net_buy_yi: 4.2 }],
    margin: [{ date: '2026-10-13', margin_net_5d_yi: -8.9, margin_bal_yi: 16500.3 }],
    dzjy: [{ date: '2026-10-14', dzjy_amount_yi: 18.6, dzjy_premium_share: 0.42 }],
    survey: [{ date: '2026-10-14', survey_n_stocks: 33, survey_n_inst: 210 }],
  },
  freshness: { sources: {}, margin_last: '2026-10-13', kline_last: '2026-10-14' },
  north_note: '2024-08-19 起北向仅季度披露',
};

const BRANCHES = {
  as_of: '2026-10-14',
  window: 5,
  branches: [
    { branch_name: '国泰君安南京太平南路', net_amount: 2.5e8, buy_amount: 3e8, sell_amount: 0.5e8, n_days: 3, stocks_bought: 'SH600000, SZ000001' },
    { branch_name: '中信建投北京朝阳门', net_amount: -1.2e8, buy_amount: 0.3e8, sell_amount: 1.5e8, n_days: 2, stocks_bought: null },
  ],
};

const EVIDENCE = {
  validated_at: '2026-10-15T23:24:00Z',
  window: { eval_start: '2024-01-02', eval_end: '2026-10-14', warmup_start: '2023-10-01' },
  method: {},
  signals: [
    { name: 'inst_net_buy_5d_z', family: 'inst', min_names: null, admitted: true, direction: 1, decision: 'admitted', results: { '10': seg(0.0195, 55), '5': seg(0.0179, 35) }, by_year: { '10': { '2024': seg(0.0121, 50), '2025': seg(-0.0075, -14) } } },
    { name: 'lhb_net_ratio_5d', family: 'lhb', min_names: null, admitted: true, direction: -1, decision: 'admitted', results: { '10': seg(-0.0412, -40) } },
    { name: 'inst_net_ratio_5d', family: 'inst', min_names: null, admitted: false, direction: 1, decision: 'rejected', results: { '10': seg(0.0013, 23) } },
  ],
  decay: {
    composite: {
      '2': { ic: 0.008, icir: 0.1, n_days: 600 },
      '3': { ic: 0.012, icir: 0.14, n_days: 600 },
      '5': { ic: 0.02, icir: 0.2, n_days: 600 },
      '10': { ic: 0.031, icir: 0.31, n_days: 600 },
      '20': { ic: 0.025, icir: 0.22, n_days: 600 },
    },
    inst_net_buy_5d_z: { '2': { ic: 0.004, icir: 0.05, n_days: 600 }, '10': { ic: 0.0195, icir: 0.2, n_days: 600 } },
  },
  correlation: { pairs: [], collapsed: [] },
  composite: {
    results: { '10': seg(0.031, 71), '5': seg(0.018, 40) },
    by_year: { '10': { '2024': seg(0.0121, 50), '2025': seg(-0.0075, -14), '2026': seg(-0.0022, 37) } },
    n_dates: 661,
    avg_pool: 725,
    avg_covered: 2.1,
  },
  evidence_only: [{ name: 'north_q_delta', conclusion: '北向持仓仅季度频率', use: '仅展示不入柱' }],
  admitted: PILLARS.map((p) => p.name),
  rejected: [{ name: 'inst_net_ratio_5d', family: 'inst', reason: '|IC| 过弱', rank_ic_10: 0.0013, q5_minus_q1_bp_10: 23, n_days_10: 661 }],
};

const TRACK = {
  as_of: '2026-10-14',
  generated_at: '2026-10-15T15:30:00Z',
  top_n: 10,
  horizons: [5, 10],
  benchmark: '全市场等权',
  daily: [{ date: '2026-10-14', ret_h: 0.012, bench_h: 0.003, excess_h: 0.009 }],
  windows: [
    { window: 'all', h5: { n_days: 500, mean_excess_bp: 61.2, hit_rate: 0.58, mean_ret_bp: 80 }, h10: { n_days: 480, mean_excess_bp: 74.5, hit_rate: 0.57, mean_ret_bp: 110 } },
    { window: '20d', h5: { n_days: 18, mean_excess_bp: -12.4, hit_rate: 0.44, mean_ret_bp: -5 }, h10: { n_days: 0, mean_excess_bp: null, hit_rate: null, mean_ret_bp: null } },
  ],
  cum_excess: {
    h5: [
      { date: '2026-10-10', cum: 12.5 },
      { date: '2026-10-13', cum: 13.1 },
      { date: '2026-10-14', cum: 14.0 },
    ],
    h10: [{ date: '2026-10-14', cum: 9.9 }],
  },
  buckets: {
    labels: ['<50', '50-70', '70-85', '85+'],
    midpoints: [25, 60, 77, 92],
    mean_excess_bp: [-20.1, 10.2, 44.8, 88.5],
    n_days: [500, 500, 500, 500],
    monotonicity: 1.0,
  },
  rolling_ic: { h: 10, window: 60, series: [{ date: '2026-10-13', ic: 0.02 }, { date: '2026-10-14', ic: 0.031 }] },
  recent: [
    { date: '2026-10-14', symbol: 'SH600001', name: '示例甲', score: 91.2, ret_2: 0.02, ret_5: null, ret_10: null, status: '持有中', hold_days: 3, ret_so_far: 0.035 },
    { date: '2026-10-10', symbol: 'SZ300002', name: '示例乙', score: 88.0, ret_2: 0.01, ret_5: -0.008, ret_10: null, status: '部分了结', hold_days: 5, ret_so_far: 0.012 },
  ],
};

const EVENT_STUDY = {
  as_of: '2026-10-14',
  generated_at: '2026-10-15T15:30:00Z',
  car_days: 20,
  benchmark: '全市场等权',
  families: {
    龙虎榜: { n_events: 1200, n_scored: 1100, n_q5: 220, n_q1: 220, car_q5_bp: [5.2, 8.1], car_q1_bp: [-3.1, -6.4], spread_bp: [8.3, 14.5] },
    机构席位: { n_events: 800, n_scored: 750, n_q5: 150, n_q1: 150, car_q5_bp: [4.0, 6.2], car_q1_bp: [-2.2, -3.5], spread_bp: [6.2, 9.7] },
  },
};

const SEATS = {
  as_of: '2026-10-14',
  horizon: 5,
  min_trades: 5,
  windows: ['20', '60', 'all'],
  window: 'all',
  sort: 'win_rate',
  class: null as string | null,
  baseline_win_rate: 0.458,
  n_events: 13274,
  classes: [
    { class: 'inst', label: '机构专用', n_events: 3000, n_seats: 80, win_rate: 0.52, avg_excess_bp: 40.2, total_net_yi: 120.5 },
    { class: 'north', label: '陆股通', n_events: 900, n_seats: 30, win_rate: 0.51, avg_excess_bp: 22.1, total_net_yi: 60.2 },
    { class: 'branch', label: '券商席位', n_events: 8000, n_seats: 120, win_rate: 0.46, avg_excess_bp: 5.0, total_net_yi: -30.4 },
    { class: 'other', label: '其他', n_events: 1374, n_seats: 20, win_rate: 0.44, avg_excess_bp: -8.8, total_net_yi: -10.1 },
  ],
  total: 2,
  items: [
    { seat_name: '华鑫证券上海分公司', class: 'branch', class_label: '券商席位', total_net_yi: 12.5, last_seen: '2026-10-14', windows: { all: { n: 9, win_rate: 0.55, avg_excess_bp: 66.0 }, '20': { n: 3, win_rate: 0.66, avg_excess_bp: 40.0 } } },
    { seat_name: '机构专用', class: 'inst', class_label: '机构专用', total_net_yi: 30.2, last_seen: '2026-10-13', windows: { all: { n: 3, win_rate: 0.33, avg_excess_bp: -10.0 }, '20': { n: 1, win_rate: 0.0, avg_excess_bp: -50.0 } } },
  ],
};

const DATES = { dates: ['2026-10-10', '2026-10-13', '2026-10-14'], as_of: '2026-10-14', generated_at: '2026-10-15T15:30:00Z' };

const STOCK_DETAIL = {
  symbol: 'SH603668',
  name: '天马科技',
  industry: '农牧饲渔',
  as_of: '2026-10-14',
  generated_at: '2026-10-15T15:30:00Z',
  partial: false,
  on_board: true,
  item: ITEM_FULL,
  pillars: PILLARS,
  timeline: [
    { type: 'lhb', label: '龙虎榜', date: '2026-10-14', net_buy: 1e8, net_ratio: 0.08, reason: '日涨幅偏离值达 7%', fwd_ret_5d: 0.02, fwd_ret_10d: null },
    { type: 'survey', label: '机构调研', date: '2026-10-10', inst_count: 12, reception_type: '特定对象调研', reception_place: '公司会议室' },
  ],
  north: [{ quarter: '2026Q2', report_date: '2026-06-30', pct_pp: 2.31, chg_pp: 0.42 }],
  seats: {
    date: '2026-10-14',
    seats: [
      { seat_name: '华鑫证券上海分公司', side: 'buy', class: 'branch', class_label: '券商席位', buy_amount: 5e7, sell_amount: null, net_amount: 5e7, buy_ratio: 0.08, sell_ratio: null },
      { seat_name: '机构专用', side: 'sell', class: 'inst', class_label: '机构专用', buy_amount: null, sell_amount: 3e7, net_amount: -3e7, buy_ratio: null, sell_ratio: 0.05 },
    ],
  },
  dzjy: [
    { date: '2026-10-13', deal_price: 12.5, close: 12.8, premium: -0.023, volume: 2000000, amount: 2.5e7, amount_ratio: 0.03, seat_buyer: '机构专用', seat_seller: '某某营业部', buyer_is_inst: true, seller_is_inst: false },
  ],
  study_ref: { families: ['龙虎榜', '机构专用'], endpoint: '/smart-money/event-study', note: '分位分组口径见事件研究区' },
};

beforeEach(() => {
  vi.clearAllMocks();
  mockOverview.mockResolvedValue(OVERVIEW);
  mockStocks.mockImplementation(async (q: SmartStocksQuery = {}) => {
    let items = [ITEM_FULL, ITEM_MISSING];
    if (q.event) items = items.filter((i) => i.latest_event?.type === q.event);
    const offset = q.offset ?? 0;
    const limit = q.limit ?? 50;
    return {
      ...STOCKS_BASE,
      items: items.slice(offset, offset + limit),
      total: items.length,
      offset,
      limit,
      requested_date: q.date ?? null,
    };
  });
  mockBranches.mockResolvedValue(BRANCHES);
  mockEvidence.mockResolvedValue(EVIDENCE);
  mockTrack.mockResolvedValue(TRACK);
  mockEventStudy.mockResolvedValue(EVENT_STUDY);
  mockSeats.mockResolvedValue(SEATS);
  mockDates.mockResolvedValue(DATES);
  mockStock.mockResolvedValue(STOCK_DETAIL);
});

describe('聪明钱面板接线 v2.1（一框六面）', () => {
  it('榜单行点击 → 个股档案抽屉；抽屉「打开个股终端」→ onOpenTerminal(symbol)', async () => {
    const onOpenTerminal = vi.fn();
    render(<SmartMoneyPanel onOpenTerminal={onOpenTerminal} />);

    await screen.findByText('天马科技');
    await userEvent.click(screen.getByText('天马科技'));

    expect(mockStock).toHaveBeenCalledWith('SH603668');
    await screen.findByText('事件时间线');
    expect(screen.getByText('最近龙虎榜席位构成')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /打开个股终端/ }));
    expect(onOpenTerminal).toHaveBeenCalledWith('SH603668');
  });

  it('缺失条目在榜上显示「—」而不是 0 分（缺失 ≠ 0）', async () => {
    render(<SmartMoneyPanel />);
    await screen.findByText('平安银行');
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('B1：事件筛选空态与基础空态分区文案', async () => {
    render(<SmartMoneyPanel />);
    await screen.findByText('天马科技');

    // 事件筛选 = 机构席位：mock 无匹配（仅 lhb 一条）→ 筛选空态文案
    await userEvent.click(screen.getByRole('button', { name: '机构席位' }));
    await screen.findByText(/当前筛选条件下榜内无匹配标的/);
    expect(screen.queryByText(/当日事件池为空/)).not.toBeInTheDocument();
  });

  it('B1b：无筛选且榜为空 → 基础空态文案', async () => {
    mockStocks.mockResolvedValue({ ...STOCKS_BASE, items: [], total: 0 });
    render(<SmartMoneyPanel />);
    await screen.findByText(/当日事件池为空/);
  });

  it('B8：服务端分页——下一页请求 offset=50 并展示页码', async () => {
    mockStocks.mockResolvedValue({ ...STOCKS_BASE, total: 120 });
    render(<SmartMoneyPanel />);
    await screen.findByText('天马科技');
    await userEvent.click(screen.getByRole('button', { name: '明细' }));

    await userEvent.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() =>
      expect(mockStocks).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50, limit: 50 })),
    );
    await screen.findByText(/第 2\/3 页/);
  });

  it('v3：选股榜默认图表视图（综合分/六柱热力/散点），可切明细表', async () => {
    render(<SmartMoneyPanel />);
    await screen.findByText('天马科技');
    expect(screen.getByText(/综合分 Top/)).toBeInTheDocument();
    expect(screen.getByText(/六柱分位热力图/)).toBeInTheDocument();
    expect(screen.getByText(/综合分 × 机构净买/)).toBeInTheDocument();
    expect(screen.getAllByTestId('echarts-mock').length).toBeGreaterThanOrEqual(3);

    await userEvent.click(screen.getByRole('button', { name: '明细' }));
    expect(screen.getByRole('button', { name: '下一页' })).toBeInTheDocument();
  });

  it('B6：date prop 驱动取数 + 头部披露「请求日 → 实际快照」', async () => {
    render(<SmartMoneyPanel date="2026-10-10" />);
    await screen.findByText('天马科技');

    expect(mockStocks).toHaveBeenCalledWith(expect.objectContaining({ date: '2026-10-10' }));
    // as_of=2026-10-14 ≠ 请求 2026-10-10 → 回退披露（面板头部一处）
    await screen.findByText(/请求 2026-10-10，回退最近快照/);
  });

  it('B2：overview.partial=true 且榜单 partial=false → 仍披露横幅', async () => {
    mockOverview.mockResolvedValue({ ...OVERVIEW, partial: true });
    render(<SmartMoneyPanel />);
    await screen.findByText(/部分数据源未就绪/);
  });

  it('B9：北向卡增持/减持双向展示（top_dec 不再被丢弃）', async () => {
    render(<SmartMoneyPanel />);
    await screen.findByText('招商银行');
    expect(screen.getByText('五粮液')).toBeInTheDocument();
    expect(screen.getByText('+1.21pp')).toBeInTheDocument();
    expect(screen.getByText('-0.88pp')).toBeInTheDocument();
  });

  it('主面 404（smart_money_not_built）→ 空态说明卡而非白屏/报错', async () => {
    const err = new SmartMoneyError('smart_money_not_built', 404);
    mockOverview.mockRejectedValue(err);
    render(<SmartMoneyPanel />);

    await screen.findByText(/聪明钱统计尚未生成/);
    expect(screen.queryByText('天马科技')).not.toBeInTheDocument();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
  });

  it('v2.1：标签栏六面 + 默认选股榜激活（aria-selected）', async () => {
    render(<SmartMoneyPanel />);
    await screen.findByText('天马科技');

    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual([
      '选股榜',
      '战绩追踪',
      '席位面',
      '事件研究',
      '生态趋势',
      '验证口径',
    ]);
    expect(screen.getByRole('tab', { name: '选股榜' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: '验证口径' })).toHaveAttribute('aria-selected', 'false');
  });

  it('v2.1：懒加载——未切到的面绝不取数（战绩/事件/证据/营业部榜）', async () => {
    render(<SmartMoneyPanel />);
    await screen.findByText('天马科技');

    expect(mockTrack).not.toHaveBeenCalled();
    expect(mockEventStudy).not.toHaveBeenCalled();
    expect(mockEvidence).not.toHaveBeenCalled();
    expect(mockBranches).not.toHaveBeenCalled();

    await openFace('战绩追踪');
    await waitFor(() => expect(mockTrack).toHaveBeenCalledTimes(1));
    expect(mockEventStudy).not.toHaveBeenCalled();
    expect(mockEvidence).not.toHaveBeenCalled();

    await openFace('事件研究');
    await waitFor(() => expect(mockEventStudy).toHaveBeenCalledTimes(1));
    expect(mockEvidence).not.toHaveBeenCalled();

    await openFace('验证口径');
    await waitFor(() => expect(mockEvidence).toHaveBeenCalledTimes(1));

    await openFace('席位面');
    await waitFor(() => expect(mockBranches).toHaveBeenCalledWith(5));
  });

  it('v2.1：非激活面卸载（切走即卸，回切重新取数）', async () => {
    render(<SmartMoneyPanel />);
    await screen.findByText('天马科技');
    const callsAfterFirstRender = mockStocks.mock.calls.length;

    await openFace('事件研究');
    await screen.findByText(/事件研究 · CAR/);
    expect(screen.queryByText('天马科技')).not.toBeInTheDocument();

    await openFace('选股榜');
    await screen.findByText('天马科技');
    await waitFor(() => expect(mockStocks.mock.calls.length).toBeGreaterThan(callsAfterFirstRender));
  });

  it('B4：席位窗口切换失败 → 错误条（不伪装空数据）', async () => {
    render(<SmartMoneyPanel />);
    await openFace('席位面');
    await screen.findByText('华鑫证券上海分公司');

    mockSeats.mockRejectedValue(new Error('席位库连接失败'));
    // SeatBoard 的「20 日」位于 DOM 前（席位榜在营业部榜之前）
    const twentyButtons = screen.getAllByRole('button', { name: '20 日' });
    await userEvent.click(twentyButtons[0]);
    await screen.findByText(/席位库连接失败/);
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('席位榜：窗口/排序/类型三筛均透传服务端', async () => {
    render(<SmartMoneyPanel />);
    await openFace('席位面');
    await screen.findByText('华鑫证券上海分公司');
    expect(mockSeats).toHaveBeenCalledWith(expect.objectContaining({ window: 'all', sort: 'win_rate' }));

    const twentyButtons = screen.getAllByRole('button', { name: '20 日' });
    await userEvent.click(twentyButtons[0]);
    await waitFor(() => expect(mockSeats).toHaveBeenLastCalledWith(expect.objectContaining({ window: '20' })));

    await userEvent.click(screen.getByRole('button', { name: '按净买' }));
    await waitFor(() => expect(mockSeats).toHaveBeenLastCalledWith(expect.objectContaining({ sort: 'net' })));

    const instChips = screen.getAllByRole('button', { name: /机构专用/ });
    await userEvent.click(instChips[0]);
    await waitFor(() => expect(mockSeats).toHaveBeenLastCalledWith(expect.objectContaining({ seatClass: 'inst' })));
  });

  it('营业部榜窗口切换重新取数（5 → 20）', async () => {
    render(<SmartMoneyPanel />);
    await openFace('席位面');
    await screen.findByText('国泰君安南京太平南路');
    expect(mockBranches).toHaveBeenCalledWith(5);

    const twentyButtons = screen.getAllByRole('button', { name: '20 日' });
    await userEvent.click(twentyButtons[1]);
    await waitFor(() => expect(mockBranches).toHaveBeenCalledWith(20));
  });

  it('B5：证据台 h5/h10 切换 + 分年表 + 衰减图存在', async () => {
    render(<SmartMoneyPanel />);
    await openFace('验证口径');
    await screen.findByText(/验证证据台/);

    // 默认 h10（复合分分年表标题随视野）
    expect(screen.getByText('复合分分年（h10）')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'h5' }));
    expect(screen.getByText('复合分分年（h5）')).toBeInTheDocument();
    expect(screen.getByText('IC·h5')).toBeInTheDocument();

    // 图表（衰减图均为 mock 元素）；口径与新鲜度随面归位（原底部三列 → 本面尾）
    expect(screen.getAllByTestId('echarts-mock').length).toBeGreaterThan(0);
    expect(screen.getByText('口径与新鲜度')).toBeInTheDocument();
    expect(screen.getByText(/两融披露截至/)).toBeInTheDocument();
  });

  it('面内取数失败 → 面内错误条 + 重试成功；不自动风暴', async () => {
    mockEvidence.mockRejectedValueOnce(new Error('证据库崩了'));
    render(<SmartMoneyPanel />);

    await openFace('验证口径');
    await screen.findByText(/证据库崩了/);

    // 失败后既不再自动重取，也不在来回切面时重取（loaded/error 闸）
    await openFace('选股榜');
    await screen.findByText('天马科技');
    await openFace('验证口径');
    await screen.findByText(/证据库崩了/);
    expect(mockEvidence).toHaveBeenCalledTimes(1);

    // 显式重试（第二次 mock 生效 → 内容出现）
    await userEvent.click(screen.getByRole('button', { name: '重试' }));
    await screen.findByText(/验证证据台/);
    expect(mockEvidence).toHaveBeenCalledTimes(2);
  });

  it('刷新：概览重取 + 已激活懒面重取（战绩面为例）', async () => {
    render(<SmartMoneyPanel />);
    await openFace('战绩追踪');
    await waitFor(() => expect(mockTrack).toHaveBeenCalledTimes(1));
    const overviewCalls = mockOverview.mock.calls.length;

    await userEvent.click(screen.getByRole('button', { name: /刷新/ }));
    await waitFor(() => expect(mockTrack).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mockOverview.mock.calls.length).toBeGreaterThan(overviewCalls));
  });

  it('战绩区：全史头号数字 + 最近案例行点击开档案', async () => {
    render(<SmartMoneyPanel />);
    await openFace('战绩追踪');
    await screen.findByText(/战绩追踪 · 历史回溯统计/);
    expect(screen.getByText('平均超额')).toBeInTheDocument();
    expect(screen.getByText('持有中·3d')).toBeInTheDocument();

    await userEvent.click(screen.getByText('示例甲'));
    await waitFor(() => expect(mockStock).toHaveBeenCalledWith('SH600001'));
  });
});
