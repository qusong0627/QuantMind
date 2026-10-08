/**
 * 组合实验室 —— 页面的四条不能坏的行为线：
 *
 * 1. **只给选有面板的因子**：组合优化吃价值级面板（zscore+fret），无面板因子
 *    选了也必然被后端 400 —— UI 从源头不给选，而不是让用户提交后吃错。
 * 2. **< 2 个因子按钮禁用**（与后端 MIN_FACTORS=2 镜像），非法 seed 就地拦截、
 *    不发请求 —— 校验错误不许变成一次无意义的建行。
 * 3. **提交 → 轮询 → done 渲染闭环**：权重（正负号）、train/valid 指标对比、
 *    valid 净值曲线、seed 回执都要出现；valid 缺值显「—」而不是 0。
 * 4. **failed 行如实显示错误**；历史组合点击进详情。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { ComboLabTab } from '../ComboLabTab';
import type {
  ComboDetail,
  ComboWindowMetrics,
  PoolFactorRow,
} from '../../services-v2/api';

const { getPoolFactorsMock, listCombosMock, getComboMock, optimizeComboMock } = vi.hoisted(
  () => ({
    getPoolFactorsMock: vi.fn(),
    listCombosMock: vi.fn(),
    getComboMock: vi.fn(),
    optimizeComboMock: vi.fn(),
  }),
);

vi.mock('../../services-v2/api', () => ({
  getPoolFactors: getPoolFactorsMock,
  listCombos: listCombosMock,
  getCombo: getComboMock,
  optimizeCombo: optimizeComboMock,
}));

// jsdom 没有真 canvas：只验证曲线数据契约（点数）而不是像素。
vi.mock('echarts-for-react', () => ({
  default: ({ option }: { option: { series?: { data?: unknown[] }[] } }) => (
    <div
      data-testid="combo-curve"
      data-points={String(option?.series?.[0]?.data?.length ?? 0)}
    />
  ),
}));

const ok = <T,>(data: T) => ({ success: true as const, data });

function mkPoolRow(over: Partial<PoolFactorRow> & { factorId: string }): PoolFactorRow {
  return {
    factorName: over.factorId,
    factorFormulation: 'ts_mean(close,5)',
    ic: 0.0321,
    rankIc: null,
    icir: 0.45,
    pfs: 0.85,
    poolScore: 0.61,
    novelty: 0.5,
    maxPoolCorr: 0.2,
    maxPoolCorrWith: null,
    diversityContrib: null,
    timesRetrieved: 1,
    lastRetrievedAt: null,
    hasPanel: true,
    createdAt: null,
    updatedAt: null,
    gates: null,
    ...over,
  };
}

function mkMetrics(over: Partial<ComboWindowMetrics> = {}): ComboWindowMetrics {
  return {
    meanRankIc: 0.0412,
    rankIcir: 0.53,
    turnoverDaily: 0.21,
    annTurnover: 52.9,
    annReturnNet: 0.081,
    sharpeNet: 1.2,
    maxDrawdownNet: -0.13,
    nDays: 30,
    nObs: 240,
    curve: null,
    config: null,
    ...over,
  };
}

const ROW_WITH_PANEL = mkPoolRow({ factorId: 'f1', factorName: 'alpha_001' });
const ROW_NO_PANEL = mkPoolRow({ factorId: 'f2', factorName: 'alpha_002', hasPanel: false });
const ROW_WITH_PANEL_2 = mkPoolRow({ factorId: 'f3', factorName: 'alpha_003' });

const PENDING_RESULT = ok({ comboId: 'comboaaaa111', status: 'pending', pid: 4242 });

const RUNNING_DETAIL: ComboDetail = {
  comboId: 'comboaaaa111',
  market: 'a_share',
  universe: '',
  name: '',
  factorIds: ['f1', 'f3'],
  weights: {},
  trainWindow: null,
  trainMetrics: null,
  validMetrics: null,
  status: 'running',
  error: null,
  createdAt: '2026-10-08T02:00:00Z',
  updatedAt: '2026-10-08T02:00:30Z',
};

const DONE_DETAIL: ComboDetail = {
  comboId: 'comboaaaa111',
  market: 'a_share',
  universe: '',
  name: '动量试验',
  factorIds: ['f1', 'f3'],
  weights: { f1: 0.8123, f3: -0.1877 },
  trainWindow: 'train 21d 2026-01-05~2026-02-02 / valid 9d 2026-02-03~2026-02-16',
  trainMetrics: mkMetrics({
    config: {
      seed: 7,
      popsize: 15,
      maxiter: 40,
      tol: 0.01,
      maxDays: 120,
      trainRatio: 0.7,
      timeBudgetS: 180,
      costRate: 0.002,
      converged: true,
      nEvaluations: 611,
    },
  }),
  validMetrics: mkMetrics({
    meanRankIc: null, // 缺值必须显「—」，绝不伪造 0
    curve: { dates: ['2026-02-03', '2026-02-04'], values: [1.0, 1.0234] },
  }),
  status: 'done',
  error: null,
  createdAt: '2026-10-08T02:00:00Z',
  updatedAt: '2026-10-08T02:01:00Z',
};

const FAILED_DETAIL: ComboDetail = {
  ...DONE_DETAIL,
  status: 'failed',
  error: 'ValueError: 以下因子面板缺收益列（fret），请先重算面板',
  weights: {},
  trainMetrics: null,
  validMetrics: null,
};

const HISTORY_ITEM = {
  comboId: 'comboaaaa111',
  name: '动量试验',
  market: 'a_share',
  universe: '',
  nFactors: 2,
  status: 'done',
  error: null,
  trainWindow: null,
  trainMeanRankIc: 0.04,
  validMeanRankIc: 0.03,
  createdAt: '2026-10-08T02:00:00Z',
  updatedAt: '2026-10-08T02:01:00Z',
};

beforeEach(() => {
  Object.values({ getPoolFactorsMock, listCombosMock, getComboMock, optimizeComboMock }).forEach(
    (m) => m.mockReset(),
  );

  getPoolFactorsMock.mockResolvedValue(
    ok({ total: 3, items: [ROW_WITH_PANEL, ROW_NO_PANEL, ROW_WITH_PANEL_2], limit: 500, offset: 0 }),
  );
  listCombosMock.mockResolvedValue(ok({ total: 0, items: [] }));
  getComboMock.mockResolvedValue(ok(RUNNING_DETAIL));
  optimizeComboMock.mockResolvedValue(PENDING_RESULT);
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderTab() {
  return render(<ComboLabTab market="a_share" universe="" pollMs={20} />);
}

const picker = () => within(screen.getByTestId('combo-picker'));

describe('ComboLabTab 因子选择', () => {
  test('只列有面板的因子；无面板因子从候选里消失', async () => {
    renderTab();
    expect(await screen.findByText('alpha_001')).toBeTruthy();
    expect(screen.getByText('alpha_003')).toBeTruthy();
    expect(screen.queryByText('alpha_002')).toBeNull();
  });

  test('选 1 个时「开始优化」禁用；选到 2 个才可用', async () => {
    renderTab();
    const btn = await screen.findByRole('button', { name: /开始优化/ });
    expect(btn.hasAttribute('disabled')).toBe(true);

    fireEvent.click(picker().getByRole('button', { name: /alpha_001/ }));
    expect(screen.getByRole('button', { name: /开始优化/ }).hasAttribute('disabled')).toBe(true);

    fireEvent.click(picker().getByRole('button', { name: /alpha_003/ }));
    expect(screen.getByRole('button', { name: /开始优化/ }).hasAttribute('disabled')).toBe(false);
    expect(optimizeComboMock).not.toHaveBeenCalled();
  });

  test('seed 非数字就地拦截，不发请求', async () => {
    renderTab();
    await screen.findByText('alpha_001');
    fireEvent.click(picker().getByRole('button', { name: /alpha_001/ }));
    fireEvent.click(picker().getByRole('button', { name: /alpha_003/ }));
    fireEvent.change(screen.getByPlaceholderText('如 42'), { target: { value: 'abc' } });
    fireEvent.click(screen.getByRole('button', { name: /开始优化/ }));

    expect(await screen.findByText(/随机种子须为非负整数/)).toBeTruthy();
    expect(optimizeComboMock).not.toHaveBeenCalled();
  });
});

describe('ComboLabTab 提交与轮询', () => {
  test('提交参数正确；pending→running→done 后渲染权重/指标/曲线/seed 回执', async () => {
    getComboMock
      .mockResolvedValueOnce(ok(RUNNING_DETAIL))
      .mockResolvedValue(ok(DONE_DETAIL));
    renderTab();
    await screen.findByText('alpha_001');
    fireEvent.click(picker().getByRole('button', { name: /alpha_001/ }));
    fireEvent.click(picker().getByRole('button', { name: /alpha_003/ }));
    fireEvent.change(screen.getByPlaceholderText('如：动量×反转 试验'), {
      target: { value: '动量试验' },
    });
    fireEvent.change(screen.getByPlaceholderText('如 42'), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: /开始优化/ }));

    await waitFor(() =>
      expect(optimizeComboMock).toHaveBeenCalledWith({
        market: 'a_share',
        universe: '',
        factorIds: ['f1', 'f3'],
        name: '动量试验',
        seed: 7,
      }),
    );

    // 轮询到 done：权重（正负号格式）、train/valid 对比、曲线、seed 回执
    expect(await screen.findByText('+0.8123')).toBeTruthy();
    expect(screen.getByText('-0.1877')).toBeTruthy();
    expect(screen.getByText('0.0412')).toBeTruthy(); // train 日均 rank-IC
    expect(screen.getAllByText('—').length).toBeGreaterThan(0); // valid 缺值
    expect(screen.getByTestId('combo-curve').getAttribute('data-points')).toBe('2');
    expect(screen.getByText('seed=7')).toBeTruthy();
    expect(screen.getByText(/train 21d/)).toBeTruthy();
  });

  test('failed 行如实显示错误原因', async () => {
    getComboMock.mockResolvedValue(ok(FAILED_DETAIL));
    renderTab();
    await screen.findByText('alpha_001');
    fireEvent.click(picker().getByRole('button', { name: /alpha_001/ }));
    fireEvent.click(picker().getByRole('button', { name: /alpha_003/ }));
    fireEvent.click(screen.getByRole('button', { name: /开始优化/ }));

    expect(await screen.findByText(/面板缺收益列/)).toBeTruthy();
  });
});

describe('ComboLabTab 历史组合', () => {
  test('点击历史项载入对应详情', async () => {
    listCombosMock.mockResolvedValue(ok({ total: 1, items: [HISTORY_ITEM] }));
    getComboMock.mockResolvedValue(ok(DONE_DETAIL));
    renderTab();

    const item = await screen.findByRole('button', { name: /动量试验/ });
    fireEvent.click(item);
    await waitFor(() => expect(getComboMock).toHaveBeenCalledWith('comboaaaa111'));
    expect(await screen.findByText('+0.8123')).toBeTruthy();
  });

  test('重开同一历史行：补拉一次详情，不会卡回「加载中…」', async () => {
    listCombosMock.mockResolvedValue(ok({ total: 1, items: [HISTORY_ITEM] }));
    getComboMock.mockResolvedValue(ok(DONE_DETAIL));
    renderTab();

    const item = await screen.findByRole('button', { name: /动量试验/ });
    fireEvent.click(item);
    expect(await screen.findByText('+0.8123')).toBeTruthy();

    const before = getComboMock.mock.calls.length;
    fireEvent.click(item); // 再点同一行：state 不变 effect 不重跑，必须手动补拉
    await waitFor(() => expect(getComboMock.mock.calls.length).toBe(before + 1));
    expect(screen.queryByText('加载中…')).toBeNull();
    expect(screen.getByText('+0.8123')).toBeTruthy();
  });
});

describe('ComboLabTab 轮询纪律', () => {
  test('done 后停表：不再有新的详情请求', async () => {
    getComboMock.mockResolvedValue(ok(DONE_DETAIL));
    renderTab();
    await screen.findByText('alpha_001');
    fireEvent.click(picker().getByRole('button', { name: /alpha_001/ }));
    fireEvent.click(picker().getByRole('button', { name: /alpha_003/ }));
    fireEvent.click(screen.getByRole('button', { name: /开始优化/ }));

    expect(await screen.findByText('+0.8123')).toBeTruthy();
    const callsAtDone = getComboMock.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 80)); // ≥ 4 个 pollMs 窗口
    expect(getComboMock.mock.calls.length).toBe(callsAtDone);
  });

  test('详情拉取失败即停表：错误可见且不再打请求', async () => {
    getComboMock.mockResolvedValue({ success: false, error: '组合详情获取失败' });
    renderTab();
    await screen.findByText('alpha_001');
    fireEvent.click(picker().getByRole('button', { name: /alpha_001/ }));
    fireEvent.click(picker().getByRole('button', { name: /alpha_003/ }));
    fireEvent.click(screen.getByRole('button', { name: /开始优化/ }));

    expect(await screen.findByText('组合详情获取失败')).toBeTruthy();
    const calls = getComboMock.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(getComboMock.mock.calls.length).toBe(calls);
  });
});

describe('ComboLabTab 作用域守卫', () => {
  test('市场切换后在途的旧响应被丢弃，不覆盖新市场候选', async () => {
    let resolveOld: (value: unknown) => void = () => {};
    getPoolFactorsMock
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOld = resolve;
          }),
      )
      .mockResolvedValueOnce(
        ok({
          total: 1,
          items: [mkPoolRow({ factorId: 'us1', factorName: 'us_alpha' })],
          limit: 500,
          offset: 0,
        }),
      );

    const { rerender } = render(<ComboLabTab market="a_share" universe="" pollMs={20} />);
    rerender(<ComboLabTab market="us_stock" universe="" pollMs={20} />);
    expect(await screen.findByText('us_alpha')).toBeTruthy();

    // 旧市场的响应此刻才回来 —— 不许把 cn_alpha 画进 us_stock 的候选列表
    resolveOld(
      ok({
        total: 1,
        items: [mkPoolRow({ factorId: 'cn1', factorName: 'cn_alpha' })],
        limit: 500,
        offset: 0,
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.queryByText('cn_alpha')).toBeNull();
    expect(screen.getByText('us_alpha')).toBeTruthy();
  });

  test('seed 超 2^32−1 就地拦截（与后端 _MAX_SEED 镜像），不发请求', async () => {
    renderTab();
    await screen.findByText('alpha_001');
    fireEvent.click(picker().getByRole('button', { name: /alpha_001/ }));
    fireEvent.click(picker().getByRole('button', { name: /alpha_003/ }));
    fireEvent.change(screen.getByPlaceholderText('如 42'), { target: { value: '5000000000' } });
    fireEvent.click(screen.getByRole('button', { name: /开始优化/ }));

    expect(await screen.findByText(/不超过 4294967295/)).toBeTruthy();
    expect(optimizeComboMock).not.toHaveBeenCalled();
  });
});
