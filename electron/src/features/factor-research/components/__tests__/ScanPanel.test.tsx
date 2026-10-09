/**
 * 「扫描因子来源」面板 —— 行为契约。
 *
 * 这个面板存在的唯一理由：新挖的因子写进 quantdb 后，界面看不见（因子目录是**快照
 * 产物**，不是每次读盘现算）；而重算一次私人库要 5~15 分钟并重写 5.5GB 宽表。
 * 于是它必须先回答「有什么新的」，再由用户决定付不付这次重算。
 *
 * 因此这里守的不是样式，而是三件事：
 * 1. 差异**看得见**：新增按来源库分组、消失单独一列，计数与后端一致；
 * 2. 它**只扫描**：面板里绝不能悄悄发起重算（那等于把"先看看"变成"先算 15 分钟"）；
 *    要重算必须走用户点按钮、并交给既有快照面板（进度/日志都在那边）；
 * 3. 后端的话**原样传到脸上**：快照不是 auto 建的要说清楚（否则新增数会显得离谱），
 *    报错的中文 detail 要显示出来而不是"扫描失败"。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ScanPanel } from '../ScanPanel';
import type { ScanDiff } from '../../services/factorResearchService';

const { scanMock, promoteMock, promoteStatusMock } = vi.hoisted(() => ({
  scanMock: vi.fn(),
  promoteMock: vi.fn(),
  promoteStatusMock: vi.fn(),
}));

vi.mock('../../services/factorResearchService', async () => {
  const actual = await vi.importActual<typeof import('../../services/factorResearchService')>(
    '../../services/factorResearchService',
  );
  return { ...actual, getScanSources: scanMock, postPromote: promoteMock, getPromoteStatus: promoteStatusMock };
});

const DIFF: ScanDiff = {
  new: [
    { code: 'gap_mined_01', library: 'gap_mined', library_label: '空档挖掘因子' },
    { code: 'gap_mined_02', library: 'gap_mined', library_label: '空档挖掘因子' },
    { code: 'alpha_new', library: 'alpha_library', library_label: 'Alpha 因子库' },
  ],
  missing: [{ code: 'gone_1', library: 'l2_factors', library_label: 'L2 因子' }],
  new_by_library: { gap_mined: 2, alpha_library: 1 },
  unchanged_count: 2751,
  discovered_count: 2754,
  catalog_count: 2752,
  dataset: 'private',
  snapshot_at: '2026-09-19T11:22:33',
  snapshot_source: 'auto',
};

function renderPanel(over: Partial<React.ComponentProps<typeof ScanPanel>> = {}) {
  const onRebuild = vi.fn();
  const onClose = vi.fn();
  render(<ScanPanel onRebuild={onRebuild} onClose={onClose} {...over} />);
  return { onRebuild, onClose };
}

/** 毕业管道快照：CUSTOM 102 个、CN 80 个 → 22 个卡在桥前（2026-10-09 实测形态） */
const PIPELINE_PENDING = {
  state: 'pending' as const,
  custom_n_factors: 102,
  cn_n_factors: 80,
  pending_factors: 22,
  pending_factor_names: ['rd_alpha_001', 'rd_alpha_002'],
  pending_partitions: 1637,
  custom_last_dt: '20261008',
  cn_last_dt: '20260929',
};

beforeEach(() => {
  scanMock.mockReset();
  promoteMock.mockReset();
  promoteStatusMock.mockReset();
  scanMock.mockResolvedValue(DIFF);
});

