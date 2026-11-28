/**
 * 组合选股的纯函数层（P5）。
 *
 * 这些函数决定「缺失显示什么、历史怎么拼、CSV 怎么转义、计划何时算过期」，
 * 错了每个页面都错，所以逐条对拍，不靠渲染快照。
 */

import { describe, expect, it } from 'vitest';
import type {
  BlendPlan,
  BlendPlanRow,
  BlendRankingRow,
  BlendRollingColumn,
  BlendRollingExit,
  ComboTemplate,
} from '../../../services/blendTypes';
import {
  ROLLING_TONE,
  blendHorizonLabel,
  blendMethodLabel,
  buildRankingCsv,
  columnBySymbol,
  coverageLabel,
  dispersionLabel,
  exitLabel,
  exitTradeLabel,
  formatAmount,
  formatPrice,
  formatReturnPct,
  historyLabel,
  isPlanStale,
  planDisplayRows,
  positionLabel,
  rankingCsvFilename,
  returnToneClass,
  rollingHeldLabel,
  rollingStatus,
  scoreLabel,
  scrollGeometry,
  templateGradeLabel,
  templateRollingDefaults,
  templateToCreateRequest,
} from '../components/combo/comboRankingModel';

function rankingRow(over: Partial<BlendRankingRow> = {}): BlendRankingRow {
  return {
    code: 'SH600000',
    symbol: 'SH600000',
    name: '浦发银行',
    S: 0.8,
    S_pct: 0.9,
    score: 0.8,
    pct: 0.9,
    rank: 1,
    research_score: 90.0,
    position: 'BUY',
    coverage: 13,
    dispersion: 0.03,
    dispersion_band: 'low',
    contributions: { m1: 0.9 },
    missing_members: [],
    ...over,
  };
}

function planRow(symbol: string, over: Partial<BlendPlanRow> = {}): BlendPlanRow {
  return {
    symbol,
    mean20: 0.5,
    exit_line: 0.45,
    ...over,
  };
}

function plan(over: Partial<BlendPlan> = {}): BlendPlan {
  return {
    plan_id: 'plan-1',
    blend_id: 'blend-1',
    data_trade_date: '2026-10-19',
    target_date: '2026-10-20',
    params: {
      capital: 10_000_000,
      top_k: 50,
      hold_days: 10,
      entry_per_day: 5,
      target_positions: 50,
      cash_buffer: 0.02,
      adv_participation: 0.05,
      exit_delta: 0.05,
      lookback: 20,
      min_lookback_days: 10,
    },
    summary: {
      capital: 10_000_000,
      target_positions: 50,
      entry_per_day: 5,
      hold_days: 10,
      top_k: 50,
      cash_buffer: 0.02,
      n_entries: 5,
      n_holds: 45,
      n_exits: 3,
      n_open_after: 50,
    },
    entries: [planRow('SH600001')],
    holds: [planRow('SH600002')],
    exits: [planRow('SH600003')],
    warnings: [],
    ...over,
  };
}

describe('展示词的单一来源', () => {
  it('分歧档位只翻译已知枚举，未知一律 —', () => {
    expect(dispersionLabel('low')).toBe('低');
    expect(dispersionLabel('MEDIUM')).toBe('中');
    expect(dispersionLabel('high')).toBe('高');
    expect(dispersionLabel('unknown')).toBe('—');
    expect(dispersionLabel(null)).toBe('—');
  });

  it('位置词走 signalVocabulary，不直接暴露 BUY/HOLD/SELL', () => {
    expect(positionLabel({ position: 'BUY' })).toBe('靠前');
    expect(positionLabel({ position: 'HOLD' })).toBe('居中');
    expect(positionLabel({ position: 'SELL' })).toBe('靠后');
    expect(positionLabel({ position: 'STRONG_BUY' })).toBe('靠前');
    expect(positionLabel({ position: 'WHAT' })).toBe('—');
  });

  it('综合分缺失显示 — 而不是 0', () => {
    expect(scoreLabel(rankingRow({ research_score: null }))).toBe('—');
    expect(scoreLabel(rankingRow({ research_score: 52.34 }))).toBe('52.3');
  });

  it('覆盖显示 n/成员数；成员数未知时只显示 n', () => {
    expect(coverageLabel(13, 13)).toBe('13/13');
    expect(coverageLabel(7, 0)).toBe('7');
    expect(coverageLabel(null, 13)).toBe('—');
  });

  it('期限 / 合成方法 / 证据等级只做翻译，不猜', () => {
    expect(blendHorizonLabel(10)).toBe('T+10');
    expect(blendHorizonLabel(null)).toBe('—');
    expect(blendMethodLabel('mean')).toBe('等权均值');
    expect(blendMethodLabel('trimmed')).toBe('截尾均值');
    expect(blendMethodLabel('custom')).toBe('custom');
    expect(templateGradeLabel('watch')).toBe('观察');
    expect(templateGradeLabel('unverified')).toBe('未验证');
    expect(templateGradeLabel('weird')).toBe('待验证');
  });
});

