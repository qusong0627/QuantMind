/**
 * 筛选页签的勾选与跳转契约。
 *
 * 这个页签的钱都花在**两条命名空间之间的那道缝**上：它列的是盘上算出来的因子，
 * 而勾选（注册）和点进去（单因子分析 / 因子报告）都活在快照目录的空间里。
 * 实测 362 个保留因子里 331 个在私人库目录、31 个在经典库目录、0 个重叠——
 * 所以这里守的不是「渲染了几个格子」，而是几件**点错了不会报错、只会静默做错事**
 * 的性质：
 *
 * 1. 经典库的行**不能**被勾进注册清单。后端按私人库的 factors_meta 解析来源库，
 *    经典库那份是 None 且 l2 是「动量」这类中文字面量，放行过去只会得到一屏 skipped；
 * 2. 跳转必须带上**这一行自己的**数据集。拿经典库的 code 去问私人库的快照，
 *    界面只会显示「该因子不可用」，不会告诉你库选错了；
 * 3. 快照里没有的因子要保持可见但不可点。它们确实通过了筛选，平白消失会让人
 *    以为筛选少算了；
 * 4. 报告按钮只在该子库真有报告快照时渲染——因子报告是另一个脚本按自己的清单
 *    生成的，`factor_research` 在那边压根不存在，点了会落到别的因子上。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ScreeningTab } from '../ScreeningTab';
import type { FactorLocation } from '../ScreeningTab';

const { screeningMock, datasetsMock } = vi.hoisted(() => ({
  screeningMock: vi.fn(),
  datasetsMock: vi.fn(),
}));

vi.mock('../../services/factorResearchService', () => ({ getScreening: screeningMock }));
vi.mock('../../services/factorReportService', () => ({ getFactorDatasets: datasetsMock }));

/** 私人库因子：可勾、可点、有报告。 */
const PRIVATE_NAME = 'a101_001';
/** 经典库因子：可点、不可勾、无报告（factor_research 在报告那边不存在）。 */
const CLASSIC_NAME = 'ABTURN';
/** 快照目录里还没有它（新挖到但没重算快照）：不可点、不可勾、无报告。 */
const ORPHAN_NAME = 'orphan_x';

const INDEX = new Map<string, FactorLocation>([
  [PRIVATE_NAME, { dataset: 'private', code: PRIVATE_NAME }],
  [CLASSIC_NAME, { dataset: 'classic', code: CLASSIC_NAME }],
]);

const SCREENING = {
  generated_at: '2026-10-07T10:00:00Z',
  gates: { min_abs_ic: 0.02, min_abs_icir: 0.3, corr_threshold: 0.8 },
  cross_corr: '全样本截面',
  counts: { candidates: 400, kept: 3, gated_out: 10, deduped: 5, total_considered: 415 },
  kept: [
    { name: PRIVATE_NAME, display_name: 'A101 001', library: 'alpha_library', ic_mean: 0.031, icir: 0.35, turnover: 0.2 },
    { name: CLASSIC_NAME, display_name: '换手率', library: 'factor_research', ic_mean: -0.022, icir: -0.41, turnover: 0.3 },
    { name: ORPHAN_NAME, display_name: '孤儿因子', library: 'l1_factors', ic_mean: 0.011, icir: 0.21, turnover: 0.1 },
  ],
  dropped_gated: [{ name: 'g1', library: 'l1_factors', reason: '|IC| 不足' }],
  dropped_duplicate: [{ name: 'd1', library: 'l1_factors', duplicate_of: 'keep1', abs_corr: 0.93 }],
};

type Props = React.ComponentProps<typeof ScreeningTab>;

function renderTab(over: Partial<Props> = {}) {
  const onToggle = vi.fn();
  const onToggleMany = vi.fn();
  const onOpenSingle = vi.fn();
  const onOpenReport = vi.fn();
  const onRegister = vi.fn();
  render(
    <ScreeningTab
      index={INDEX}
      selected={[]}
      onToggle={onToggle}
      onToggleMany={onToggleMany}
      onOpenSingle={onOpenSingle}
      onOpenReport={onOpenReport}
      canRegisterToTraining
      onRegister={onRegister}
      catalogStatus="ready"
      {...over}
    />,
  );
  return { onToggle, onToggleMany, onOpenSingle, onOpenReport, onRegister };
}

beforeEach(() => {
  screeningMock.mockReset();
  datasetsMock.mockReset();
  screeningMock.mockResolvedValue(SCREENING);
  // 报告那边有快照的数据集：注意**没有** factor_research
  datasetsMock.mockResolvedValue({
    default: 'alpha_library',
    items: [
      { dataset: 'alpha_library', label: 'Alpha 库', available: true },
      { dataset: 'l1_factors', label: 'L1 因子', available: true },
      { dataset: 'jq110', label: 'JQ110', available: false },
    ],
  });
});

