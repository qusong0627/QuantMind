import { beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MarketTimingV2Panel } from '../components/MarketTimingV2Panel';
import type { TimingV2Metrics, TimingV2Snapshot } from '../types/timingV2';

const metrics = (overrides: Partial<TimingV2Metrics> = {}): TimingV2Metrics => ({
  n: 272,
  ann: 0.143,
  vol: 0.071,
  sharpe: 2.0,
  mdd: -0.051,
  hit: 0.22,
  turnover: 0.12,
  exposure: 0.24,
  t: 1.97,
  total: 0.16,
  ...overrides,
});

const sample: TimingV2Snapshot = {
  status: 'ok',
  model_version: 'market-timing-v2-shadow-1',
  generated_at: '2026-11-24T17:12:00+08:00',
  data_as_of: '2026-11-23',
  tft_as_of: '2026-11-17',
  mlp_as_of: '2026-11-23',
  as_of: '2026-11-17',
  stale_trading_days: 4,
  market: 'CN',
  tft_model: { model_id: 'mdl_tft', role: '战术层', pred_mean: 0.0134, pred_bottom20: -0.068, z: 2.35 },
  mlp_model: { model: 'market_mlp_v2', role: '战略确认层', pred: 0.0126, z: 0.56 },
  signal: {
    tft_z: 2.35,
    mlp_z: 0.56,
    exposure: 1,
    label: '进攻（满额）',
    rule: 'TFT_z>0 → 基准 50%；TFT_z>0 且 MLP_z>0 → 100%；TFT_z≤0 → 0%',
    execution: 'HS300 ETF/期货，T+1 开盘执行，5bp/次；不加杠杆、不做空',
    confidence: 'shadow',
  },
  structure: {
    breadth: 0.243,
    gap_ie_20: 0.014,
    gap_hz_20: 0.01,
    vol20: 0.0108,
    vol20_ann: 0.171,
    bp_capw_pct: 0.44,
    ep_capw_pct: 0.5,
    med_pe_pct: 0.32,
    amount_ratio20: 0.96,
  },
  performance: {
    window: { start: '2025-09-29', end: '2026-11-17', n: 272 },
    combined: metrics(),
    tft_only: metrics({ ann: 0.19, sharpe: 2.01, mdd: -0.058, exposure: 0.375 }),
    mlp_only: metrics({ ann: -0.041, sharpe: -0.35, mdd: -0.092, exposure: 0.35 }),
    buy_hold: metrics({ ann: -0.127, sharpe: -0.73, mdd: -0.214, exposure: 1 }),
    placebo_p: 0.0033,
    mlp_long_history: {
      window: { start: '2019-01-01', end: '2026-11-23' },
      strategy: metrics({ ann: 0.094, sharpe: 0.65, mdd: -0.217 }),
      buy_hold: metrics({ ann: 0.058, sharpe: 0.28, mdd: -0.467 }),
    },
  },
  history: [
    { date: '2026-11-13', tft_z: 1.54, mlp_z: -0.12, exposure: 0.5, tft_pred_mean: 0.0072, mlp_pred: 0.0018 },
    { date: '2026-11-16', tft_z: 2.37, mlp_z: 0.35, exposure: 1, tft_pred_mean: 0.0135, mlp_pred: 0.0092 },
    { date: '2026-11-17', tft_z: 2.35, mlp_z: 0.56, exposure: 1, tft_pred_mean: 0.0134, mlp_pred: 0.0126 },
  ],
  roles: [
    { model: 'TFT 深度序列', role: '战术层', use: 'pred_mean / pred_bottom20 聚合分', status: '参与' },
    { model: 'MLP 市场级', role: '战略确认层', use: '趋势/广度/估值/裂口/两融特征', status: '参与' },
    { model: 'LightGBM 树模型', role: '截面选股', use: '只做个股排序，不参与大盘方向', status: '不参与' },
  ],
  notes: ['影子盘/研究口径，不构成投资建议。', '树模型不参与大盘方向。'],
};

describe('MarketTimingV2Panel 机构择时', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test('展示暴露、TFT/MLP 信号、表现与模型分工', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => sample }) as Response),
    );
    render(<MarketTimingV2Panel />);
    expect((await screen.findAllByText('进攻（满额）')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('100%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('+2.35').length).toBeGreaterThan(0);
    expect(screen.getAllByText('+0.56').length).toBeGreaterThan(0);
    expect(screen.getAllByText('TFT 50% + MLP 确认').length).toBeGreaterThan(0);
    expect(screen.getByText('影子盘 · 不构成投资建议')).toBeInTheDocument();
    expect(screen.getByText('LightGBM 树模型')).toBeInTheDocument();
    expect(screen.getByText(/TFT 信号落后行情/)).toBeInTheDocument();
    expect(screen.getByText(/placebo p = 0.003/)).toBeInTheDocument();
  });

  test('快照缺失时展示空态而不是白屏', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) }) as Response),
    );
    render(<MarketTimingV2Panel />);
    await waitFor(() => expect(screen.getByText('机构择时 v2 尚未就绪')).toBeInTheDocument());
    expect(screen.getByText(/每日 17:10 自动生成/)).toBeInTheDocument();
  });
});
