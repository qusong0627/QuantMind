/**
 * FactorMarketReport —— 单因子 × 市场报告抽屉（T-FB-14 / 报告块 T-FB-16）。
 *
 * 钉死的边：
 * - 降级终态（data_unsupported 等）：给出状态 + 原因原文，不拉序列/报告、图表页签禁用
 *   ——诚实降级，不画空图；
 * - 完成：并行拉 /runs/{id}/series 与 /report/{id}，概览出标量卡 + 机构报告块
 *   （显著性 / 成本网格 / 头部 / 暂缺清单）；基准是等权兜底时必须显著标注
 *   （不能冒充指数超额）；
 * - 报告端点 available=false（序列未落盘）→ 只出原因文本，不出数字区块；
 * - Esc / 背板关闭。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { FactorMarketReport } from '../FactorMarketReport';
import type { DrillTarget, RunReport, RunSeries } from '../../../types-v2/backtestCenter';

const mocks = vi.hoisted(() => ({
  getRunSeries: vi.fn(),
  getRunReport: vi.fn(),
  downloadRunReportPdf: vi.fn(),
  fetchMatrix: vi.fn(),
}));

vi.mock('../../../services-v2/factorBacktestApi', () => ({
  getRunSeries: mocks.getRunSeries,
  getRunReport: mocks.getRunReport,
  downloadRunReportPdf: mocks.downloadRunReportPdf,
  fetchMatrix: mocks.fetchMatrix,
}));
// jsdom 无 canvas
vi.mock('../../../../../components/common/EChartsChart', () => ({
  EChartsChart: () => <div data-testid="chart" />,
}));

const mkSeries = (): RunSeries => ({
  dates: ['2024-01-01', '2024-01-02', '2024-01-03'],
  ic: [0.1, 0.2, -0.05],
  icCum: [0.1, 0.3, 0.25],
  navLong: [1, 1.01, 1.02],
  navLs: [1, 1.015, 1.01],
  navBench: [1, 1.005, 1.01],
  qCurves: { q1: [1, 0.99, 0.98], q2: [1, 1.02, 1.03] },
  turnover: [0.1, 0.1, 0.12],
  coverage: [300, 300, 298],
  bench: 'equal_weight',
  meta: { costBps: 10, topPct: 0.1, nBuckets: 2, turnoverConvention: '双边' },
});

const mkRun = () => ({
  runId: 'run-1',
  factorId: 'fa',
  factorName: '因子A',
  status: 'completed',
  kind: null,
  market: 'us_stock',
  universe: 'top300',
  dataSource: 'quantdb_factors',
  dateRange: '2024-01-01~2024-12-31',
  error: null,
  metrics: { rank_ic: 0.03, n_days: 250 },
  hasSeries: true,
  createdAt: '2026-10-10T01:00:00Z',
  finishedAt: '2026-10-10T01:05:00Z',
});

const TARGET_COMPLETED: DrillTarget = {
  factorId: 'fa',
  factorName: '因子A',
  market: 'us_stock',
  marketLabel: '美股（样本外）',
  runId: 'run-1',
  status: 'completed',
  metrics: { rank_ic: 0.03, n_days: 250 },
};

const TARGET_DEGRADED: DrillTarget = {
  factorId: 'fa',
  factorName: '因子A',
  market: 'crypto',
  marketLabel: '区块链（样本外）',
  runId: 'run-x',
  status: 'data_unsupported',
  error: '列 vol_persistence_20 在本市场不存在',
  dateRange: '2024-01-01~2024-12-31',
};

/** 机构报告块（T-FB-16；service 层已映射为 camelCase） */
const REPORT_OK: RunReport = {
  available: true,
  status: 'completed',
  runId: 'run-1',
  nDays: 3,
  headline: {
    nDays: 3,
    muDaily: 0.001,
    sigmaDaily: 0.01,
    annVol: 0.1587,
    returns: 0.252,
    cumReturn: 0.02,
    ir: 1.6,
    turnover: 0.3,
    fitness: 0.95,
    margin: 0.84,
  },
  significance: {
    plainT: 1.8,
    nwT: 1.5,
    pValue: 0.13,
    qValueBhy: 0.26,
    familyN: 4,
    familyNote: '族 = 同一批次全部完成单元（NW t → 正态双侧 p → BY 校正）',
    dsr: 0.71,
    nTrials: 4,
    nTrialsSource: 'batch_completed_units',
    dsrNote:
      'DSR 去膨胀所用试次数取批内完成单元数（挖掘史选型的真实试错数不可考，此为可计算下界，非全史试次数）',
    bootstrap: { lo: -0.02, hi: 0.09, point: 0.03, level: 0.95, nBoot: 2000, stat: 'mean' },
    crowding: {
      score: 0.5,
      turnoverPct: 0.4,
      icAutocorrLag1: 0.6,
      nDays: 3,
      note: '0.5×近期换手分位 + 0.5×IC 一阶自相关（截断到 [0,1]）',
    },
  },
  costGrid: {
    rows: [
      { bps: 0, netReturn: 0.25, netIr: 1.6, netFitness: 0.95 },
      { bps: 10, netReturn: 0.18, netIr: 1.2, netFitness: 0.8 },
      { bps: 20, netReturn: 0.11, netIr: 0.7, netFitness: 0.5 },
    ],
    breakEvenBps: 21.4,
    breakEvenNote: null,
    defaultBps: 20,
  },
  excess: {
    kind: 'equal_weight',
    benchmarkRef: 'csi300',
    label: '区间等权兜底',
    note: '超额基准为全域等权组合（兜底口径）；不得把该差额解读为对指数的超额。',
  },
  unavailable: [
    { block: 'capacity', reason: '序列载荷不含成交额与持仓市值，容量模型缺输入' },
    { block: 'ic_half_life', reason: '运行只存单视界 IC，无多视界 IC 衰减表' },
  ],
  meta: { costBps: 10, topPct: 0.1, turnoverConvention: 'daily_two_sided', source: 'stored_series' },
};

