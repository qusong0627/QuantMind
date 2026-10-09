/**
 * 单因子页「Top 30 股票」截面回退（2026-10-09 rd_mined/gap_mined 空表事故）。
 *
 * 断更因子（源库物化停在 8 月底）原先拿全局末月空截面：只有表头、行业分布
 * 「—」、市值分布全空，日期还写着最新日（假日期）。后端已回退到该因子自身
 * 最近截面，前端必须把「这是回退截面」显式说清楚，而不是让用户以为是最新数据：
 *
 * 1. **stale**：滞后提示上屏、带真实截面日、标题标注「该因子最近可用截面」；
 * 2. **fresh**：不得出现滞后提示（误伤即噪音，用户会学会无视它）;
 * 3. **整段无数据**：空表配说明行 + 标题「暂无截面数据」，不是一张无名空表。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SingleFactorTab } from '../SingleFactorTab';
import type { FactorDetail } from '../../types/factorResearch';

const { getFactorDetailMock } = vi.hoisted(() => ({
  getFactorDetailMock: vi.fn(),
}));

vi.mock('../../services/factorResearchService', () => ({
  getFactorDetail: getFactorDetailMock,
}));

// jsdom 无 canvas：图表面不影响本用例要守的行为线，整体替换成占位。
vi.mock('../../../../components/common/EChartsChart', () => ({
  EChartsChart: () => <div data-testid="chart" />,
}));

const RANGE = { start: '2026-01-01', end: '2026-10-08' };

function mkDetail(over: Partial<FactorDetail> = {}): FactorDetail {
  return {
    code: 'rd_mined_x1',
    name_cn: '挖掘因子X1',
    display_name: '',
    l1: 'rd_mined',
    l2: 'rd_mined',
    direction: 1,
    description: '',
    formula: '',
    wind_source: '',
    available: true,
    env_tag: '',
    time_tag: '',
    range: { start: '2026-01-01', end: '2026-10-08', n_months: 10 },
    variants: [],
    benchmarks: [],
    nscan: [],
    ic: [],
    ic_kpi: {},
    stocks: [
      {
        rank: 1,
        symbol: '600036.SH',
        name: '招商银行',
        industry: '银行',
        total_mv_yi: 9000,
        pe_ttm: 6.1,
        pb: 1.0,
        avg_amount_yi: 20.5,
        score: 3.1,
        raw: 0.42,
      },
    ],
    stocks_date: '2026-09-16',
    stocks_stale: true,
    industry_dist: [{ name: '银行', count: 1 }],
    cap_dist: [{ name: '>3000亿', count: 1 }],
    ...over,
  };
}

function renderTab() {
  return render(<SingleFactorTab code="rd_mined_x1" range={RANGE} dataset="private" />);
}

beforeEach(() => {
  getFactorDetailMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('SingleFactorTab：断更因子截面回退', () => {
  test('stale：滞后提示带真实截面日，标题标注最近可用截面', async () => {
    getFactorDetailMock.mockResolvedValue(mkDetail());
    renderTab();

    const notice = await screen.findByText(/未更新到最新截面/);
    expect(notice.textContent).toContain('2026-09-16');
    // 标题带日期前缀 —— 只匹配标题，不误中页脚同措辞
    expect(
      screen.getByText(/Top 30 股票（截面日 2026-09-16，该因子最近可用截面/),
    ).toBeTruthy();
    expect(screen.getByText('600036.SH')).toBeTruthy();
  });

  test('fresh：不出现滞后提示', async () => {
    getFactorDetailMock.mockResolvedValue(
      mkDetail({ stocks_date: '2026-10-08', stocks_stale: false }),
    );
    renderTab();

    await screen.findByText('600036.SH');
    expect(screen.queryByText(/未更新到最新截面/)).toBeNull();
  });

  test('整段无数据：空表配说明行，标题显「暂无截面数据」', async () => {
    getFactorDetailMock.mockResolvedValue(
      mkDetail({
        stocks: [],
        stocks_date: null,
        stocks_stale: false,
        industry_dist: [],
        cap_dist: [],
      }),
    );
    renderTab();

    expect(await screen.findByText(/没有可用的截面数据/)).toBeTruthy();
    expect(screen.getByText(/暂无截面数据/)).toBeTruthy();
  });
});
