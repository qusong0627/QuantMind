/**
 * 推送确认面板纯逻辑测试。
 *
 * 盯的是四处「显示错了也不报错」的地方：
 * 1. 阻断的行必须说得出为什么（点不动按钮但不说原因 = 死按钮）；
 * 2. 镜像跳过要显示成「不会下发真单」，不能混进成功；
 * 3. 改量非法必须把确认按住，合法改量要让金额与汇总跟着变；
 * 4. 回执里 skipped/fail 不得渲染成成功（这条链最后会动真钱）。
 */

import { describe, expect, it } from 'vitest';
import {
  MAX_QUANTITY,
  blockedReason,
  blockedTag,
  budgetBanner,
  channelLabel,
  effectiveQuantity,
  executeHeadline,
  isRealDirect,
  legAmount,
  legResultView,
  mirrorPlanIssues,
  mirrorPrecheckText,
  mirrorReasonText,
  mirrorReceiptView,
  panelSummary,
  parseQuantity,
  pushGate,
  quotaLine,
  realDirectText,
  riskVerdictView,
} from '../pushModel';
import type { PushExecute, PushLeg, PushLegResult, PushMirrorPlan } from '../../stock-terminal-shared/types';

const leg = (over: Partial<PushLeg> = {}): PushLeg => ({
  symbol: '600036.SH',
  name: '招商银行',
  price: 40,
  position_score: 0.5,
  signal_date: '2026-09-18',
  available_position: 0,
  amount: 20000,
  risk: null,
  quantity: 500,
  source: 'auto',
  note: '',
  problem: '',
  blocked_by: '',
  executable: true,
  mirror_precheck: { will_skip: false, reason: '' },
  ...over,
});

describe('blockedReason / blockedTag', () => {
  it('可执行的行没有阻断标签', () => {
    // Act / Assert
    expect(blockedTag(leg())).toBe('');
    expect(blockedReason(leg())).toBe('');
  });

  it('服务端给了 reason 就用它（它带理由正文）', () => {
    // Arrange
    const row = leg({ executable: false, blocked_by: 'list', problem: '在排除名单内（基本面长期排除名单）' });

    // Act / Assert
    expect(blockedTag(row)).toBe('名单');
    expect(blockedReason(row)).toBe('在排除名单内（基本面长期排除名单）');
  });

  it('服务端没给原因也不留空（空白看起来像界面坏了）', () => {
    // Act / Assert
    expect(blockedReason(leg({ executable: false, blocked_by: 'risk' }))).toContain('风控');
  });

  it('实盘配额跳过的腿标「实盘配额」而不是通用「阻断」', () => {
    // Arrange：_mirror_plan 改 executable 时不写 blocked_by，只补 problem
    const row = leg({
      executable: false,
      blocked_by: '',
      problem: '实盘配额不足（max_daily_symbols），该笔不会下发真单',
      mirror_precheck: { will_skip: true, reason: 'max_daily_symbols' },
    });

    // Act / Assert
    expect(blockedTag(row)).toBe('实盘配额');
  });
});

describe('mirrorPrecheckText', () => {
  it('不会跳过 → 将下发真单', () => {
    // Act / Assert
    expect(mirrorPrecheckText(leg())).toBe('将下发真单');
  });

  it('会跳过 → 说成「不会下发真单」并给出卡在哪一条', () => {
    // Act
    const text = mirrorPrecheckText(leg({ mirror_precheck: { will_skip: true, reason: 'max_daily_value' } }));

    // Assert：只说「配额不足」用户仍会以为模拟盘与真单都成了
    expect(text).toContain('不会下发真单');
    expect(text).toContain('日委托金额上限');
  });

  it('未知机器词原样带出而不是吞掉', () => {
    // Act / Assert
    expect(mirrorPrecheckText(leg({ mirror_precheck: { will_skip: true, reason: 'max_weekly_orders' } }))).toContain('max_weekly_orders');
  });

  it('没查过镜像配额是独立态（与「查了没事」不同）', () => {
    // Act / Assert
    expect(mirrorPrecheckText(leg({ mirror_precheck: null }))).toBe('未查询镜像配额');
  });
});