describe('历史徽标', () => {
  it('无历史返回 —，不显示「持有 0 天」', () => {
    expect(historyLabel(null)).toBe('—');
    expect(historyLabel({ held_days: 0, reentry_count: 0, consecutive_days_in_top50: 0, first_entry_date: null, last_entry_date: null })).toBe('—');
  });

  it('按持有/连续在榜/再入选拼词，持有天数带分母', () => {
    expect(
      historyLabel(
        {
          first_entry_date: '2026-10-14',
          last_entry_date: '2026-10-19',
          reentry_count: 2,
          held_days: 4,
          consecutive_days_in_top50: 3,
        },
        10,
      ),
    ).toBe('持有 4/10 日 · 连续在榜 3 日 · 再入选 2 次');
  });

  it('没有目标持有日时不编分母', () => {
    expect(
      historyLabel(
        {
          first_entry_date: '2026-10-14',
          last_entry_date: '2026-10-19',
          reentry_count: 0,
          held_days: 4,
          consecutive_days_in_top50: 0,
        },
        null,
      ),
    ).toBe('持有 4 日');
  });
});

describe('滚动视图的色块与状态', () => {
  it('新进/留存判定只看后端的 is_new', () => {
    expect(rollingStatus({ is_new: true })).toBe('new');
    expect(rollingStatus({ is_new: false })).toBe('held');
    expect(rollingHeldLabel({ is_new: true, held_days: 0 })).toBe('新进');
    expect(rollingHeldLabel({ is_new: false, held_days: 3 })).toBe('持有 3 日');
    expect(rollingHeldLabel({ is_new: false, held_days: 0 })).toBe('—');
  });

  it('色块刻意不用红绿', () => {
    const tones = Object.values(ROLLING_TONE).join(' ');
    expect(tones).not.toMatch(/red|green|rose|emerald/);
  });

  it('同列重复代码保留首个，不求和', () => {
    const column: BlendRollingColumn = {
      date: '2026-10-19',
      picks: [
        { symbol: 'SH600000', name: 'A', score: 0.5, rank: 1, row_rank: 1, coverage: 13, pct: 0.9, position: 'BUY', held_days: 1, is_new: true },
        { symbol: 'SH600000', name: 'A2', score: 0.9, rank: 2, row_rank: 2, coverage: 13, pct: 0.8, position: 'BUY', held_days: 1, is_new: true },
      ],
      exits: ['SZ000001'],
    };
    expect(columnBySymbol(column).get('SH600000')?.name).toBe('A');
    expect(exitLabel(column)).toBe('SZ000001');
    expect(exitLabel(null)).toBe('');
  });
});

describe('滚动退出损益（收盘价买卖口径）', () => {
  it('收益格式带符号、两位小数；缺失 — 而不是 0', () => {
    expect(formatReturnPct(1.234)).toBe('+1.23%');
    expect(formatReturnPct(-0.456)).toBe('-0.46%');
    expect(formatReturnPct(0)).toBe('0.00%');
    expect(formatReturnPct(null)).toBe('—');
    expect(formatReturnPct(undefined)).toBe('—');
    expect(formatReturnPct(Number.NaN)).toBe('—');
  });

  it('A股红涨绿跌：盈红、亏绿、持平中性、缺失灰', () => {
    expect(returnToneClass(1)).toBe('text-red-600');
    expect(returnToneClass(-1)).toBe('text-green-600');
    expect(returnToneClass(0)).toBe('text-slate-500');
    expect(returnToneClass(null)).toBe('text-slate-400');
  });

  it('价格两位小数，缺失 —', () => {
    expect(formatPrice(10.5)).toBe('10.50');
    expect(formatPrice(0)).toBe('—');
    expect(formatPrice(null)).toBe('—');
  });

  it('退出弹窗：入选→退出收盘价、持有天数、窗口起点标注，且不含买卖字眼', () => {
    const exit: BlendRollingExit = {
      symbol: 'SZ000001',
      name: '平安银行',
      entry_date: '2026-10-08',
      exit_date: '2026-10-10',
      entry_price: 20,
      exit_price: 18,
      return_pct: -10,
      held_days: 2,
      entry_from_window_start: true,
    };
    const label = exitTradeLabel(exit);
    expect(label).toBe(
      '入选 2026-10-08 收盘 20.00 → 退出 2026-10-10 收盘 18.00 · 持有 2 日 · 按窗口起点计',
    );
    expect(label).not.toMatch(/买入|卖出/);
    expect(exitTradeLabel({ ...exit, entry_from_window_start: false })).not.toContain('窗口起点');
    expect(exitTradeLabel({ ...exit, entry_price: null })).toContain('收盘 —');
  });
});

