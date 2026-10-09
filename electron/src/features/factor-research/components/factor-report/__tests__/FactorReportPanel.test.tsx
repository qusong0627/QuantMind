/**
 * 因子报告面板的深链契约（2026-10-07 由筛选页签的「报告」按钮接进来）。
 *
 * 面板原本是自足的：进来自动选第一个可用数据集的第一个因子。加了深链之后，
 * 有两条**不会报错、只会把人领错**的路必须守住：
 *
 * 1. `initialDataset` 不能被「第一个可用数据集」改写。改了的话用户点的是
 *    L2 因子，落到的却是 Alpha 库，而界面一切正常；
 * 2. 点名要的因子不在快照里时，**绝不能回落到第一个因子**。筛选清单来自盘上
 *    现算的因子，报告快照是另一个脚本按自己的清单生成的，两者会不同步
 *    （新挖到的因子就是典型）。回落 = 点 A 看到 B。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { FactorReportPanel } from '../FactorReportPanel';

const { summaryMock, datasetsMock, detailMock, relatedMock, corrMock, buildStatusMock, startBuildMock } = vi.hoisted(() => ({
  summaryMock: vi.fn(),
  datasetsMock: vi.fn(),
  detailMock: vi.fn(),
  relatedMock: vi.fn(),
  corrMock: vi.fn(),
  buildStatusMock: vi.fn(),
  startBuildMock: vi.fn(),
}));

vi.mock('../../../services/factorReportService', () => ({
  getFactorSummary: summaryMock,
  getFactorDatasets: datasetsMock,
  getFactorDetail: detailMock,
  getFactorRelated: relatedMock,
  getFactorCorrelation: corrMock,
  getFactorReportBuildStatus: buildStatusMock,
  startFactorReportBuild: startBuildMock,
}));

// 重子组件换成桩：本文件测的是面板选谁，不是子组件怎么画
vi.mock('../FactorRankList', () => ({
  FactorRankList: (p: {
    factors: Array<{ name: string }>;
    selected: string | null;
    onSelect: (name: string) => void;
  }) => (
    <div data-testid="rank-list" data-selected={p.selected} data-n={p.factors.length}>
      {p.factors.map((f) => (
        <button key={f.name} onClick={() => p.onSelect(f.name)}>{`桩-选 ${f.name}`}</button>
      ))}
    </div>
  ),
}));
vi.mock('../FactorDetailTabs', () => ({
  FactorDetailTabs: (p: { factor: string }) => <div data-testid="detail" data-factor={p.factor} />,
}));
vi.mock('../FactorClusterModal', () => ({ FactorClusterModal: () => null }));
vi.mock('../FactorPortfolioModal', () => ({ FactorPortfolioModal: () => null }));

const DATASETS = {
  default: 'alpha_library',
  items: [
    { dataset: 'alpha_library', label: 'Alpha 库', available: true },
    { dataset: 'l2_factors', label: 'L2 因子', available: true },
  ],
};

/** 顶栏会逐字段读它（universe / horizon.replace(...)…），给全字段免得踩空。 */
const META = {
  universe: '全 A 非 ST',
  horizon: 'fwd_ret_5',
  start: '20220104',
  end: '20260827',
  n_dates: 1000,
  generated_at: '2026-09-19 20:20:50',
};

const summaryFor = (factors: Array<{ name: string }>) => ({
  available: true,
  dataset: 'alpha_library',
  meta: META,
  total: factors.length,
  factors,
});

beforeEach(() => {
  summaryMock.mockReset();
  datasetsMock.mockReset();
  detailMock.mockReset();
  relatedMock.mockReset();
  corrMock.mockReset();
  buildStatusMock.mockReset();
  startBuildMock.mockReset();
  datasetsMock.mockResolvedValue(DATASETS);
  summaryMock.mockResolvedValue(summaryFor([{ name: 'f1' }, { name: 'f2' }]));
  detailMock.mockResolvedValue({ factor: 'f1' });
  relatedMock.mockResolvedValue({ related: [] });
  corrMock.mockResolvedValue({});
  // 重建完成态（running:false）：轮询首拍即收敛，测试无需推进计时器
  buildStatusMock.mockResolvedValue({ dataset: 'alpha_library', exists: true, running: false, step: '' });
  startBuildMock.mockResolvedValue({ started: true, running: true });
});