beforeEach(() => {
  mocks.getRunSeries.mockReset();
  mocks.getRunSeries.mockResolvedValue({
    success: true,
    data: { run: mkRun(), series: mkSeries() },
  });
  mocks.getRunReport.mockReset();
  mocks.getRunReport.mockResolvedValue({
    success: true,
    data: { run: mkRun(), report: REPORT_OK },
  });
  mocks.downloadRunReportPdf.mockReset();
  mocks.downloadRunReportPdf.mockResolvedValue({
    success: true,
    data: { blob: new Blob(['%PDF-1.4']), filename: '因子回测_因子A_fa_us_stock_run-1.pdf' },
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('因子报告抽屉', () => {
  test('降级终态：显示原因原文与诚实提示，图表页签禁用、不拉序列', async () => {
    render(<FactorMarketReport target={TARGET_DEGRADED} onClose={vi.fn()} />);
    await act(async () => {});

    expect(screen.getByText(/降级终态（数据不支持）/)).toBeTruthy();
    expect(screen.getByText(/该因子的代码依赖本市场不存在的列/)).toBeTruthy();
    expect(screen.getByText(/列 vol_persistence_20 在本市场不存在/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'IC' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '分组' }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.getRunSeries).not.toHaveBeenCalled();
    expect(mocks.getRunReport).not.toHaveBeenCalled();
    // 降级终态没有机构报告可导——按钮不出现
    expect(screen.queryByTestId('export-report-pdf')).toBeNull();
  });

  test('完成：拉序列、概览出标量卡，等权兜底显著标注', async () => {
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
    await act(async () => {});

    expect(mocks.getRunSeries).toHaveBeenCalledWith('run-1');
    expect(mocks.getRunReport).toHaveBeenCalledWith('run-1');
    // 标量卡（后端原始键 → 显示标签）
    expect(screen.getByText('Rank IC')).toBeTruthy();
    expect(screen.getByText('0.0300')).toBeTruthy();
    expect(screen.getByText('有效天数')).toBeTruthy();
    expect(screen.getByText('250')).toBeTruthy();
    // 等权兜底不是指数超额——必须标注
    expect(screen.getByText(/等权兜底/)).toBeTruthy();
    expect(screen.getByTestId('chart')).toBeTruthy();
    // 曲线页签可用
    expect((screen.getByRole('button', { name: 'IC' }) as HTMLButtonElement).disabled).toBe(false);
  });

  test('超额页签：真实指数基准显中文名（BENCH_LABELS）', async () => {
    mocks.getRunSeries.mockResolvedValue({
      success: true,
      data: { run: mkRun(), series: { ...mkSeries(), bench: 'csi300' } },
    });
    mocks.getRunReport.mockResolvedValue({
      success: true,
      data: {
        run: mkRun(),
        report: {
          ...REPORT_OK,
          excess: {
            kind: 'csi300',
            benchmarkRef: '000300.SH',
            label: '沪深300 指数',
            note: '超额基准 = 沪深300 指数（000300.SH）日收益（QuantDB index_daily，与落盘交易日对齐）。',
          },
        },
      },
    });
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
    await act(async () => {});

    fireEvent.click(screen.getByRole('button', { name: '超额' }));
    expect(screen.getByText(/——基准 沪深300 指数/)).toBeTruthy();
    expect(screen.getByText(/QuantDB index_daily/)).toBeTruthy();
  });

  test('超额页签：等权兜底写明非指数超额', async () => {
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
    await act(async () => {});

    fireEvent.click(screen.getByRole('button', { name: '超额' }));
    expect(screen.getByText(/——基准为等权兜底/)).toBeTruthy();
    expect(screen.getByText(/非指数超额/)).toBeTruthy();
  });

  test('机构报告块：显著性/成本网格/头部/暂缺清单落地', async () => {
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
    await act(async () => {});

    // 显著性：NW t / BY q / 试次数来源 / DSR 注解 / 拥挤度
    expect(screen.getByTestId('report-significance')).toBeTruthy();
    expect(screen.getByText('NW t 值')).toBeTruthy();
    expect(screen.getByText('1.50')).toBeTruthy();
    expect(screen.getByText('BY q 值（族校正后）')).toBeTruthy();
    expect(screen.getByText('0.2600')).toBeTruthy();
    expect(screen.getByText('批内完成单元数')).toBeTruthy();
    expect(screen.getByText(/DSR 去膨胀所用试次数/)).toBeTruthy();
    expect(screen.getByText(/拥挤度 0.500/)).toBeTruthy();

    // 多空腿头部（BRAIN 口径）
    expect(screen.getByTestId('report-headline')).toBeTruthy();
    expect(screen.getByText('Returns（毛年化）')).toBeTruthy();
    expect(screen.getByText('Margin（R/换手）')).toBeTruthy();

    // 成本敏感性网格：盈亏平衡 + 默认档行 + 单调行数据
    const grid = screen.getByTestId('report-cost-grid');
    expect(grid.textContent).toContain('盈亏平衡');
    expect(grid.textContent).toContain('21.4');
    expect(grid.textContent).toContain('净 Fitness');
    expect(screen.getByText('净年化')).toBeTruthy();

    // 暂缺清单（中文标签映射）
    const unavail = screen.getByTestId('report-unavailable');
    expect(unavail.textContent).toContain('容量估算');
    expect(unavail.textContent).toContain('IC 衰减半衰期');
  });

  test('报告块 available=false（序列未落盘）：只出原因文本，不出数字区块', async () => {
    mocks.getRunReport.mockResolvedValue({
      success: true,
      data: {
        run: mkRun(),
        report: {
          available: false,
          status: 'completed',
          reason: 'series_not_stored',
          note: '序列未落盘（或落盘失败），报告无数据面可装配',
        },
      },
    });
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
    await act(async () => {});

    const note = screen.getByTestId('report-unavailable-note');
    expect(note.textContent).toContain('series_not_stored');
    expect(screen.queryByTestId('report-significance')).toBeNull();
    expect(screen.queryByTestId('report-cost-grid')).toBeNull();
    expect(screen.queryByTestId('report-headline')).toBeNull();
    // 不可用块没有可导出的数字面——导出按钮不渲染
    expect(screen.queryByTestId('export-report-pdf')).toBeNull();
  });

  test('导出 PDF：available 时按钮出现，点击触发下载且无错误行', async () => {
    const createObjectURL = vi.fn(() => 'blob:mock-url');
    const revokeObjectURL = vi.fn();
    const origCreate = window.URL.createObjectURL;
    const origRevoke = window.URL.revokeObjectURL;
    window.URL.createObjectURL = createObjectURL as typeof window.URL.createObjectURL;
    window.URL.revokeObjectURL = revokeObjectURL;
    try {
      render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
      await act(async () => {});

      const btn = screen.getByTestId('export-report-pdf');
      await act(async () => {
        fireEvent.click(btn);
      });

      expect(mocks.downloadRunReportPdf).toHaveBeenCalledWith('run-1');
      expect(createObjectURL).toHaveBeenCalledTimes(1);
      expect(screen.queryByTestId('export-pdf-error')).toBeNull();
      // 按钮恢复可用（非导出中）
      expect((screen.getByTestId('export-report-pdf') as HTMLButtonElement).disabled).toBe(false);
    } finally {
      window.URL.createObjectURL = origCreate;
      window.URL.revokeObjectURL = origRevoke;
    }
  });

  test('导出 PDF 失败（如降级竞态 409）：amber 错误行带原因原文', async () => {
    mocks.downloadRunReportPdf.mockResolvedValue({
      success: false,
      error: '报告不可导出：series_not_stored',
    });
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
    await act(async () => {});

    await act(async () => {
      fireEvent.click(screen.getByTestId('export-report-pdf'));
    });

    const err = screen.getByTestId('export-pdf-error');
    expect(err.textContent).toContain('报告不可导出：series_not_stored');
  });

  test('报告块加载失败：amber 提示，曲线与标量卡不受影响', async () => {
    mocks.getRunReport.mockResolvedValue({ success: false, error: 'network down' });
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
    await act(async () => {});

    expect(screen.getByText(/机构报告块加载失败：network down/)).toBeTruthy();
    expect(screen.getByTestId('chart')).toBeTruthy();
    expect(screen.getByText('Rank IC')).toBeTruthy();
  });

  test('Esc 关闭', async () => {
    const onClose = vi.fn();
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={onClose} />);
    await act(async () => {});

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test('target 为 null：不渲染抽屉', () => {
    render(<FactorMarketReport target={null} onClose={vi.fn()} />);
    expect(screen.queryByTestId('factor-report')).toBeNull();
  });
});
