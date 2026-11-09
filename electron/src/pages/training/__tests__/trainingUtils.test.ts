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
  buildLabelFormula,
  buildTrainingRequest,
  DEFAULT_CONTEXT,
  DEFAULT_PARAMS,
  DEFAULT_TARGET,
  DEFAULT_TIME_PERIODS,
  normalizeTrainingTarget,
  resolveEvalIcHeadline,
  summarizeRunRequest,
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

describe('任务载荷摘要 summarizeRunRequest', () => {
  /**
   * 监控台「请求预览」必须显示**任务提交时的载荷**（跨库任务实测 300 特征），
   * 而不是页内草稿——草稿在切页/新会话后可能为空，曾把运行中任务显示成 0 特征。
   */
  it('跨库载荷按输出列名去重计数：库:列 与裸列名视为同一列', () => {
    const summary = summarizeRunRequest({
      features: ['l2_factors:amt_20', 'amt_20', 'style_idio_vol_20'],
    });

    expect(summary?.featureCount).toBe(2);
    expect(summary?.featureKeys).toEqual(['amt_20', 'style_idio_vol_20']);
  });

  it('T+5 跨库任务载荷：展示字段逐项取自提交内容', () => {
    const summary = summarizeRunRequest({
      job_name: 'T5_Cross300_LGB_20261016',
      display_name: 'T+5 跨库300因子 LightGBM',
      features: ['concept_exposure_top1', 'l2_factors:amt_20'],
      model_type: 'lightgbm',
      ensemble: 'none',
      context: { market: 'CN', benchmark: 'SH000300' },
      factor_selection: { method: '', n_top: 300, ic_threshold: 0.01 },
      train_start: '2022-10-17T00:00:00+08:00',
      test_end: '2026-09-10T00:00:00+08:00',
    });

    expect(summary?.displayName).toBe('T+5 跨库300因子 LightGBM');
    expect(summary?.featureCount).toBe(2);
    expect(summary?.modelTypes).toEqual(['lightgbm']);
    expect(summary?.ensembleMethod).toBe('none');
    expect(summary?.market).toBe('CN');
    expect(summary?.benchmark).toBe('SH000300');
    expect(summary?.poolLabel).toBe('全市场');
    expect(summary?.trainStart).toBe('2022-10-17T00:00:00+08:00');
    expect(summary?.testEnd).toBe('2026-09-10T00:00:00+08:00');
  });

  it('method 为空串 = 训练不做特征筛选；有 method 才视为启用', () => {
    const disabled = summarizeRunRequest({
      features: ['a'],
      factor_selection: { method: '', n_top: 300, ic_threshold: 0.01 },
    });
    const enabled = summarizeRunRequest({
      features: ['a'],
      factor_selection: { method: 'ic_icir', n_top: 80, ic_threshold: 0.01 },
    });

    expect(disabled?.filterEnabled).toBe(false);
    expect(disabled?.filterNTop).toBe(300);
    expect(enabled?.filterEnabled).toBe(true);
    expect(enabled?.filterNTop).toBe(80);
    expect(enabled?.filterIcThreshold).toBe(0.01);
  });

  it('factor_selection 缺失时筛选状态为「未知」（null），不冒充已关闭', () => {
    const summary = summarizeRunRequest({ features: ['a'] });

    expect(summary?.filterEnabled).toBeNull();
    expect(summary?.filterNTop).toBeNull();
  });

  it('多模型载荷优先用 model_types；空载荷返回 null 由调用方回落草稿', () => {
    const multi = summarizeRunRequest({
      features: ['a'],
      model_type: 'lightgbm',
      model_types: ['lightgbm', 'xgboost'],
    });

    expect(multi?.modelTypes).toEqual(['lightgbm', 'xgboost']);
    expect(summarizeRunRequest(null)).toBeNull();
    expect(summarizeRunRequest(undefined)).toBeNull();
  });
});

/**
 * 标签与执行口径（2026-11-10）：后端训练管线新增 execution_price / label_transform /
 * label_top_pct / label_drop_extreme_pct。前端默认严格实盘（T+1 开盘买），并且
 * 旧草稿/旧配置缺字段时必须回落默认值，不能把 undefined 发进载荷。
 */
describe('标签与执行口径', () => {
  it('默认严格实盘：标签从 T+1 开盘起算，到 T+1+N 收盘', () => {
    expect(DEFAULT_TARGET.executionPrice).toBe('open');
    expect(buildLabelFormula({ ...DEFAULT_TARGET, horizonDays: 5 }))
      .toBe('label = close(T+6) / open(T+1) - 1');
    expect(buildLabelFormula({ ...DEFAULT_TARGET, horizonDays: 5, mode: 'classification' }))
      .toBe('label = 1[ close(T+6) / open(T+1) - 1 > 0 ]');
  });

  it('close 口径回退到 T+1 收盘买（旧行为）', () => {
    expect(buildLabelFormula({ ...DEFAULT_TARGET, horizonDays: 10, executionPrice: 'close' }))
      .toBe('label = close(T+11) / close(T+1) - 1');
  });

  it('提交载荷透传四个标签字段', () => {
    const request = buildTrainingRequest(
      ['VOLUME48'],
      [{ id: 'momentum', name: '动量', icon: null, features: [{ key: 'VOLUME48', label: 'VOLUME48' }] }],
      DEFAULT_TIME_PERIODS,
      {
        ...DEFAULT_TARGET,
        labelTransform: 'top_tail',
        labelTopPct: 0.3,
        labelDropExtremePct: 0.05,
      },
      DEFAULT_PARAMS,
      DEFAULT_CONTEXT,
      '标签口径载荷',
      'CN',
    );
    const payload = buildBackendTrainingPayload(request, DEFAULT_TIME_PERIODS) as Record<string, unknown>;

    expect(payload.execution_price).toBe('open');
    expect(payload.label_transform).toBe('top_tail');
    expect(payload.label_top_pct).toBe(0.3);
    expect(payload.label_drop_extreme_pct).toBe(0.05);
  });

  it('旧草稿/旧配置缺字段时补默认值，非法值被钳制', () => {
    expect(normalizeTrainingTarget({ mode: 'return', horizonDays: 3 })).toMatchObject({
      executionPrice: 'open',
      labelTransform: 'rank',
      labelTopPct: 0.2,
      labelDropExtremePct: 0,
    });
    expect(normalizeTrainingTarget(null)).toMatchObject({ horizonDays: 5, executionPrice: 'open' });

    const clamped = normalizeTrainingTarget({
      mode: 'return',
      horizonDays: 99,
      executionPrice: 'bogus' as never,
      labelTransform: 'bogus' as never,
      labelTopPct: 0.9,
      labelDropExtremePct: 0.5,
    });
    expect(clamped.horizonDays).toBe(30);
    expect(clamped.executionPrice).toBe('open');
    expect(clamped.labelTransform).toBe('rank');
    expect(clamped.labelTopPct).toBe(0.5);
    expect(clamped.labelDropExtremePct).toBe(0.2);
  });
});