describe('mirrorPlanIssues', () => {
  const plan = (over: Partial<PushMirrorPlan>): PushMirrorPlan => ({ requested: true, available: true, ...over });

  it('未请求实盘时无问题', () => {
    // Act / Assert
    expect(mirrorPlanIssues({ requested: false })).toEqual([]);
  });

  it('急停与通道未就绪并列展示，不互相吞并', () => {
    // Arrange
    const p = plan({ kill_switch: true, real_trading_ready: false, not_ready_reason: 'ENABLE_REAL_TRADING 未开' });

    // Act
    const issues = mirrorPlanIssues(p);

    // Assert
    expect(issues.some(t => t.includes('急停'))).toBe(true);
    expect(issues.some(t => t.includes('ENABLE_REAL_TRADING'))).toBe(true);
  });

  it('非交易时段入队要说清是「排队」而不是已成交', () => {
    // Act
    const issues = mirrorPlanIssues(plan({ trading_time: false, will_queue: true }));

    // Assert
    expect(issues.some(t => t.includes('入队等开盘'))).toBe(true);
    expect(issues.some(t => t.includes('不会下发'))).toBe(false);
  });

  it('非交易时段且不排队 → 明说真单不会下发', () => {
    // Act / Assert
    expect(mirrorPlanIssues(plan({ trading_time: false, will_queue: false })).join()).toContain('不会下发');
  });

  it('控制面不可读时只报这一条（后面的字段都不可信）', () => {
    // Act
    const issues = mirrorPlanIssues({ requested: true, available: false, reason: 'redis 超时' });

    // Assert
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain('redis 超时');
  });
});

describe('quotaLine', () => {
  it('逐维列出剩余量', () => {
    // Act
    const text = quotaLine({ requested: true, available: true, quota: { remaining_symbols: 5, remaining_orders: 20, remaining_value: 50000 } });

    // Assert
    expect(text).toContain('标的 5 只');
    expect(text).toContain('委托 20 笔');
    expect(text).toContain('¥50,000');
  });

  it('未设上限的维度不出现（`null` 不等于 0）', () => {
    // Act
    const text = quotaLine({ requested: true, available: true, quota: { remaining_symbols: 5, remaining_orders: null, remaining_value: null } });

    // Assert
    expect(text).toBe('今日实盘配额剩余：标的 5 只');
  });

  it('未请求实盘时没有配额行', () => {
    // Act / Assert
    expect(quotaLine({ requested: false })).toBeNull();
  });
});

describe('parseQuantity', () => {
  it('正整数通过', () => {
    // Act / Assert
    expect(parseQuantity(' 300 ')).toEqual({ value: 300, error: '' });
  });

  it('小数被拒（A 股按股申报，0.5 股不存在）', () => {
    // Act / Assert
    expect(parseQuantity('100.5').error).toContain('正整数');
  });

  it('0 与负数被拒', () => {
    // Act / Assert
    expect(parseQuantity('0').error).toContain('大于 0');
    expect(parseQuantity('-100').error).toContain('正整数');
  });

  it('空输入被拒并且不说「正整数」（用户还没打字）', () => {
    // Act / Assert
    expect(parseQuantity('  ').error).toContain('不能为空');
  });

  it('超出单笔上限被拒', () => {
    // Act / Assert
    expect(parseQuantity(String(MAX_QUANTITY + 1)).error).toContain('上限');
  });
});

describe('effectiveQuantity / legAmount', () => {
  it('没有改量时用服务端算的数', () => {
    // Act / Assert
    expect(effectiveQuantity(leg(), new Map())).toBe(500);
  });

  it('改量后金额按新数量算（否则汇总与逐笔对不上）', () => {
    // Arrange
    const edits = new Map([['600036.SH', 1000]]);

    // Act / Assert
    expect(effectiveQuantity(leg(), edits)).toBe(1000);
    expect(legAmount(leg(), edits)).toBe(40000);
  });

  it('缺价的行金额落 0 而不是 NaN（NaN 会渲染成三个字母）', () => {
    // Act / Assert
    expect(legAmount(leg({ price: null }), new Map())).toBe(0);
  });
});