describe('FactorReportPanel：深链', () => {
  test('带 initialDataset + initialCode 时落到指定的数据集与因子', async () => {
    summaryMock.mockResolvedValue(summaryFor([{ name: 'OTHER' }, { name: 'vol_persistence' }]));

    render(<FactorReportPanel initialDataset="l2_factors" initialCode="vol_persistence" />);
    await screen.findByTestId('detail');

    // 关键：数据集清单（alpha_library 在前）是**异步**回来的，它触发的重渲染会在
    // 首帧之后才把 dataset 改写掉。只断言「首帧选对了」等于没测——那个改写发生时
    // 用例早就通过了。这里显式把微任务/宏任务跑完，再看**终态**。
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });

    expect(screen.getByTestId('detail').getAttribute('data-factor')).toBe('vol_persistence');
    // 整个过程只该问过 l2_factors 这一个数据集；出现过 alpha_library 就是被改写了
    expect(summaryMock.mock.calls.map((c) => (c[0] as { dataset: string }).dataset)).toEqual(['l2_factors']);
  });

  test('点名的因子不在快照里：明说找不到，**不**回落到第一个因子', async () => {
    render(<FactorReportPanel initialDataset="alpha_library" initialCode="ghost_factor" />);

    expect(await screen.findByText(/该因子不在/)).toBeTruthy();
    expect(screen.getByText('ghost_factor')).toBeTruthy();
    // 关键：没有偷偷选中 f1
    expect(screen.queryByTestId('detail')).toBeNull();
    expect(screen.getByTestId('rank-list').getAttribute('data-selected')).toBeNull();
  });

  test('不传深链：保持原行为，选第一个可用数据集的第一个因子', async () => {
    render(<FactorReportPanel />);

    const detail = await screen.findByTestId('detail');
    expect(detail.getAttribute('data-factor')).toBe('f1');
    expect(summaryMock).toHaveBeenCalledWith(expect.objectContaining({ dataset: 'alpha_library' }));
  });

  test('落空后用户自己挑一个因子：明细要顶掉横幅，不能是死路', async () => {
    summaryMock.mockResolvedValue(summaryFor([{ name: 'f1' }, { name: 'f2' }]));
    render(<FactorReportPanel initialDataset="alpha_library" initialCode="ghost_factor" />);

    expect(await screen.findByText(/该因子不在/)).toBeTruthy();

    fireEvent.click(screen.getByText('桩-选 f2'));

    // 横幅之外还必须**看得到明细**：横幅若只是被 selected 遮着不撤，用户点了
    // 就像没反应（明细其实已经取回来了），连刷新都洗不掉。
    const detail = await screen.findByTestId('detail');
    expect(detail.getAttribute('data-factor')).toBe('f2');
    expect(screen.queryByText(/该因子不在/)).toBeNull();
  });

  test('慢的摘要后到：不把过期那一份的清单与选中项写到界面上', async () => {
    let releaseSlow: (v: unknown) => void = () => undefined;
    summaryMock.mockImplementation((p: { dataset: string }) =>
      p.dataset === 'alpha_library'
        ? new Promise((r) => { releaseSlow = r; })
        : Promise.resolve(summaryFor([{ name: 'l2_only' }])),
    );

    // 深链落到 alpha_library（大快照，慢），它还没回来用户就切到了 L2
    render(<FactorReportPanel initialDataset="alpha_library" />);
    // 数据集切换 2026-10-09 收敛到顶栏下拉：先展开再点选项
    fireEvent.click(await screen.findByTestId('report-dataset-dropdown'));
    fireEvent.click(await screen.findByTestId('report-dataset-option-l2_factors'));

    const detail = await screen.findByTestId('detail');
    expect(detail.getAttribute('data-factor')).toBe('l2_only');

    // 放行那份慢的：它是过期请求，一个字都不该写进去
    releaseSlow(summaryFor([{ name: 'alpha_only' }]));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });

    // 断言终态：左侧列表、选中项、明细必须都还是 L2 的
    expect(screen.getByTestId('rank-list').getAttribute('data-selected')).toBe('l2_only');
    expect(screen.getByTestId('detail').getAttribute('data-factor')).toBe('l2_only');
    expect(screen.getByTestId('rank-list').getAttribute('data-n')).toBe('1');
  });
});

