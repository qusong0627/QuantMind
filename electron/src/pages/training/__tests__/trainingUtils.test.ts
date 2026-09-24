/**
 * `resolveEvalIcHeadline` —— 训练结果页磁贴的主展示口径（测试段优先、口径如实标注）。
 *
 * 为什么值得钉住：headline（`rank_ic`）是全窗合计、含训练段，约虚高 1.5×（49 份
 * CN 报告实测 test 中位 0.085 vs 全窗 0.127）。磁贴切到测试段后，最危险的失败形态
 * 是「by_split 缺失/字段缺失时静默拿全窗数值冒充样本外」——数值仍然好看、标签不变，
 * 人眼在页面上分不出来。这组用例把「回落必须换标签」钉成不变量。
 */

import { describe, expect, it } from 'vitest';

import { resolveEvalIcHeadline, type EvalReport } from '../trainingUtils';

const FULL_WINDOW = { mean: 0.1268, icir: 1.2, win_rate: 0.72, t_stat: 7.0 };

const withTest: EvalReport = {
  rank_ic: FULL_WINDOW,
  by_split: {
    train: { mean: 0.15, icir: 1.4, win_rate: 0.8, t_stat: 10.0 },
    valid: { mean: 0.10, icir: 1.0, win_rate: 0.7, t_stat: 6.0 },
    test: { mean: 0.085, icir: 0.466, win_rate: 0.671, t_stat: 4.0 },
  },
};

describe('resolveEvalIcHeadline', () => {
  it('有测试段时取测试段数值，且不与全窗 headline 混用', () => {
    const head = resolveEvalIcHeadline(withTest);

    expect(head.isTest).toBe(true);
    expect(head.tag).toBe('测试段');
    expect(head.mean).toBe(0.085);
    expect(head.icir).toBe(0.466);
    expect(head.winRate).toBe(0.671);
    // 关键：绝不能停留在全窗数值上（旧版显示口径就是这么虚高的）
    expect(head.mean).not.toBe(FULL_WINDOW.mean);
  });

  it('无 by_split 的老报告：回落全窗，但标签如实标注，不冒充样本外', () => {
    const head = resolveEvalIcHeadline({ rank_ic: FULL_WINDOW });

    expect(head.isTest).toBe(false);
    expect(head.tag).toBe('全窗含训练');
    expect(head.mean).toBe(FULL_WINDOW.mean);
    expect(head.icir).toBe(FULL_WINDOW.icir);
  });

  it('有 test 段但 mean 缺失：同样回落并换标签，不给出半个「测试段」', () => {
    const head = resolveEvalIcHeadline({
      rank_ic: FULL_WINDOW,
      by_split: { test: { mean: null, icir: 1.0, win_rate: 0.6, t_stat: 2.0 } },
    });

    expect(head.isTest).toBe(false);
    expect(head.tag).toBe('全窗含训练');
    expect(head.mean).toBe(FULL_WINDOW.mean);
  });

  it('by_split 只有 train/valid：不拿验证段冒充测试段', () => {
    const head = resolveEvalIcHeadline({
      rank_ic: FULL_WINDOW,
      by_split: {
        train: { mean: 0.2, icir: 2.0, win_rate: 0.9, t_stat: 12.0 },
        valid: { mean: 0.15, icir: 1.5, win_rate: 0.8, t_stat: 8.0 },
      },
    });

    expect(head.isTest).toBe(false);
    expect(head.mean).toBe(FULL_WINDOW.mean);
  });
});