describe('ScreeningTab：勾选只对真能注册的行开放', () => {
  test('私人库的行点了会回调勾选', async () => {
    const { onToggle } = renderTab();

    fireEvent.click(await screen.findByTitle('加入注册清单'));

    expect(onToggle).toHaveBeenCalledWith(PRIVATE_NAME);
  });

  test('经典库的行点勾选框没有任何回调，且说明了原因', async () => {
    const { onToggle } = renderTab();

    const box = await screen.findByTitle(/经典因子库不支持注册/);
    fireEvent.click(box);

    expect(onToggle).not.toHaveBeenCalled();
  });

  test('快照目录里没有的行不能勾，理由是「定位不到来源库」', async () => {
    const { onToggle } = renderTab();

    const box = await screen.findByTitle(/无法勾选｜该因子不在快照目录里/);
    fireEvent.click(box);

    expect(onToggle).not.toHaveBeenCalled();
  });

  test('目录还没到手时说「正在读取」，不说「不在快照目录里」', async () => {
    renderTab({ catalogStatus: 'loading' });

    // 这两句是给用户的两条不同的指令：等一会 / 去重算快照（十分钟）。弄反了
    // 用户会去跑一次昂贵的重建，而问题只是目录还在路上。
    expect((await screen.findAllByTitle(/正在读取因子目录/)).length).toBeGreaterThan(0);
    expect(screen.queryByTitle(/不在快照目录里/)).toBeNull();
  });

  test('目录没取回来时说重试，绝不说「新挖到的因子还没重算快照」', async () => {
    renderTab({ catalogStatus: 'degraded' });

    expect((await screen.findAllByTitle(/因子目录没取回来/)).length).toBeGreaterThan(0);
    expect(screen.queryByTitle(/重算快照/)).toBeNull();
  });

  test('非管理员整个勾选列都不渲染（勾选在这个页签里只有注册一个用途）', async () => {
    renderTab({ canRegisterToTraining: false });

    await screen.findByText(PRIVATE_NAME);
    expect(screen.queryByTitle('加入注册清单')).toBeNull();
    expect(screen.queryByLabelText('全选可注册的因子')).toBeNull();
    expect(screen.queryByText(/注册到训练目录/)).toBeNull();
  });

  test('全选只覆盖本页**可注册**的行，经典库与找不到落点的都不在内', async () => {
    const { onToggleMany } = renderTab();

    fireEvent.click(await screen.findByLabelText('全选可注册的因子'));

    expect(onToggleMany).toHaveBeenCalledWith([PRIVATE_NAME], true);
  });

  test('已全选时表头变成「取消全选」，并带回取消语义', async () => {
    const { onToggleMany } = renderTab({ selected: [PRIVATE_NAME] });

    fireEvent.click(await screen.findByLabelText('取消全选'));

    expect(onToggleMany).toHaveBeenCalledWith([PRIVATE_NAME], false);
  });
});

describe('ScreeningTab：点行跳单因子分析', () => {
  test('私人库的行带去 private', async () => {
    const { onOpenSingle } = renderTab();

    fireEvent.click(await screen.findByText(PRIVATE_NAME));

    expect(onOpenSingle).toHaveBeenCalledWith(PRIVATE_NAME, 'private');
  });

  test('经典库的行带去 classic —— 不带对数据集就会查到另一个库去', async () => {
    const { onOpenSingle } = renderTab();

    fireEvent.click(await screen.findByText(CLASSIC_NAME));

    expect(onOpenSingle).toHaveBeenCalledWith(CLASSIC_NAME, 'classic');
  });

  test('快照里没有的因子仍然显示，但点了不动', async () => {
    const { onOpenSingle } = renderTab();

    const cell = await screen.findByText(ORPHAN_NAME);
    fireEvent.click(cell);

    expect(onOpenSingle).not.toHaveBeenCalled();
    // 它确实在清单里（通过筛选的因子不能平白消失）
    expect(cell).toBeTruthy();
  });
});

describe('ScreeningTab：报告入口只在该子库真有快照时出现', () => {
  test('alpha_library 的行有报告按钮，且带子库名与因子名', async () => {
    const { onOpenReport } = renderTab();

    fireEvent.click(await screen.findByLabelText(`在因子报告里打开 ${PRIVATE_NAME}`));

    expect(onOpenReport).toHaveBeenCalledWith('alpha_library', PRIVATE_NAME);
  });

  test('factor_research 的行没有报告按钮（报告快照里没有这个数据集）', async () => {
    renderTab();

    await screen.findByText(CLASSIC_NAME);
    expect(screen.queryByLabelText(`在因子报告里打开 ${CLASSIC_NAME}`)).toBeNull();
  });

  test('快照里没有的因子也没有报告按钮——报告同样按 code 找它', async () => {
    renderTab();

    await screen.findByText(ORPHAN_NAME);
    expect(screen.queryByLabelText(`在因子报告里打开 ${ORPHAN_NAME}`)).toBeNull();
  });

  test('报告数据集清单拉失败时一个报告按钮都不渲染（宁缺勿错）', async () => {
    datasetsMock.mockRejectedValue(new Error('boom'));
    renderTab();

    await screen.findByText(PRIVATE_NAME);
    await waitFor(() => expect(screen.queryByLabelText(`在因子报告里打开 ${PRIVATE_NAME}`)).toBeNull());
  });
});

describe('ScreeningTab：注册入口的开关', () => {
  test('没选任何因子时注册按钮禁用', async () => {
    renderTab();

    const btn = await screen.findByText(/注册到训练目录（0）/);
    expect(btn).toBeDisabled();
  });

  test('选中后按钮带数量、可点，并回调注册', async () => {
    const { onRegister } = renderTab({ selected: [PRIVATE_NAME] });

    const btn = await screen.findByText(/注册到训练目录（1）/);
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);

    expect(onRegister).toHaveBeenCalled();
  });

  test('「清空」把当前选中整批移除', async () => {
    const { onToggleMany } = renderTab({ selected: [PRIVATE_NAME] });

    fireEvent.click(await screen.findByText('清空'));

    expect(onToggleMany).toHaveBeenCalledWith([PRIVATE_NAME], false);
  });
});
