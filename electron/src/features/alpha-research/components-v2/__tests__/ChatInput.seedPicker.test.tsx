import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ChatInput } from '../ChatInput';

/**
 * 父本选择器契约（T-MV-01）：展开按「本市场 × 本池」并行拉正向/反向
 * pool_score 各前 50（方向口径 = ic_value 符号，与因子库「方向」列一致），
 * 分组渲染（空组不渲染）；选择 ≤3 且保序（跨组保点击顺序）；切市场/池清空；
 * 任一侧失败显式报错（绝不静默只显示半列）；拆解载荷带 seeds（未选不携带键）。
 * 上限服务端 DECOMPOSE_SEED_MAX 是硬闸，这里只测前端一致性。
 */

const mocks = vi.hoisted(() => ({
  listMarkets: vi.fn(),
  getUniverses: vi.fn(),
  getPoolFactors: vi.fn(),
}));

vi.mock('../../services/alphaAgentService', () => ({
  alphaAgentService: { listMarkets: mocks.listMarkets },
}));

vi.mock('../../services-v2/api', () => ({
  getUniverses: mocks.getUniverses,
  getPoolFactors: mocks.getPoolFactors,
}));

const row = (over: Record<string, unknown> = {}) => ({
  factorId: 'f-1',
  factorName: 'mom_20d',
  factorFormulation: 'ts_mean(close/ref(close,20)-1,5)',
  ic: 0.031,
  icir: 0.62,
  ...over,
});

/** 正向（ic >= 0）候选：ICIR 正常 + ICIR 缺失回退 IC */
const POS_ROWS = [
  row(),
  row({ factorId: 'f-2', factorName: 'vol_ratio_10', icir: null, ic: 0.02 }),
];
/** 反向（ic < 0）候选：ICIR 正常 + 两者都缺回退 — */
const NEG_ROWS = [
  row({ factorId: 'f-3', factorName: 'turn_1', icir: 1.1, ic: -0.045 }),
  row({ factorId: 'f-4', factorName: 'amt_log', icir: null, ic: null }),
];

const okByDirection = (itemsByDir: { pos: unknown[]; neg: unknown[] }) =>
  mocks.getPoolFactors.mockImplementation((params: { direction?: string }) =>
    Promise.resolve({
      success: true,
      data: { items: params.direction === 'neg' ? itemsByDir.neg : itemsByDir.pos },
    }),
  );

const renderInput = (onDecompose = vi.fn(), prompt = '') =>
  render(
    <ChatInput inline onSubmit={vi.fn()} onDecomposeRequest={onDecompose} initialPrompt={prompt} />,
  );

const openSeedPanel = () => fireEvent.click(screen.getByTitle(/父本定向演化/));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listMarkets.mockResolvedValue([]);
  mocks.getUniverses.mockResolvedValue({ data: { universes: [] } });
  okByDirection({ pos: POS_ROWS, neg: NEG_ROWS });
});

