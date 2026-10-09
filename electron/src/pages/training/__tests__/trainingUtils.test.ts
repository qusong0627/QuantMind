/**
 * `resolveEvalIcHeadline` —— 训练结果页磁贴的主展示口径（测试段优先、口径如实标注）。
 *
 * 为什么值得钉住：headline（`rank_ic`）是全窗合计、含训练段，约虚高 1.5×（49 份
 * CN 报告实测 test 中位 0.085 vs 全窗 0.127）。磁贴切到测试段后，最危险的失败形态
 * 是「by_split 缺失/字段缺失时静默拿全窗数值冒充样本外」——数值仍然好看、标签不变，
 * 人眼在页面上分不出来。这组用例把「回落必须换标签」钉成不变量。
 */

import { describe, expect, it } from 'vitest';
import dayjs from 'dayjs';

import {
  buildAutoDisplayName,
  buildBackendTrainingPayload,
  buildTrainingRequest,
  DEFAULT_CONTEXT,
  DEFAULT_PARAMS,
  DEFAULT_TARGET,
  DEFAULT_TIME_PERIODS,
  resolveEvalIcHeadline,
  type EvalReport,
} from '../trainingUtils';

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

/**
 * 集合训练（多模型 Stacking）的提交载荷与自动命名 —— 2026-09-05 单选化把 UI 入口
 * 摘掉后，payload 组装链一直健在但无人走到；此次恢复入口，用测试钉住：
 * 多选必须发出 model_types + ensemble + n_folds/meta_alpha，单选绝不能带 ensemble
 * ——否则后端会把一次普通单模型训练当成多模型集成路径（train.py 按 types 长度分派）。
 */

describe('集合训练提交载荷 buildBackendTrainingPayload', () => {
  const buildPayload = (params: Partial<typeof DEFAULT_PARAMS>) => {
    const request = buildTrainingRequest(
      ['VOLUME48', 'MOM_5'],
      [{ id: 'momentum', name: '动量', icon: null, features: [
        { key: 'VOLUME48', label: 'VOLUME48' },
        { key: 'MOM_5', label: 'MOM_5' },
      ] }],
      DEFAULT_TIME_PERIODS,
      DEFAULT_TARGET,
      { ...DEFAULT_PARAMS, ...params },
      DEFAULT_CONTEXT,
      '集成载荷',
      'CN',
    );
    return buildBackendTrainingPayload(request, DEFAULT_TIME_PERIODS) as Record<string, unknown>;
  };

  it('多选 + Stacking：发出 model_types / ensemble / n_folds / meta_alpha', () => {
    const payload = buildPayload({
      model_type: 'lightgbm',
      model_types: ['lightgbm', 'xgboost'],
      ensemble_method: 'stacking',
      n_folds: 5,
      meta_alpha: 2.5,
    });

    expect(payload.model_types).toEqual(['lightgbm', 'xgboost']);
    expect(payload.ensemble).toBe('stacking');
    expect(payload.n_folds).toBe(5);
    expect(payload.meta_alpha).toBe(2.5);
  });

  it('多选 + 无集成：ensemble 仍显式发 none（各自独立训练，仍走多模型路径）', () => {
    const payload = buildPayload({
      model_type: 'lightgbm',
      model_types: ['lightgbm', 'catboost'],
      ensemble_method: 'none',
    });

    expect(payload.model_types).toEqual(['lightgbm', 'catboost']);
    expect(payload.ensemble).toBe('none');
  });

  it('单选：绝不发 model_types / ensemble，防被后端误判为集成任务', () => {
    const payload = buildPayload({
      model_type: 'lightgbm',
      model_types: ['lightgbm'],
      ensemble_method: 'stacking', // 即便残留 stacking，单选也不得透传
    });

    expect(payload.model_types).toBeUndefined();
    expect(payload.ensemble).toBeUndefined();
    expect(payload.n_folds).toBeUndefined();
    expect(payload.meta_alpha).toBeUndefined();
  });
});

describe('集合训练自动命名 buildAutoDisplayName', () => {
  it('多模型短码用 + 连接：LGB+XGB_…', () => {
    const name = buildAutoDisplayName(dayjs('2026-10-09'), { mode: 'return', horizonDays: 5 }, 48, undefined, 'CN', ['lightgbm', 'xgboost']);

    expect(name).toBe('LGB+XGB_09_T5_Alpha48_Base_CN');
  });

  it('单模型（字符串入参）行为不变', () => {
    const name = buildAutoDisplayName(dayjs('2026-10-09'), { mode: 'return', horizonDays: 5 }, 48, undefined, 'CN', 'lightgbm');

    expect(name).toBe('LGB_09_T5_Alpha48_Base_CN');
  });
});