describe('FactorReportPanel：快照落后与重建（2026-10-09）', () => {
  test('盘上有新因子：顶栏徽章报出差额，点「重建快照」发起构建并自动刷新', async () => {
    datasetsMock.mockResolvedValue({
      default: 'alpha_library',
      items: [
        {
          dataset: 'alpha_library', label: 'Alpha 库', available: true,
          stale: true, stale_reason: '盘上已有 102 个因子（快照 80 个，新增 22）',
          disk_n_factors: 102, snapshot_n_factors: 80, n_factors: 80,
        },
        { dataset: 'l2_factors', label: 'L2 因子', available: true },
      ],
    });
    const datasetsCallsBefore = () => datasetsMock.mock.calls.length;

    render(<FactorReportPanel />);

    // 徽章：把「102 vs 80」的差额直接摆出来，而不是只有一句「落后了」
    const badge = await screen.findByTestId('report-stale-badge');
    expect(badge.textContent).toContain('+22');

    // 下拉里同样标了落后 + 原因（切数据集之前就能看到值不值得重建）
    fireEvent.click(screen.getByTestId('report-dataset-dropdown'));
    expect(await screen.findByText('快照落后')).toBeTruthy();
    expect(screen.getByText(/盘上已有 102 个因子/)).toBeTruthy();

    const before = datasetsCallsBefore();
    fireEvent.click(screen.getByTestId('report-rebuild'));

    // 发起的构建目标 = 正在浏览的数据集（不是「第一个可用集」）
    await waitFor(() => expect(startBuildMock).toHaveBeenCalledWith('alpha_library'));
    // 轮询首拍即 running:false → 重新拉数据集清单（徽章数据源随之更新）
    await waitFor(() => expect(datasetsCallsBefore()).toBeGreaterThan(before));
  });

  test('盘上无变化：不出现落后徽章（避免「永远是脏的」噪声）', async () => {
    render(<FactorReportPanel />);
    await screen.findByTestId('detail');
    expect(screen.queryByTestId('report-stale-badge')).toBeNull();
    expect(startBuildMock).not.toHaveBeenCalled();
  });

  test('快照尚未生成的数据集也可选中：选中 → 横幅给重建入口，重建目标就是它', async () => {
    // 回归（2026-10-09 实跑抓获）：选项曾对 unavailable 数据集 disabled ——
    // 而重建按钮的目标 = 当前数据集，禁选 = 把唯一需要重建的数据集锁死，死路。
    datasetsMock.mockResolvedValue({
      default: 'alpha_library',
      items: [
        { dataset: 'alpha_library', label: 'Alpha 库', available: true },
        { dataset: 'rd_mined', label: 'RD-Agent 挖掘因子', available: false, disk_n_factors: 102, stale: false },
      ],
    });
    summaryMock.mockImplementation((p: { dataset: string }) =>
      p.dataset === 'rd_mined'
        ? Promise.resolve({ available: false, dataset: 'rd_mined', reason: '数据集 rd_mined 的快照尚未生成' })
        : Promise.resolve(summaryFor([{ name: 'f1' }])),
    );

    render(<FactorReportPanel />);
    await screen.findByTestId('detail');

    fireEvent.click(screen.getByTestId('report-dataset-dropdown'));
    const opt = await screen.findByTestId('report-dataset-option-rd_mined');
    expect(opt.hasAttribute('disabled')).toBe(false);
    fireEvent.click(opt);

    // 选中后：横幅说明不可用 + 带重建按钮；且下拉项里提示了盘上已有多少因子
    expect(await screen.findByText('因子报告快照不可用')).toBeTruthy();
    fireEvent.click(screen.getByTestId('report-unavailable-rebuild'));

    // 重建目标 = rd_mined（不是「第一个可用数据集」）
    await waitFor(() => expect(startBuildMock).toHaveBeenCalledWith('rd_mined'));
  });
});
