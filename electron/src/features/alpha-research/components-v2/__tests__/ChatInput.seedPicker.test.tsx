import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ChatInput } from '../ChatInput';

/**
 * 父本选择器契约（T-MV-01）：展开按「本市场 × 本池」拉 pool_score 前 50；
 * 选择 ≤3 且保序；切市场/池清空；拆解载荷带 seeds（未选不携带键）。
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

const FOUR_ROWS = [
  row(),
  row({ factorId: 'f-2', factorName: 'vol_ratio_10', icir: null, ic: 0.02 }),
  row({ factorId: 'f-3', factorName: 'turn_1', icir: 1.1 }),
  row({ factorId: 'f-4', factorName: 'amt_log', icir: null, ic: null }),
];

const renderInput = (onDecompose = vi.fn(), prompt = '') =>
  render(
    <ChatInput inline onSubmit={vi.fn()} onDecomposeRequest={onDecompose} initialPrompt={prompt} />,
  );

const openSeedPanel = () => fireEvent.click(screen.getByTitle(/父本定向演化/));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listMarkets.mockResolvedValue([]);
  mocks.getUniverses.mockResolvedValue({ data: { universes: [] } });
  mocks.getPoolFactors.mockResolvedValue({
    success: true,
    data: { items: FOUR_ROWS },
  });
});

describe('ChatInput 父本选择器', () => {
  it('展开面板按本市场×本池拉 pool_score 前 50，选项显示名称与 ICIR', async () => {
    renderInput();
    openSeedPanel();

    await waitFor(() =>
      expect(mocks.getPoolFactors).toHaveBeenCalledWith({
        market: 'a_share',
        universe: 'csi300',
        limit: 50,
        sort: 'pool_score',
      }),
    );
    expect(await screen.findByText('mom_20d')).toBeTruthy();
    expect(screen.getByText('ICIR 0.62')).toBeTruthy();
    // ICIR 缺失回退 IC；两者都缺显示 —（绝不显示成 0）
    expect(screen.getByText('IC 0.020')).toBeTruthy();
    expect(screen.getByText('—')).toBeTruthy();
  });

  it('选中父本保点击顺序；拆解载荷携带 seeds 与方向/市场/池', async () => {
    const onDecompose = vi.fn();
    renderInput(onDecompose, '围绕动量做变异');
    openSeedPanel();

    fireEvent.click(await screen.findByText('vol_ratio_10'));
    fireEvent.click(screen.getByText('mom_20d'));
    expect(screen.getByText('2/3')).toBeTruthy();

    fireEvent.click(screen.getByTitle('智能拆解：把当前方向拆成多张正交卡片后批量派发'));
    expect(onDecompose).toHaveBeenCalledWith({
      direction: '围绕动量做变异',
      market: 'a_share',
      universe: 'csi300',
      seeds: [
        { id: 'f-2', name: 'vol_ratio_10' },
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

  it('选满 3 个后其余选项禁用；再点已选项可取消', async () => {
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
    // 新市场重新拉取（scope 跟随切换）
    expect(mocks.getPoolFactors).toHaveBeenCalledWith(
      expect.objectContaining({ market: 'crypto' }),
    );
  });

  it('拉取失败显示错误文案；空池显示引导文案', async () => {
    mocks.getPoolFactors.mockRejectedValueOnce(new Error('后端不可达'));
    renderInput();
    openSeedPanel();
    expect(await screen.findByText('后端不可达')).toBeTruthy();

    mocks.getPoolFactors.mockResolvedValueOnce({ success: true, data: { items: [] } });
    // 重新收起再展开触发重拉
    fireEvent.click(screen.getByTitle(/父本定向演化/));
    fireEvent.click(screen.getByTitle(/父本定向演化/));
    expect(
      await screen.findByText('本池暂无已完成回测的因子——先挖出因子、回测入池后即可选作父本'),
    ).toBeTruthy();
  });
});
