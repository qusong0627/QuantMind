/**
 * 池内供给面（T-MV-02）契约：
 * - 缺什么显「—」：均值/中位 IC 与饱和度缺失绝不渲染成 0；
 * - 饱和度是「÷最满真实类」的相对值，「其他」行恒 —（不是挖掘方向）；
 * - 失败给可重试的错误文案，空池给引导文案（不假装 0 供给）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SupplyFacePanel } from '../SupplyFacePanel';
import type { PoolCategoryStat } from '../../services-v2/api';

const apiMocks = vi.hoisted(() => ({ getPoolOverview: vi.fn() }));

vi.mock('../../services-v2/api', () => ({
  getPoolOverview: apiMocks.getPoolOverview,
}));

const stat = (over: Partial<PoolCategoryStat> = {}): PoolCategoryStat => ({
  category: 'momentum',
  label: '动量与趋势',
  count: 10,
  share: 0.5,
  avgIc: 0.0231,
  medianIc: 0.0184,
  nIc: 8,
  avgIcir: 0.4,
  nIcir: 8,
  avgPoolScore: 0.7,
  avgNovelty: 0.6,
  saturation: 1.0,
  topFactors: [],
  ...over,
});

const ok = (rows: PoolCategoryStat[], total = rows.reduce((s, r) => s + r.count, 0)) => ({
  success: true,
  data: { categoryBreakdown: rows, total },
});

beforeEach(() => {
  apiMocks.getPoolOverview.mockReset();
});

describe('SupplyFacePanel', () => {
  it('按后端顺序渲染大类：计数、均值/中位 IC、饱和度占比', async () => {
    apiMocks.getPoolOverview.mockResolvedValue(
      ok([
        stat(),
        stat({
          category: 'overnight',
          label: '隔夜与跳空',
          count: 5,
          share: 0.25,
          avgIc: 0.01,
          medianIc: null,
          nIc: 5,
          saturation: 0.5,
        }),
        stat({
          category: 'other',
          label: '其他',
          count: 5,
          share: 0.25,
          avgIc: null,
          medianIc: null,
          nIc: 0,
          saturation: null,
        }),
      ]),
    );

    render(<SupplyFacePanel />);

    expect(await screen.findByText('动量与趋势')).toBeTruthy();
    expect(screen.getByText('隔夜与跳空')).toBeTruthy();
    expect(screen.getByText('其他')).toBeTruthy();
    expect(apiMocks.getPoolOverview).toHaveBeenCalledWith({
      market: 'a_share',
      universe: 'csi300',
    });

    // 计数与 IC：均值/中位两位有效渲染；缺失（中位 null / 均值为 null）显「—」
    expect(screen.getByText('10 个')).toBeTruthy();
    expect(screen.getByText(/均值 IC 0\.0231/)).toBeTruthy();
    expect(screen.getByText('中位 0.0184')).toBeTruthy();
    // 覆盖率不足（nIc < count）随行可见：8/10；满覆盖不标
    expect(screen.getByText(/均值 IC 0\.0231（8\/10）/)).toBeTruthy();
    expect(screen.getByText(/均值 IC 0\.0100/)).toBeTruthy();

    // 缺失全部显「—」（other 的均值 IC 无样本；overnight 与 other 的中位缺失）
    expect(screen.getByText(/均值 IC —（0\/5）/)).toBeTruthy();
    expect(screen.getAllByText('中位 —')).toHaveLength(2);

    // 饱和度：50% 行有；「其他」恒 —（不只是数值缺失——语义就不适用）
    expect(screen.getByText('50%')).toBeTruthy();
    expect(screen.getByText('100%')).toBeTruthy();
    expect(screen.getByText('—')).toBeTruthy();
  });

  it('拉取失败给错误文案与重试；重试后恢复', async () => {
    apiMocks.getPoolOverview.mockRejectedValueOnce(new Error('后端不可达'));
    apiMocks.getPoolOverview.mockResolvedValueOnce(ok([stat()]));

    render(<SupplyFacePanel />);

    expect(await screen.findByText(/供给面读取失败：后端不可达/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /重试/ }));
    expect(await screen.findByText('动量与趋势')).toBeTruthy();
    expect(apiMocks.getPoolOverview).toHaveBeenCalledTimes(2);
  });

  it('空池显示引导文案（不渲染 0 供给）', async () => {
    apiMocks.getPoolOverview.mockResolvedValue(ok([], 0));

    render(<SupplyFacePanel />);

    expect(await screen.findByText(/本池暂无已挖因子/)).toBeTruthy();
  });
});
