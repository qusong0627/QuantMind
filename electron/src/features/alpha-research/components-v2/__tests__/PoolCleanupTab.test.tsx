/**
 * 清理建议 tab —— 五条不能坏的行为线：
 *
 * 1. **判据必须带数字证据上屏**（后端 detail 原样展示）；空池显「不需要清理」
 *    而不是空白表格。
 * 2. **归档是用户的显式决定**：不勾选不能归档（按钮禁用）；点了归档必须过
 *    confirm；confirm 取消 → 一个请求都不发。
 * 3. **归档闭环**：选 1 条 → archivePoolFactors([id]) → 结果回执 + 列表重拉。
 * 4. **恢复闭环**：已归档行显「恢复」→ unarchivePoolFactors([id])；
 *    归档列表查询必须带 include_archived=true（否则恢复入口永远空）。
 * 5. **失败如实上屏**：建议接口失败显错误，不静默空态。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { PoolCleanupTab } from '../PoolCleanupTab';
import type {
  PoolCleanupReport,
  PoolFactorRow,
} from '../../services-v2/api';

const {
  getPoolCleanupSuggestionsMock,
  getPoolFactorsMock,
  archivePoolFactorsMock,
  unarchivePoolFactorsMock,
} = vi.hoisted(() => ({
  getPoolCleanupSuggestionsMock: vi.fn(),
  getPoolFactorsMock: vi.fn(),
  archivePoolFactorsMock: vi.fn(),
  unarchivePoolFactorsMock: vi.fn(),
}));

vi.mock('../../services-v2/api', () => ({
  getPoolCleanupSuggestions: getPoolCleanupSuggestionsMock,
  getPoolFactors: getPoolFactorsMock,
  archivePoolFactors: archivePoolFactorsMock,
  unarchivePoolFactors: unarchivePoolFactorsMock,
}));

const ok = <T,>(data: T) => ({ success: true as const, data });

function mkArchivedRow(factorId: string, factorName: string): PoolFactorRow {
  return {
    factorId,
    factorName,
    factorFormulation: 'ts_mean(close,5)',
    ic: 0.01,
    rankIc: null,
    icir: 0.02,
    pfs: null,
    poolScore: 0.2,
    novelty: null,
    maxPoolCorr: 0.93,
    maxPoolCorrWith: 'strong9',
    diversityContrib: null,
    timesRetrieved: 1,
    lastRetrievedAt: null,
    hasPanel: false,
    createdAt: null,
    updatedAt: '2026-10-09T03:00:00Z',
    archivedAt: '2026-10-09T03:00:00Z',
    category: null,
    categoryLabel: null,
    rawCategoryLabel: null,
    gates: null,
  };
}

const REPORT: PoolCleanupReport = {
  items: [
    {
      factorId: 'weakdup1',
      factorName: 'mom_rev_5',
      factorFormulation: 'ts_mean(close,5)',
      icir: 0.02,
      poolScore: 0.31,
      maxPoolCorr: 0.93,
      maxPoolCorrWith: 'strong9',
      diversityContrib: null,
      timesRetrieved: 3,
      severity: 'high',
      reasons: [
        {
          code: 'duplicate',
          label: '冗余被支配',
          detail: '与「mom_ref_20」|ρ|=0.93（ICIR 0.020 vs 0.910），信息已被更强副本覆盖',
        },
      ],
    },
    {
      factorId: 'weakicir2',
      factorName: 'vol_ratio_7',
      factorFormulation: 'std(close,7)/std(close,60)',
      icir: 0.05,
      poolScore: 0.44,
      maxPoolCorr: null,
      maxPoolCorrWith: null,
      diversityContrib: null,
      timesRetrieved: 0,
      severity: 'medium',
      reasons: [
        {
          code: 'weak_icir',
          label: '预测力垫底',
          detail: 'ICIR 0.050 ≤ 池内后 20% 分位（0.050，样本 7）',
        },
      ],
    },
  ],
  total: 2,
  poolSize: 7,
  archivedCount: 1,
  summary: { duplicate: 1, weak_icir: 1 },
  criteria: {
    corrDup: 0.9,
    weakIcirQuantile: 0.2,
    minIcirSample: 5,
    weakIcirThreshold: 0.05,
    icirSampleSize: 7,
  },
  sota: { count: 7, bestIc: 0.05, bestIcir: 1.2, bestPfs: 1.8 },
};

const EMPTY_REPORT: PoolCleanupReport = {
  ...REPORT,
  items: [],
  total: 0,
  archivedCount: 0,
  summary: {},
};

beforeEach(() => {
  Object.values({
    getPoolCleanupSuggestionsMock,
    getPoolFactorsMock,
    archivePoolFactorsMock,
    unarchivePoolFactorsMock,
  }).forEach((m) => m.mockReset());

  getPoolCleanupSuggestionsMock.mockResolvedValue(ok(REPORT));
  getPoolFactorsMock.mockResolvedValue(
    ok({
      total: 1,
      items: [mkArchivedRow('old1', 'legacy_alpha')],
      limit: 500,
      offset: 0,
    }),
  );
  archivePoolFactorsMock.mockResolvedValue(
    ok({ archived: 1, archivedIds: ['weakdup1'], skipped: [] }),
  );
  unarchivePoolFactorsMock.mockResolvedValue(
    ok({ restored: 1, restoredIds: ['old1'], skipped: [] }),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderTab() {
  return render(<PoolCleanupTab market="a_share" universe="" />);
}

describe('PoolCleanupTab 建议渲染', () => {
  test('判据带数字证据上屏；严重度徽章 + 汇总 chips 可见', async () => {
    renderTab();
    expect(await screen.findByText(/ICIR 0\.020 vs 0\.910/)).toBeTruthy();
    expect(screen.getByText(/信息已被更强副本覆盖/)).toBeTruthy();
    expect(screen.getByText(/ICIR 0\.050 ≤ 池内后 20% 分位/)).toBeTruthy();
    // 汇总 chips：判据计数据全量而非截断后
    expect(screen.getByText(/冗余被支配 × 1/)).toBeTruthy();
    expect(screen.getByText(/预测力垫底 × 1/)).toBeTruthy();
    // 不勾选：归档按钮禁用（清理是用户的显式决定）
    expect(
      screen.getByRole('button', { name: /归档所选/ }).hasAttribute('disabled'),
    ).toBe(true);
  });

  test('空池显「不需要清理」而不是空白表', async () => {
    getPoolCleanupSuggestionsMock.mockResolvedValue(ok(EMPTY_REPORT));
    getPoolFactorsMock.mockResolvedValue(ok({ total: 0, items: [], limit: 500, offset: 0 }));
    renderTab();
    expect(await screen.findByText(/不需要清理/)).toBeTruthy();
  });

  test('建议接口失败：错误如实上屏', async () => {
    getPoolCleanupSuggestionsMock.mockResolvedValue({
      success: false,
      error: '清理建议获取失败',
    });
    renderTab();
    expect(await screen.findByText('清理建议获取失败')).toBeTruthy();
  });
});

describe('PoolCleanupTab 归档闭环', () => {
  test('勾选 → confirm → archivePoolFactors([id]) → 回执 + 重拉', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderTab();
    await screen.findByText('mom_rev_5');

    fireEvent.click(screen.getByRole('checkbox', { name: /选择 mom_rev_5/ }));
    const btn = screen.getByRole('button', { name: /归档所选（1）/ });
    expect(btn.hasAttribute('disabled')).toBe(false);

    const callsBefore = getPoolCleanupSuggestionsMock.mock.calls.length;
    fireEvent.click(btn);
    await waitFor(() =>
      expect(archivePoolFactorsMock).toHaveBeenCalledWith(['weakdup1']),
    );
    expect(confirmSpy).toHaveBeenCalled();
    expect(await screen.findByText(/已归档 1 个/)).toBeTruthy();
    await waitFor(() =>
      expect(getPoolCleanupSuggestionsMock.mock.calls.length).toBe(callsBefore + 1),
    );
  });

  test('confirm 取消：不发任何归档请求', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderTab();
    await screen.findByText('mom_rev_5');
    fireEvent.click(screen.getByRole('checkbox', { name: /选择 mom_rev_5/ }));
    fireEvent.click(screen.getByRole('button', { name: /归档所选（1）/ }));

    expect(archivePoolFactorsMock).not.toHaveBeenCalled();
  });

  test('skipped 明示（不在池/非本人/已归档）', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    archivePoolFactorsMock.mockResolvedValue(
      ok({ archived: 1, archivedIds: ['weakdup1'], skipped: ['ghost'] }),
    );
    renderTab();
    await screen.findByText('mom_rev_5');
    fireEvent.click(screen.getByRole('checkbox', { name: /选择 mom_rev_5/ }));
    fireEvent.click(screen.getByRole('button', { name: /归档所选（1）/ }));

    expect(await screen.findByText(/跳过 1 个/)).toBeTruthy();
  });
});

describe('PoolCleanupTab 恢复闭环', () => {
  test('归档列表查询带 include_archived；恢复按钮发 unarchive 并重拉', async () => {
    renderTab();
    await screen.findByText('legacy_alpha');
    // 归档行不查 include_archived 永远是空的——恢复入口必须带这个参数
    expect(getPoolFactorsMock).toHaveBeenCalledWith(
      expect.objectContaining({ includeArchived: true, sort: 'updated_at' }),
    );

    const callsBefore = getPoolCleanupSuggestionsMock.mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: /恢复/ }));
    await waitFor(() =>
      expect(unarchivePoolFactorsMock).toHaveBeenCalledWith(['old1']),
    );
    expect(await screen.findByText(/重新参与注入与池视图/)).toBeTruthy();
    await waitFor(() =>
      expect(getPoolCleanupSuggestionsMock.mock.calls.length).toBe(callsBefore + 1),
    );
  });
});