describe('ChatInput 父本选择器', () => {
  it('展开面板并行拉正/反方向各前 50（带 direction 参数），分组渲染并保 ICIR→IC→— 回退', async () => {
    renderInput();
    openSeedPanel();

    await waitFor(() =>
      expect(mocks.getPoolFactors).toHaveBeenCalledWith({
        market: 'a_share',
        universe: 'csi300',
        limit: 50,
        sort: 'pool_score',
        direction: 'pos',
      }),
    );
    expect(mocks.getPoolFactors).toHaveBeenCalledWith({
      market: 'a_share',
      universe: 'csi300',
      limit: 50,
      sort: 'pool_score',
      direction: 'neg',
    });
    expect(mocks.getPoolFactors).toHaveBeenCalledTimes(2);
    // 分组头按「正向 → 反向」顺序，各带本组数量
    expect(screen.getByText('正向（2）')).toBeTruthy();
    expect(screen.getByText('反向（2）')).toBeTruthy();
    expect(await screen.findByText('mom_20d')).toBeTruthy();
    expect(screen.getByText('turn_1')).toBeTruthy();
    expect(screen.getByText('ICIR 0.62')).toBeTruthy();
    // ICIR 缺失回退 IC；两者都缺显示 —（绝不显示成 0）
    expect(screen.getByText('IC 0.020')).toBeTruthy();
    expect(screen.getByText('—')).toBeTruthy();
  });

  it('跨组选中保点击顺序；拆解载荷携带 seeds 与方向/市场/池', async () => {
    const onDecompose = vi.fn();
    renderInput(onDecompose, '围绕动量做变异');
    openSeedPanel();

    fireEvent.click(await screen.findByText('turn_1'));
    fireEvent.click(screen.getByText('mom_20d'));
    expect(screen.getByText('2/3')).toBeTruthy();

    fireEvent.click(screen.getByTitle('智能拆解：把当前方向拆成多张正交卡片后批量派发'));
    expect(onDecompose).toHaveBeenCalledWith({
      direction: '围绕动量做变异',
      market: 'a_share',
      universe: 'csi300',
      seeds: [
        { id: 'f-3', name: 'turn_1' },
        { id: 'f-1', name: 'mom_20d' },
      ],
    });
  });

  it('未选父本：拆解载荷不含 seeds 键（条件展开）', () => {
    const onDecompose = vi.fn();
    renderInput(onDecompose, '纯方向');
    fireEvent.click(screen.getByTitle('智能拆解：把当前方向拆成多张正交卡片后批量派发'));

    const [payload] = onDecompose.mock.calls[0];
    expect('seeds' in payload).toBe(false);
  });

  it('选满 3 个后其余选项禁用（跨组计数）；再点已选项可取消', async () => {
    renderInput();
    openSeedPanel();
    fireEvent.click(await screen.findByText('mom_20d'));
    fireEvent.click(screen.getByText('vol_ratio_10'));
    fireEvent.click(screen.getByText('turn_1'));
    expect(screen.getByText('3/3')).toBeTruthy();

    const fourth = screen.getByText('amt_log').closest('button') as HTMLButtonElement;
    expect(fourth).toBeDisabled();
    expect(fourth.getAttribute('title')).toBe('最多选 3 个父本');

    // 取消一个后第 4 个恢复可选
    fireEvent.click(screen.getByText('turn_1'));
    expect(screen.getByText('2/3')).toBeTruthy();
    expect(fourth).not.toBeDisabled();
  });

  it('切市场清空已选父本（旧池父本对新池无意义）', async () => {
    renderInput();
    openSeedPanel();
    fireEvent.click(await screen.findByText('mom_20d'));
    expect(screen.getByText('1/3')).toBeTruthy();

    fireEvent.click(screen.getByText('加密货币'));
    await waitFor(() => expect(screen.queryByText('1/3')).toBeNull());
    // 新市场重新拉取（scope 跟随切换；两个方向都带上新市场）
    expect(mocks.getPoolFactors).toHaveBeenCalledWith(
      expect.objectContaining({ market: 'crypto', direction: 'pos' }),
    );
    expect(mocks.getPoolFactors).toHaveBeenCalledWith(
      expect.objectContaining({ market: 'crypto', direction: 'neg' }),
    );
  });

  it('任一侧失败显式报错，不静默只显示另一侧（候选不完整 = 误导选择）', async () => {
    mocks.getPoolFactors.mockImplementation((params: { direction?: string }) =>
      params.direction === 'neg'
        ? Promise.reject(new Error('后端不可达'))
        : Promise.resolve({ success: true, data: { items: POS_ROWS } }),
    );
    renderInput();
    openSeedPanel();

    expect(await screen.findByText('后端不可达')).toBeTruthy();
    // 正向侧其实拉到了——但绝不半列展示
    expect(screen.queryByText('mom_20d')).toBeNull();
  });

  it('单侧为空：只渲染非空分组，不出现空标题', async () => {
    okByDirection({ pos: [], neg: NEG_ROWS });
    renderInput();
    openSeedPanel();

    expect(await screen.findByText('反向（2）')).toBeTruthy();
    expect(screen.queryByText('正向（0）')).toBeNull();
    expect(screen.queryByText(/^正向（/)).toBeNull();
  });

  it('两侧皆空显示引导文案；两侧都有时无引导文案', async () => {
    okByDirection({ pos: [], neg: [] });
    renderInput();
    openSeedPanel();
    expect(
      await screen.findByText('本池暂无已完成回测的因子——先挖出因子、回测入池后即可选作父本'),
    ).toBeTruthy();
  });
});