describe('panelSummary', () => {
  // 金额一律由 `price × 数量` 现算（改量后要跟着变），故这里的 amount 必须与之一致：
  // 40×500=20000、100×300=30000。
  const legs = [
    leg({ symbol: '600036.SH', price: 40, quantity: 500, amount: 20000 }),
    leg({ symbol: '600519.SH', price: 100, quantity: 300, amount: 30000 }),
    leg({ symbol: '000001.SZ', executable: false, blocked_by: 'list', problem: '在排除名单内' }),
  ];

  it('金额只算会发出去的腿（把阻断的算进去会误导资金判断）', () => {
    // Act
    const s = panelSummary(legs, new Set(), new Map());

    // Assert
    expect(s.total).toBe(3);
    expect(s.willRun).toBe(2);
    expect(s.blocked).toBe(1);
    expect(s.estAmount).toBe(50000);
  });

  it('勾除的腿不计入执行数，但仍留在总行数里', () => {
    // Act
    const s = panelSummary(legs, new Set(['600519.SH']), new Map());

    // Assert
    expect(s.willRun).toBe(1);
    expect(s.deselected).toBe(1);
    expect(s.estAmount).toBe(20000);
  });

  it('改量后汇总金额跟着变', () => {
    // Act
    const s = panelSummary(legs, new Set(), new Map([['600036.SH', 1000]]));

    // Assert
    expect(s.estAmount).toBe(70000);
  });

  it('被镜像跳过的腿同时计入 blocked 与 mirrorSkips', () => {
    // Arrange：两种「不会下发」是两件事，两个数都要看得见
    const skipped = leg({
      symbol: '601988.SH',
      executable: false,
      problem: '实盘配额不足（max_daily_symbols）',
      mirror_precheck: { will_skip: true, reason: 'max_daily_symbols' },
    });

    // Act
    const s = panelSummary([...legs, skipped], new Set(), new Map());

    // Assert
    expect(s.mirrorSkips).toBe(1);
    expect(s.blocked).toBe(2);
    expect(s.willRun).toBe(2);
  });
});

describe('pushGate', () => {
  const base = { willRun: 1, blocked: 0, invalid: 0, total: 1, deselected: 0, estAmount: 100, mirrorSkips: 0 };

  it('一切都好时可点', () => {
    // Act / Assert
    expect(pushGate(base, { channels: ['sim'], realAck: false }).ok).toBe(true);
  });

  it('全部被阻断时按住按钮并说清是「全部被阻断」', () => {
    // Act
    const gate = pushGate({ ...base, willRun: 0, blocked: 2 }, { channels: ['sim'], realAck: false });

    // Assert
    expect(gate.ok).toBe(false);
    expect(gate.why).toContain('全部被阻断');
  });

  it('只是没勾 → 说「至少勾选 1 笔」，不说阻断', () => {
    // Act
    const gate = pushGate({ ...base, willRun: 0, deselected: 1 }, { channels: ['sim'], realAck: false });

    // Assert
    expect(gate.why).toContain('至少勾选 1 笔');
  });

  it('改量非法时按住', () => {
    // Act / Assert
    expect(pushGate({ ...base, invalid: 1 }, { channels: ['sim'], realAck: false }).why).toContain('不合法');
  });

  it('实盘没输入确认词时按住，输入后放行', () => {
    // Act / Assert
    expect(pushGate(base, { channels: ['sim', 'real'], realAck: false }).why).toContain('确认下单');
    expect(pushGate(base, { channels: ['sim', 'real'], realAck: true }).ok).toBe(true);
  });

  it('仅模拟盘不需要确认词（否则用户会养成闭眼打字的习惯）', () => {
    // Act / Assert
    expect(pushGate(base, { channels: ['sim'], realAck: false }).ok).toBe(true);
  });

  it('预检未回时按住', () => {
    // Act / Assert
    expect(pushGate(base, { channels: ['sim'], realAck: false, loading: true }).why).toContain('预检');
  });
});

describe('legResultView', () => {
  it('未提交（预检阻断）不是失败也不是成功', () => {
    // Arrange
    const r: PushLegResult = { symbol: '600036.SH', success: false, executed: false, skipped_reason: '名单命中' };

    // Act
    const v = legResultView(r);

    // Assert
    expect(v.tone).toBe('skipped');
    expect(v.detail).toBe('名单命中');
  });

  it('幂等命中单列成「重复」，不混进成交', () => {
    // Arrange
    const r: PushLegResult = { symbol: '600036.SH', success: true, executed: true, duplicate: true };

    // Act / Assert
    expect(legResultView(r).tone).toBe('dup');
  });

  it('成交要带数量与成交价', () => {
    // Arrange
    const r: PushLegResult = { symbol: '600036.SH', success: true, executed: true, fill_price: 40.12, filled_quantity: 500, commission: 5 };

    // Act
    const v = legResultView(r);

    // Assert
    expect(v.tone).toBe('ok');
    expect(v.detail).toContain('500 股');
    expect(v.detail).toContain('40.12');
  });

  it('失败没给原因也不留空', () => {
    // Act / Assert
    expect(legResultView({ symbol: 'x', success: false, executed: true }).detail).toBe('未说明原因');
  });
});

