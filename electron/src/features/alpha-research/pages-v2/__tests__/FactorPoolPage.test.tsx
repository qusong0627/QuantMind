/**
 * 因子池页（P1）——页面的三条不能坏的行为线：
 *
 * 1. **缺失指标一律显「—」**：avgIc=null 时渲染「—」而不是 0（后端没算过 ≠ 算出来是 0，
 *    这是研究评分口径的老纪律，池页同样适用）。
 * 2. **刷新两键严格分离**：预演(dry_run=true)绝不触库；执行必须过 confirm，
 *    confirm 拒绝时绝不能发请求。
 * 3. **他人刷新任务只显示遮蔽占位**：状态文件是全局单份的，owner 不是自己时
 *    不得泄露 args/summary/日志——只回一句「由其他用户执行」。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { FactorPoolPage } from '../FactorPoolPage';

const {
  getPoolOverviewMock,
  getPoolFactorsMock,
  getPoolGraphMock,
  getPoolRefreshStatusMock,
  refreshPoolMock,
  getGateDescriptorsMock,
  getUniversesMock,
  listMarketsMock,
} = vi.hoisted(() => ({
  getPoolOverviewMock: vi.fn(),
  getPoolFactorsMock: vi.fn(),
  getPoolGraphMock: vi.fn(),
  getPoolRefreshStatusMock: vi.fn(),
  refreshPoolMock: vi.fn(),
  getGateDescriptorsMock: vi.fn(),
  getUniversesMock: vi.fn(),
  listMarketsMock: vi.fn(),
}));

vi.mock('../../services-v2/api', () => ({
  getPoolOverview: getPoolOverviewMock,
  getPoolFactors: getPoolFactorsMock,
  getPoolGraph: getPoolGraphMock,
  getPoolRefreshStatus: getPoolRefreshStatusMock,
  refreshPool: refreshPoolMock,
  getGateDescriptors: getGateDescriptorsMock,
  getUniverses: getUniversesMock,
}));

vi.mock('../../services/alphaAgentService', () => ({
  alphaAgentService: { listMarkets: listMarketsMock },
}));

// ECharts force 图在 jsdom 下没有真渲染价值：只验证页面→图组件的数据契约与点选回路。
vi.mock('../../components-v2/FactorPoolGraph', () => ({
  FactorPoolGraph: ({
    nodes,
    onSelectNode,
  }: {
    nodes: { factorId: string; factorName: string }[];
    onSelectNode?: (id: string) => void;
  }) => (
    <div data-testid="pool-graph">
      <span data-testid="pool-graph-node-count">{nodes.length}</span>
      {nodes.map((n) => (
        <button key={n.factorId} type="button" onClick={() => onSelectNode?.(n.factorId)}>
          {n.factorName}
        </button>
      ))}
    </div>
  ),
}));

// 组合实验室自持 api 调用与轮询（自己的测试文件覆盖行为）；页面侧只钉
// 「tab 挂载 + 作用域透传」，避免同一份 mock 工厂堆两套语义。
vi.mock('../../components-v2/ComboLabTab', () => ({
  ComboLabTab: ({ market, universe }: { market: string; universe: string }) => (
    <div data-testid="combo-lab">{`${market}|${universe}`}</div>
  ),
}));

const ok = <T,>(data: T) => ({ success: true as const, data });

const OVERVIEW = {
  total: 12,
  withPanel: 9,
  retrievedTotal: 30,
  retrievedFactors: 4,
  avgPoolScore: 0.5123,
  avgNovelty: 0.42,
  avgMaxCorr: 0.31,
  avgIc: null, // 后端没算过 → 页面必须显「—」
  avgIcir: 0.29,
  avgPfs: 0.88,
  poolDiversity: 0.77,
  nEff: 5.2,
  archivedCount: 0,
  categoryBreakdown: [
    {
      category: 'momentum',
      label: '动量与趋势',
      count: 7,
      share: 7 / 12,
      avgIc: 0.021,
      nIc: 6, // 7 个里只有 6 个有 IC → title 里可见覆盖率
      avgIcir: 0.4,
      nIcir: 6,
      avgPoolScore: 0.55,
      avgNovelty: 0.4,
      topFactors: ['alpha_001'],
    },
    {
      category: 'overnight',
      label: '隔夜与跳空',
      count: 3,
      share: 3 / 12,
      avgIc: null, // 该类全部缺 IC → 卡片显「—」而不是 0
      nIc: 0,
      avgIcir: null,
      nIcir: 0,
      avgPoolScore: 0.4,
      avgNovelty: 0.3,
      topFactors: [],
    },
    {
      category: 'other',
      label: '其他',
      count: 2,
      share: 2 / 12,
      avgIc: 0.01,
      nIc: 2,
      avgIcir: 0.1,
      nIcir: 2,
      avgPoolScore: 0.2,
      avgNovelty: 0.2,
      topFactors: ['misc_1'],
    },
  ],
};

const GATE_FACTOR = {
  factorId: 'abc123def4567890',
  factorName: 'alpha_001',
  factorFormulation: 'ts_mean(close, 5)',
  ic: 0.0321,
  rankIc: null,
  icir: 0.45,
  pfs: 0.85,
  poolScore: 0.61,
  novelty: 0.5,
  maxPoolCorr: 0.72,
  maxPoolCorrWith: 'alpha_000',
  diversityContrib: null,
  timesRetrieved: 3,
  lastRetrievedAt: '2026-10-01T00:00:00Z',
  hasPanel: true,
  createdAt: '2026-09-20T00:00:00Z',
  updatedAt: '2026-10-01T00:00:00Z',
  archivedAt: null,
  category: 'momentum',
  categoryLabel: '动量与趋势',
  rawCategoryLabel: '动量因子',
  gates: {
    rejected: false,
    gates: [
      {
        key: 'pfs_floor',
        label: 'PFS 下限',
        mode: 'soft',
        status: 'fail',
        message: 'PFS 0.850 低于下限 0.900',
        observed: 0.85,
        threshold: 0.9,
      },
      {
        key: 'rre_floor',
        label: 'RRE 下限',
        mode: 'soft',
        status: 'pass',
        message: 'RRE 0.600 达标',
        observed: 0.6,
        threshold: 0.5,
      },
    ],
  },
};

const NO_GATE_FACTOR = {
  ...GATE_FACTOR,
  factorId: 'zzz9999999999999',
  factorName: 'alpha_002',
  hasPanel: false,
  timesRetrieved: 0, // 从未被检索 → 列表显「—」而不是「0 次」
  gates: null,
  category: 'overnight',
  categoryLabel: '隔夜与跳空',
  rawCategoryLabel: null, // 无描述标签 → 名字兜底；徽章 title 提示按名归类
};

const FACTORS_PAGE = {
  total: 2,
  items: [GATE_FACTOR, NO_GATE_FACTOR],
  limit: 20,
  offset: 0,
};

const GATE_DESCRIPTORS = [
  {
    key: 'pfs_floor',
    label: 'PFS 下限',
    default_mode: 'soft',
    description: 'PFS 低于阈值时告警（默认软）',
    default_threshold: 0.9,
  },
  {
    key: 'corr_dedup',
    label: '去重相关',
    default_mode: 'hard',
    description: '与已有因子 |ρ|≥0.9 直接拒绝',
    default_threshold: 0.9,
  },
];

beforeEach(() => {
  Object.values({
    getPoolOverviewMock,
    getPoolFactorsMock,
    getPoolGraphMock,
    getPoolRefreshStatusMock,
    refreshPoolMock,
    getGateDescriptorsMock,
    getUniversesMock,
    listMarketsMock,
  }).forEach((m) => m.mockReset());

  getPoolOverviewMock.mockResolvedValue(ok(OVERVIEW));
  getPoolFactorsMock.mockResolvedValue(ok(FACTORS_PAGE));
  getPoolGraphMock.mockResolvedValue(
    ok({
      nodes: [
        {
          factorId: 'abc123def4567890',
          factorName: 'alpha_001',
          poolScore: 0.61,
          novelty: 0.5,
          timesRetrieved: 3,
          hasPanel: true,
          taskId: 't1',
          icir: 0.45,
        },
      ],
      edges: [],
    }),
  );
  getPoolRefreshStatusMock.mockResolvedValue(
    ok({ status: 'other_user', running: false, log: { exists: false, lines: [] } }),
  );
  refreshPoolMock.mockResolvedValue(ok({ started: true }));
  getGateDescriptorsMock.mockResolvedValue(ok({ gates: GATE_DESCRIPTORS }));
  getUniversesMock.mockResolvedValue(
    ok({ universes: [{ id: 'csi300', name: '沪深300', stockCount: 300 }] }),
  );
  listMarketsMock.mockResolvedValue([
    { market_id: 'a_share', market_name: 'A股', description: '', data_ready: true },
    { market_id: 'us_stock', market_name: '美股', description: '', data_ready: true },
  ]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('FactorPoolPage 池总览', () => {
  test('KPI 来自 overview；avgIc 缺失显示「—」而不是 0', async () => {
    render(<FactorPoolPage />);
    expect(await screen.findByText('12')).toBeTruthy();
    expect(screen.getByText('有面板 9')).toBeTruthy();
    expect(screen.getByText('累计注入 30 次')).toBeTruthy();
    expect(screen.getByText('0.5123')).toBeTruthy();
    expect(screen.getByText('有效因子数 5.2')).toBeTruthy();
    // avgIc=null：页面上应存在「—」，且不存在把 null 伪造成 '0.0000' 的 IC 卡片值
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    expect(screen.queryByText('0.0000')).toBeNull();
    expect(getPoolOverviewMock).toHaveBeenCalledWith({ market: 'a_share', universe: '' });
  });

  test('切换市场后按新作用域重新拉取总览', async () => {
    render(<FactorPoolPage />);
    await screen.findByText('12');
    fireEvent.change(screen.getByLabelText('市场'), { target: { value: 'us_stock' } });
    await waitFor(() =>
      expect(getPoolOverviewMock).toHaveBeenCalledWith({ market: 'us_stock', universe: '' }),
    );
  });
});

describe('FactorPoolPage 因子分类', () => {
  const lastFactorsCall = () =>
    getPoolFactorsMock.mock.calls.at(-1)?.[0] as Record<string, unknown> | undefined;

  test('分类概览按后端顺序渲染；类内指标缺失显「—」而不是 0', async () => {
    render(<FactorPoolPage />);
    await screen.findByText('因子分类概览');

    const titles = Array.from(
      document.querySelectorAll('button[title^="查看「"]'),
    ).map((el) => el.getAttribute('title'));
    expect(titles).toEqual([
      '查看「动量与趋势」因子',
      '查看「隔夜与跳空」因子',
      '查看「其他」因子',
    ]);

    // 动量卡：7 · 58%（7/12→58%），IC 0.0210
    const momentumCard = screen.getByTitle('查看「动量与趋势」因子');
    expect(momentumCard.textContent).toContain('7 · 58%');
    expect(momentumCard.textContent).toContain('0.0210');

    // 隔夜卡：IC/ICIR 全缺 → 显「—」，代表因子为空也显「—」
    const overnightCard = screen.getByTitle('查看「隔夜与跳空」因子');
    expect(within(overnightCard).getAllByText('—').length).toBeGreaterThan(0);
    expect(overnightCard.textContent).not.toContain('0.0000');
  });

  test('点类别卡下钻：切到池因子页签、按该类过滤、行内出类别徽章', async () => {
    render(<FactorPoolPage />);
    await screen.findByText('因子分类概览');

    fireEvent.click(screen.getByTitle('查看「动量与趋势」因子'));
    await waitFor(() =>
      expect(lastFactorsCall()).toMatchObject({ category: 'momentum' }),
    );

    // 页签标题带出当前类别
    expect(await screen.findByText(/类别「动量与趋势」/)).toBeTruthy();
    // 行内徽章：title 显示原始描述标签；名字兜底行提示按名归类
    expect((await screen.findByTitle('原始标签：动量因子')).textContent).toBe('动量与趋势');
    expect(
      screen.getByTitle('按因子名归类（无描述标签）').textContent,
    ).toBe('隔夜与跳空');
  });

  test('chip 过滤/清除；切换市场自动清除类别过滤', async () => {
    render(<FactorPoolPage />);
    await screen.findByText('12');
    fireEvent.click(screen.getByRole('button', { name: /池因子/ }));

    // 点「隔夜与跳空」chip → 带 category=overnight 拉取
    fireEvent.click(await screen.findByRole('button', { name: /隔夜与跳空/ }));
    await waitFor(() =>
      expect(lastFactorsCall()).toMatchObject({ category: 'overnight' }),
    );

    // 点「全部」→ 清除过滤（category=undefined 不成行）
    fireEvent.click(screen.getByRole('button', { name: '全部' }));
    await waitFor(() => expect(lastFactorsCall()?.category).toBeUndefined());

    // 再选一类后切市场：旧类别随作用域清掉
    fireEvent.click(screen.getByRole('button', { name: /隔夜与跳空/ }));
    await waitFor(() =>
      expect(lastFactorsCall()).toMatchObject({ category: 'overnight' }),
    );
    fireEvent.change(screen.getByLabelText('市场'), { target: { value: 'us_stock' } });
    await waitFor(() => {
      const call = lastFactorsCall();
      expect(call?.market).toBe('us_stock');
      expect(call?.category).toBeUndefined();
    });
  });
});

describe('FactorPoolPage 池因子表', () => {
  test('门禁徽标逐条渲染；无裁决行标「未物化」；软告警带观察值', async () => {
    render(<FactorPoolPage />);
    fireEvent.click(await screen.findByRole('button', { name: /池因子/ }));
    expect(await screen.findByText('alpha_001')).toBeTruthy();
    expect(screen.getByText(/PFS 下限/)).toBeTruthy();
    expect(screen.getByText(/RRE 下限/)).toBeTruthy();
    // 软告警 fail 的 chip title 指出 mode=软告警
    const pfsChip = screen.getByText(/PFS 下限/).closest('span');
    expect(pfsChip?.getAttribute('title')).toContain('软告警');
    expect(screen.getByText('未物化')).toBeTruthy();
    expect(screen.getByText('3 次')).toBeTruthy();
  });
});

describe('FactorPoolPage 刷新两键', () => {
  test('预演刷新发 dryRun=true，不弹确认', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm');
    render(<FactorPoolPage />);
    await screen.findByText('12');
    fireEvent.click(screen.getByRole('button', { name: /预演刷新/ }));
    await waitFor(() =>
      expect(refreshPoolMock).toHaveBeenCalledWith({
        market: 'a_share',
        universe: '',
        dryRun: true,
      }),
    );
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(await screen.findByText('已启动预演刷新（不改库）')).toBeTruthy();
  });

  test('执行刷新：confirm 拒绝则不请求；确认后发 dryRun=false', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<FactorPoolPage />);
    await screen.findByText('12');
    fireEvent.click(screen.getByRole('button', { name: /执行刷新/ }));
    expect(refreshPoolMock).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: /执行刷新/ }));
    await waitFor(() =>
      expect(refreshPoolMock).toHaveBeenCalledWith({
        market: 'a_share',
        universe: '',
        dryRun: false,
      }),
    );
    expect(await screen.findByText('已启动刷新，完成后自动生效')).toBeTruthy();
  });

  test('刷新进行中两键都置灰（409 前置防线）', async () => {
    getPoolRefreshStatusMock.mockResolvedValue(
      ok({ status: 'running', running: true, args: { dry_run: true } }),
    );
    render(<FactorPoolPage />);
    expect(await screen.findByText('预演刷新进行中…')).toBeTruthy();
    expect(screen.getByRole('button', { name: /预演刷新/ }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: /执行刷新/ }).hasAttribute('disabled')).toBe(true);
  });
});

describe('FactorPoolPage 刷新状态遮蔽', () => {
  test('他人任务：只显示占位句，不泄露 args/summary 与日志', async () => {
    render(<FactorPoolPage />);
    expect(await screen.findByText('最近一次刷新由其他用户执行')).toBeTruthy();
    expect(screen.queryByText(/查看刷新日志/)).toBeNull();
  });

  test('本人 done：显示完成时间与 summary 计数、日志可展开', async () => {
    getPoolRefreshStatusMock.mockResolvedValue(
      ok({
        status: 'done',
        running: false,
        finished_at: '2026-10-07T12:00:00Z',
        summary: { added: 3, updated: 2 },
        log: { exists: true, lines: ['[pool] start', '[pool] done'], truncated: false },
      }),
    );
    render(<FactorPoolPage />);
    expect(await screen.findByText(/最近刷新完成/)).toBeTruthy();
    expect(screen.getByText(/added=3/)).toBeTruthy();
    expect(screen.getByText(/查看刷新日志/)).toBeTruthy();
    expect(screen.getByText(/\[pool\] done/)).toBeTruthy();
  });
});

describe('FactorPoolPage 谱系图', () => {
  test('进入 tab 才拉图；点击节点出详情卡', async () => {
    render(<FactorPoolPage />);
    await screen.findByText('12');
    expect(getPoolGraphMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /谱系图/ }));
    expect(await screen.findByTestId('pool-graph')).toBeTruthy();
    expect(getPoolGraphMock).toHaveBeenCalledWith({
      market: 'a_share',
      universe: '',
      maxNodes: 200,
    });
    expect(screen.getByTestId('pool-graph-node-count').textContent).toBe('1');

    fireEvent.click(screen.getByRole('button', { name: 'alpha_001' }));
    expect(await screen.findByText('0.6100')).toBeTruthy(); // 详情卡池评分
    expect(screen.getByText('3 次')).toBeTruthy();
  });
});

describe('FactorPoolPage 门禁状态', () => {
  test('规则卡按 default_mode 出软/硬徽标；最近裁决汇总四桶', async () => {
    render(<FactorPoolPage />);
    await screen.findByText('12');
    fireEvent.click(screen.getByRole('button', { name: /门禁状态/ }));
    expect(await screen.findByText('门禁规则（物化准入）')).toBeTruthy();
    expect(screen.getByText('PFS 低于阈值时告警（默认软）')).toBeTruthy();
    expect(screen.getByText('去重相关')).toBeTruthy();
    expect(screen.getByText('硬门禁')).toBeTruthy();

    // 最近裁决：alpha_001 有 soft fail → 软告警桶；alpha_002 无裁决 → 未物化桶
    await waitFor(() => expect(screen.getByText('软告警（记录不拦）')).toBeTruthy());
    expect(screen.getByText('全部通过')).toBeTruthy();
    expect(screen.getByText('未物化（无裁决）')).toBeTruthy();
    expect(getPoolFactorsMock).toHaveBeenCalledWith({
      market: 'a_share',
      universe: '',
      limit: 200,
      offset: 0,
      sort: 'updated_at',
    });
  });
});

describe('FactorPoolPage 组合实验室', () => {
  test('切到组合 tab 才挂载，作用域随页头透传', async () => {
    render(<FactorPoolPage />);
    await screen.findByText('12');
    expect(screen.queryByTestId('combo-lab')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /组合实验室/ }));
    expect(await screen.findByTestId('combo-lab')).toBeTruthy();
    expect(screen.getByTestId('combo-lab').textContent).toBe('a_share|');

    fireEvent.change(screen.getByLabelText('股票池'), { target: { value: 'csi300' } });
    await waitFor(() =>
      expect(screen.getByTestId('combo-lab').textContent).toBe('a_share|csi300'),
    );
  });
});