describe('横向滚动条几何', () => {
  it('无横向溢出时不渲染轨道（不多占 1px）', () => {
    expect(scrollGeometry({ clientWidth: 1200, scrollWidth: 1200, scrollLeft: 0, trackWidth: 1200 }))
      .toEqual({ visible: false, thumbWidth: 0, thumbLeft: 0, progress: 0 });
    // 1px 误差也算无溢出：浏览器取整常见
    expect(scrollGeometry({ clientWidth: 1200, scrollWidth: 1201, scrollLeft: 0, trackWidth: 1200 }).visible)
      .toBe(false);
  });

  it('滑块宽度按视口/内容比例，位置按进度映射到剩余行程', () => {
    const start = scrollGeometry({
      clientWidth: 1580,
      scrollWidth: 1836,
      scrollLeft: 0,
      trackWidth: 1580,
    });
    expect(start.visible).toBe(true);
    expect(start.thumbWidth).toBeCloseTo((1580 / 1836) * 1580, 4);
    expect(start.thumbLeft).toBe(0);
    expect(start.progress).toBe(0);

    const end = scrollGeometry({
      clientWidth: 1580,
      scrollWidth: 1836,
      scrollLeft: 256,
      trackWidth: 1580,
    });
    expect(end.progress).toBe(1);
    expect(end.thumbLeft).toBeCloseTo(1580 - end.thumbWidth, 4);
  });

  it('进度夹在 0–1：越界滚动值不把滑块甩出轨道', () => {
    const base = { clientWidth: 500, scrollWidth: 900, trackWidth: 500 };
    expect(scrollGeometry({ ...base, scrollLeft: -40 }).progress).toBe(0);
    expect(scrollGeometry({ ...base, scrollLeft: 9999 }).progress).toBe(1);
    const mid = scrollGeometry({ ...base, scrollLeft: 200 });
    expect(mid.progress).toBeCloseTo(0.5, 6);
    expect(mid.thumbLeft).toBeCloseTo((500 - mid.thumbWidth) / 2, 4);
  });

  it('内容极长时滑块有最小宽度，仍可抓取', () => {
    const geom = scrollGeometry({ clientWidth: 100, scrollWidth: 10000, scrollLeft: 0, trackWidth: 100 });
    expect(geom.thumbWidth).toBe(28);
    expect(geom.thumbWidth).toBeLessThanOrEqual(100);
  });

  it('非法/未布局尺寸一律不可见（首帧 jsdom clientWidth=0）', () => {
    expect(scrollGeometry({ clientWidth: 0, scrollWidth: 0, scrollLeft: 0, trackWidth: 0 }).visible).toBe(false);
    expect(scrollGeometry({ clientWidth: Number.NaN, scrollWidth: 900, scrollLeft: 0, trackWidth: 500 }).visible).toBe(false);
    expect(scrollGeometry({ clientWidth: 500, scrollWidth: 900, scrollLeft: Number.NaN, trackWidth: 500 }).visible).toBe(false);
  });
});

describe('计划展示', () => {
  it('固定按新进 → 留存 → 退出摊平，类别由列表决定', () => {
    const rows = planDisplayRows(plan());
    expect(rows.map((row) => row.category)).toEqual(['新进', '留存', '退出']);
    expect(planDisplayRows(null)).toEqual([]);
  });

  it('过期判定：只允许最近 2 个交易日，缺失/空列表按过期处理', () => {
    const dates = ['2026-10-19', '2026-10-18', '2026-10-17'];
    expect(isPlanStale('2026-10-19', dates)).toBe(false);
    expect(isPlanStale('2026-10-18', dates)).toBe(false);
    expect(isPlanStale('2026-10-17', dates)).toBe(true);
    expect(isPlanStale('2026-10-20', dates)).toBe(true);
    expect(isPlanStale(null, dates)).toBe(true);
    expect(isPlanStale('2026-10-19', [])).toBe(true);
  });

  it('金额缩写保留符号，缺失为 —', () => {
    expect(formatAmount(120_000_000)).toBe('1.2 亿');
    expect(formatAmount(12_300)).toBe('1.2 万');
    expect(formatAmount(-500)).toBe('-500');
    expect(formatAmount(null)).toBe('—');
  });
});