describe('mirrorReceiptView', () => {
  it('skipped 显示成「真单未下发」，绝不是成功', () => {
    // Arrange
    const r: PushLegResult = { symbol: 'x', success: true, executed: true, mirror: { status: 'skipped', class: 'skipped', reason: 'whitelist' } };

    // Act
    const v = mirrorReceiptView(r);

    // Assert
    expect(v?.tone).toBe('skipped');
    expect(v?.label).toContain('未下发');
  });

  it('queued 是「还没发」而不是已发', () => {
    // Act / Assert
    expect(mirrorReceiptView({ symbol: 'x', success: true, executed: true, mirror: { status: 'queued', class: 'queued' } })?.tone).toBe('queued');
  });

  it('未知 class 归失败（fail-closed，与后端同口径）', () => {
    // Act / Assert
    expect(mirrorReceiptView({ symbol: 'x', success: true, executed: true, mirror: { status: 'brand_new', class: 'whatever' } })?.tone).toBe('fail');
  });

  it('没有镜像载荷时不显示镜像状态（模拟盘腿）', () => {
    // Act / Assert
    expect(mirrorReceiptView({ symbol: 'x', success: true, executed: true, mirror: null })).toBeNull();
  });
});

describe('executeHeadline', () => {
  const mk = (status: string, s: Partial<PushExecute['summary']> = {}): PushExecute => ({
    batch_id: 'b', dry_run: false, status, channels: ['sim'], channels_effective: ['sim'], results: [],
    summary: { total: 3, executable: 3, blocked: 0, est_amount: 0, attempted: 3, succeeded: 2, failed: 1, skipped: 0, ...s },
  });

  it('executed 说全部完成', () => {
    // Act / Assert
    expect(executeHeadline(mk('executed', { succeeded: 3, failed: 0 })).tone).toBe('ok');
  });

  it('partial 明说几笔成功几笔失败', () => {
    // Act
    const h = executeHeadline(mk('partial'));

    // Assert
    expect(h.tone).toBe('warn');
    expect(h.text).toContain('2');
    expect(h.text).toContain('1');
  });

  it('blocked 说清「一笔都没发出去」', () => {
    // Act
    const h = executeHeadline(mk('blocked', { attempted: 0, succeeded: 0, failed: 0, skipped: 3 }));

    // Assert
    expect(h.tone).toBe('fail');
    expect(h.text).toContain('一笔都没发出去');
  });

  it('未知状态不假装成功', () => {
    // Act / Assert
    expect(executeHeadline(mk('something_new')).tone).toBe('warn');
  });
});

describe('riskVerdictView', () => {
  it('影子期拒单要说成「不拦单」（否则预检说拦、下单却放行，更让人困惑）', () => {
    // Act
    const v = riskVerdictView(leg({ risk_verdict: 'reject', risk_enforced: false, risk_rule_id: 'l3.stale_quote' }));

    // Assert
    expect(v.txt).toBe('影子拒单');
    expect(v.title).toContain('不拦单');
  });

  it('已生效的拒单带规则号', () => {
    // Act / Assert
    expect(riskVerdictView(leg({ risk_verdict: 'reject', risk_enforced: true, risk_rule_id: 'l1.position_cap' })).txt).toContain('l1.position_cap');
  });

  it('悬停把环境闸与标的闸都列出来并标出环境', () => {
    // Act
    const v = riskVerdictView(leg({
      risk_verdict: 'reject',
      risk_enforced: true,
      subject: [{ rule_id: 'l3.lot_size', reason: '非整手' }],
      environment: [{ rule_id: 'l0.session', reason: '非交易时段' }],
    }));

    // Assert
    expect(v.title).toContain('l3.lot_size');
    expect(v.title).toContain('[环境] l0.session');
  });

  it('未判定与判定失败是两回事', () => {
    // Act / Assert
    expect(riskVerdictView(leg({ risk_verdict: 'unavailable' })).txt).toBe('未判定');
    expect(riskVerdictView(leg({ risk_verdict: 'error' })).txt).toBe('判定失败');
  });
});

describe('channelLabel', () => {
  it('实盘是叠加语义，标签如实说「+实盘镜像」', () => {
    // Act / Assert
    expect(channelLabel(['sim'])).toBe('仅模拟盘');
    expect(channelLabel(['sim', 'real'])).toBe('模拟盘 + 实盘镜像');
  });
});