describe('ScanPanel', () => {
  test('新增按来源库分组列出，计数与后端一致', async () => {
    renderPanel();

    expect(await screen.findByText('空档挖掘因子')).toBeTruthy();
    expect(screen.getByText('alpha_new')).toBeTruthy();
    // 分组计数：空档挖掘 2 个
    expect(screen.getByTestId('scan-new-gap_mined').textContent).toContain('2');
    expect(screen.getByTestId('scan-new-gap_mined').textContent).toContain('gap_mined_01');
    expect(screen.getByTestId('scan-new-alpha_library').textContent).toContain('alpha_new');
  });

  test('总量与差异数同时显示（新增 3 / 消失 1 / 未变 2751）', async () => {
    renderPanel();

    await screen.findByTestId('scan-summary');
    const s = screen.getByTestId('scan-summary').textContent || '';
    expect(s).toContain('2754'); // 盘上
    expect(s).toContain('2752'); // 快照
    expect(s).toContain('3'); // 新增
    expect(s).toContain('1'); // 消失
    expect(s).toContain('2751'); // 未变
  });

  test('消失项带来源库标签（盘上已无它，库名只能来自快照目录）', async () => {
    renderPanel();

    expect(await screen.findByText('gone_1')).toBeTruthy();
    expect(screen.getByText(/L2 因子/)).toBeTruthy();
  });

  test('扫描是只读的：面板挂载/重扫都不得发起重算', async () => {
    const { onRebuild } = renderPanel();

    await screen.findByTestId('scan-summary');
    fireEvent.click(screen.getByTestId('scan-rescan'));
    await waitFor(() => expect(scanMock).toHaveBeenCalledTimes(2));

    expect(onRebuild).not.toHaveBeenCalled();
  });

  test('「重算快照」交给既有快照面板（onRebuild），不在这里自己发构建请求', async () => {
    const { onRebuild } = renderPanel();

    fireEvent.click(await screen.findByText('重算快照'));

    expect(onRebuild).toHaveBeenCalledTimes(1);
  });

  test('无差异时显示「已是最新」，且不列出空分组', async () => {
    scanMock.mockResolvedValue({
      ...DIFF,
      new: [],
      missing: [],
      new_by_library: {},
      discovered_count: 2752,
      unchanged_count: 2752,
    });

    renderPanel();

    expect(await screen.findByText(/已是最新/)).toBeTruthy();
    expect(screen.queryByTestId('scan-new-alpha_library')).toBeNull();
  });

  test('快照不是 auto 建的时要说清楚（否则「新增」多到离谱没法解释）', async () => {
    scanMock.mockResolvedValue({ ...DIFF, snapshot_source: 'l1l2' });

    renderPanel();

    const note = await screen.findByTestId('scan-source-note');
    expect(note.textContent).toContain('l1l2');
    expect(note.textContent).toMatch(/全量|auto/);
  });

  test('快照是 auto 建的不出这条提示（常态不该有噪声）', async () => {
    renderPanel();

    await screen.findByTestId('scan-summary');
    expect(screen.queryByTestId('scan-source-note')).toBeNull();
  });

  test('后端的中文 detail 要显示出来，而不是一句「扫描失败」', async () => {
    scanMock.mockRejectedValue(new Error('因子研究接口失败 503: {"detail":"6_ml_datasets 目录缺失: /data/quantdb/6_ml_datasets"}'));

    renderPanel();

    const err = await screen.findByTestId('scan-error');
    expect(err.textContent).toContain('6_ml_datasets');
  });

  test('扫描中显示进行态（秒级，但不显示会让重复点击）', async () => {
    let release: (v: ScanDiff) => void = () => {};
    scanMock.mockReturnValue(new Promise<ScanDiff>((r) => { release = r; }));

    renderPanel();

    expect(screen.getByTestId('scan-rescan').textContent).toContain('扫描中');
    release(DIFF);
    await screen.findByTestId('scan-summary');
  });

  test('关闭按钮回上一屏（onClose）', async () => {
    const { onClose } = renderPanel();

    await screen.findByTestId('scan-summary');
    fireEvent.click(screen.getByTestId('scan-close'));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('ScanPanel：毕业管道（2026-10-09）', () => {
  test('有因子卡在毕业桥：漏斗数与因子名摆出来，一键毕业后自动重扫并转安静绿行', async () => {
    scanMock.mockResolvedValueOnce({ ...DIFF, pipeline: PIPELINE_PENDING });
    scanMock.mockResolvedValue({
      ...DIFF,
      pipeline: { ...PIPELINE_PENDING, state: 'synced', cn_n_factors: 102, pending_factors: 0, pending_factor_names: [] },
    });
    promoteMock.mockResolvedValue({ started: true, running: true });
    // 轮询首拍即完成态：无需推进计时器
    promoteStatusMock.mockResolvedValue({ running: false, pid: null, step: '', log_tail: [] });

    renderPanel();

    const block = await screen.findByTestId('scan-pipeline');
    expect(block.textContent).toContain('22'); // 待毕业
    expect(block.textContent).toContain('1637'); // 涉及分区
    expect(screen.getByText('rd_alpha_001')).toBeTruthy();

    fireEvent.click(screen.getByTestId('scan-promote'));
    await waitFor(() => expect(promoteMock).toHaveBeenCalledTimes(1));
    // 毕业完成后自动重扫（scanMock 第 2 次），状态转 synced
    await waitFor(() => expect(scanMock).toHaveBeenCalledTimes(2));
    expect((await screen.findByTestId('scan-pipeline')).textContent).toContain('已同步');
    expect(screen.queryByTestId('scan-promote')).toBeNull();
  });

  test('没有任何卡住：只留一行绿字，不给按钮（没有可点的动作）', async () => {
    scanMock.mockResolvedValue({
      ...DIFF,
      pipeline: { ...PIPELINE_PENDING, state: 'synced', cn_n_factors: 102, pending_factors: 0, pending_factor_names: [] },
    });

    renderPanel();

    const block = await screen.findByTestId('scan-pipeline');
    expect(block.textContent).toContain('已同步');
    expect(screen.queryByTestId('scan-promote')).toBeNull();
  });

  test('pipeline 为 null（部署没有毕业桥脚本）时整段隐藏，不凭空造状态', async () => {
    renderPanel();

    await screen.findByTestId('scan-summary');
    expect(screen.queryByTestId('scan-pipeline')).toBeNull();
  });
});