describe('榜单 CSV', () => {
  it('带 BOM、固定表头、缺失留空而不是 0', () => {
    const csv = buildRankingCsv(
      [
        rankingRow(),
        rankingRow({ rank: 2, code: 'SH600002', name: '名称,带逗号"引号', research_score: null, industry: null }),
      ],
      13,
    );
    expect(csv.startsWith('﻿')).toBe(true);
    const lines = csv.slice(1).split('\n');
    expect(lines[0]).toBe('排名,代码,名称,综合分,位置,覆盖,分歧,行业,市值(亿)');
    expect(lines[1]).toBe('1,SH600000,浦发银行,90.0,靠前,13/13,低,,');
    expect(lines[2]).toBe('2,SH600002,"名称,带逗号""引号",,靠前,13/13,低,,'); // 综合分留空，不是 0
  });

  it('文件名去掉路径非法字符', () => {
    expect(rankingCsvFilename('13 模型/T+10', '2026-10-19')).toBe(
      '组合排名_13 模型_T+10_2026-10-19.csv',
    );
    expect(rankingCsvFilename('   ', '2026-10-19')).toBe('组合排名_combo_2026-10-19.csv');
  });
});

describe('模板 → 保存请求', () => {
  const template: ComboTemplate = {
    template_id: 'sys:abc:10:baseline',
    kind: 'baseline',
    name: '全13·T+10 等权',
    market: 'CN',
    horizon_days: 10,
    method: 'mean',
    trim_count: 2,
    min_coverage: 2,
    consensus_min_count: 0,
    members: [
      { model_id: 'm1', model_source: 'manual', weight: 1, model_type: 'lightgbm', display_name: '一' },
      { model_id: 'm2', model_source: 'manual', weight: 1, model_type: 'xgboost', display_name: '二' },
    ],
    evidence: { grade: 'watch', candidate_id: null },
    baseline: true,
  };

  it('等权 → weights=null，保留 template_id 与 system 来源', () => {
    const body = templateToCreateRequest(template);
    expect(body.weights).toBeNull();
    expect(body.model_ids).toEqual(['m1', 'm2']);
    expect(body.method).toBe('mean');
    expect(body.source).toBe('system');
    expect(body.template_id).toBe('sys:abc:10:baseline');
    expect(body.name).toBe('全13·T+10 等权');
    // T+10 → 5 只/日 × 10 日 = 50
    expect(body.purpose).toBe('rebalance');
    expect(body.entry_per_day).toBe(5);
    expect(body.rolling_window).toBe(10);
    expect(body.target_positions).toBe(50);
  });

  it('滚动默认值随周期派生：T+5 → 25，T+20 → 100（上限 100）', () => {
    expect(templateRollingDefaults(5)).toMatchObject({
      entry_per_day: 5,
      rolling_window: 5,
      target_positions: 25,
    });
    expect(templateRollingDefaults(20)).toMatchObject({
      entry_per_day: 5,
      rolling_window: 20,
      target_positions: 100,
    });
    expect(templateRollingDefaults(null)).toMatchObject({
      rolling_window: 10,
      target_positions: 50,
    });
  });

  it('模板显式给出的滚动参数优先于派生值', () => {
    const explicit = { ...template, horizon_days: 5, entry_per_day: 3, rolling_window: 8, target_positions: 24 };
    const body = templateToCreateRequest(explicit);
    expect(body.entry_per_day).toBe(3);
    expect(body.rolling_window).toBe(8);
    expect(body.target_positions).toBe(24);
  });

  it('模板值超服务端上限时夹到 le 上限（entry 50 / window 60 / target 500）', () => {
    const body = templateToCreateRequest({
      ...template,
      horizon_days: 90,
      entry_per_day: 80,
      rolling_window: 90,
      target_positions: 900,
    });
    expect(body.entry_per_day).toBe(50);
    expect(body.rolling_window).toBe(60);
    expect(body.target_positions).toBe(500);
  });

  it('非等权 → 原样映射权重；名称可覆盖', () => {
    const weighted = {
      ...template,
      members: template.members.map((m, i) => ({ ...m, weight: i === 0 ? 2 : 1 })),
    };
    const body = templateToCreateRequest(weighted, ' 我的组合 ');
    expect(body.weights).toEqual({ m1: 2, m2: 1 });
    expect(body.name).toBe('我的组合');
  });
});