describe('budgetBanner', () => {
  it('没缩量时什么都不说', () => {
    // Arrange / Act / Assert：平时挂一句「本批未超资金」只会稀释真正的警告
    expect(budgetBanner(undefined)).toBe('');
    expect(budgetBanner(null)).toBe('');
    expect(budgetBanner({ applied: false, factor: 1 })).toBe('');
  });

  it('缩量时用服务端的说明（含缩量系数与两个金额）', () => {
    // Arrange
    const note = '本批自动算量合计 ¥1,245,098 超出可用资金 ¥488,924，已按 ×0.3926 等比例缩量';

    // Act / Assert
    expect(budgetBanner({ applied: true, factor: 0.3926, note })).toBe(note);
  });

  it('服务端没给说明时自己拼一句，绝不静默', () => {
    // Arrange：缩小了量却不报，用户会以为数量是原样算出来的
    // Act
    const txt = budgetBanner({ applied: true, factor: 0.3926, available_cash: 488924, planned_amount: 1245098 });

    // Assert
    expect(txt).toContain('488,924');
    expect(txt).toContain('1,245,098');
    expect(txt).toContain('0.3926');
  });

  it('缺金额时退化成一句只说系数的提示，也不返回空串', () => {
    // Act / Assert
    expect(budgetBanner({ applied: true, factor: 0.5 })).toContain('0.5000');
  });
});

// ---------------------------------------------------------------------------
// 实盘直发腿（exec_path='real_direct'）：没有模拟腿的那一类
// ---------------------------------------------------------------------------

describe('realDirectText', () => {
  it('实盘直发要说得出来「不经模拟台账」', () => {
    // Arrange
    const l = leg({ exec_path: 'real_direct', position_source: 'real' });

    // Act / Assert
    expect(isRealDirect(l)).toBe(true);
    expect(realDirectText(l)).toContain('不经模拟台账');
  });

  it('闸门要跳过时说「不会下发」，不说「将下发真单」', () => {
    // Arrange：这一笔是真钱，措辞与镜像腿的「将下发真单」不能混
    const l = leg({
      exec_path: 'real_direct',
      mirror_precheck: { will_skip: true, reason: 'max_daily_value' },
    });

    // Act
    const txt = realDirectText(l);

    // Assert
    expect(txt).toContain('不会下发');
    // 理由沿用镜像那套词（同一个 reason 码只有一份文案，不另编一句）
    expect(txt).toContain(mirrorReasonText('max_daily_value'));
  });

  it('模拟腿（含镜像）不落进直发口径', () => {
    // Arrange
    const l = leg({ exec_path: 'sim' });

    // Act / Assert
    expect(isRealDirect(l)).toBe(false);
  });
});

describe('legResultView · 实盘直发腿', () => {
  const rd = (over: Partial<PushLegResult> = {}): PushLegResult => ({
    symbol: '600036.SH',
    success: true,
    executed: true,
    exec_path: 'real_direct',
    real_direct: { status: 'submitted', class: 'success', limit_price: 39.8 },
    ...over,
  });

  it('已提交不等于已成交（直发腿没有模拟成交）', () => {
    // Arrange / Act
    const v = legResultView(rd());

    // Assert
    expect(v.label).toBe('真单已提交');
    expect(v.label).not.toBe('已成交');
    expect(v.detail).toContain('39.80');
  });

  it('排队中与已提交分开说（真钱还没出去）', () => {
    // Arrange / Act
    const v = legResultView(
      rd({ success: false, real_direct: { status: 'queued', class: 'queued' } }),
    );

    // Assert
    expect(v.tone).toBe('queued');
    expect(v.label).toContain('未发出');
  });

  it('闸门未放行显示成「未提交」，不是失败也不是成功', () => {
    // Arrange / Act
    const v = legResultView(
      rd({
        success: false,
        executed: false,
        skipped_reason: 'outside_trading_hours',
        real_direct: { status: 'skipped', class: 'skipped', reason: 'outside_trading_hours' },
      }),
    );

    // Assert
    expect(v.tone).toBe('skipped');
    expect(v.detail).toBe('outside_trading_hours');
  });

  it('回执缺失时归失败（不默认落进成功）', () => {
    // Arrange / Act
    const v = legResultView(rd({ real_direct: null }));

    // Assert
    expect(v.tone).toBe('fail');
    expect(v.label).toContain('未知');
  });
});
