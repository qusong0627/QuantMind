/**
 * 交易黑名单表格纯逻辑测试。
 *
 * 盯的是三处「按错了也不报错」的地方：
 * 1. 机器行不能给「删除」按钮（删了下次导入又回来）；
 * 2. 放行到期后必须回落成机器命中（否则用户以为可以买）；
 * 3. 放行未命中必须显式提示（否则用户以为解除了一条不存在的限制）。
 */

import { describe, expect, it } from 'vitest';
import {
  allowMissNote,
  expiredManual,
  manualActive,
  reasonView,
  rowActions,
  rowBadge,
  rowKind,
  sourceBreakdown,
  sourceLines,
  staleNote,
  summarize,
} from '../blacklistModel';
import type { BlacklistListMeta, BlacklistRow } from '../types';

const TODAY = '2026-09-20';

const machine = (over: Partial<BlacklistRow> = {}): BlacklistRow => ({
  symbol: '600606.SH',
  name: '绿地控股',
  sources: ['fundamental_flags'],
  source_labels: ['基本面长期排除名单'],
  reason: '连续 3 年亏损',
  expire: null,
  blocking: true,
  expired: false,
  by_source: {
    fundamental_flags: { label: '基本面长期排除名单', reason: '连续 3 年亏损', expire: null },
  },
  manual: null,
  ...over,
});

const withManual = (over: Partial<BlacklistRow>): BlacklistRow =>
  machine({ manual: { symbol: '600606.SH', action: 'block', reason: '', note: '', expire: null, operator: '10000001', created_at: '', updated_at: '' }, ...over });

describe('rowKind', () => {
  it('无本人改动 → 机器命中', () => {
    expect(rowKind(machine(), TODAY)).toBe('machine');
  });

  it('本人手工排除 → manual', () => {
    expect(rowKind(withManual({}), TODAY)).toBe('manual');
  });

  it('本人放行 → allowed', () => {
    const row = machine({ blocking: false, manual: { symbol: '600606.SH', action: 'allow', reason: '已重组', note: '', expire: null, operator: '', created_at: '', updated_at: '' } });
    expect(rowKind(row, TODAY)).toBe('allowed');
  });

  it('放行已到期 → 回落成机器命中（那只票又被排除了）', () => {
    // Arrange：放行到 9/1，今天 9/20
    const row = machine({
      blocking: true,
      manual: { symbol: '600606.SH', action: 'allow', reason: '', note: '', expire: '2026-09-01', operator: '', created_at: '', updated_at: '' },
    });

    // Act / Assert
    expect(rowKind(row, TODAY)).toBe('machine');
    expect(expiredManual(row, TODAY)).toBe(true);
  });

  it('放行到期当天仍算放行（与后端 expire >= today 同口径）', () => {
    // Arrange
    const row = machine({
      blocking: false,
      manual: { symbol: '600606.SH', action: 'allow', reason: '', note: '', expire: TODAY, operator: '', created_at: '', updated_at: '' },
    });

    // Assert
    expect(rowKind(row, TODAY)).toBe('allowed');
    expect(manualActive(row, TODAY)).toBe(true);
  });
});

describe('rowActions', () => {
  it('机器行只给「放行」，绝不给删除（删了下次导入又回来）', () => {
    // Act
    const actions = rowActions(machine(), TODAY);

    // Assert
    expect(actions.map(a => a.kind)).toEqual(['allow']);
  });

  it('本人排除的行给「改理由」+「撤销排除」，不给放行（放行会把这条改成 allow，票反而从表里消失）', () => {
    // Act
    const actions = rowActions(withManual({}), TODAY);

    // Assert
    expect(actions.map(a => a.kind)).toEqual(['edit', 'delete']);
  });

  it('已放行的行给「改理由」+「取消放行」', () => {
    // Arrange
    const row = machine({ blocking: false, manual: { symbol: '600606.SH', action: 'allow', reason: '', note: '', expire: null, operator: '', created_at: '', updated_at: '' } });

    // Act / Assert
    expect(rowActions(row, TODAY).map(a => a.kind)).toEqual(['edit', 'unallow']);
  });

  it('机器行没有「改理由」——那 1800+ 条的理由改了也会被下次导入覆盖', () => {
    // Act / Assert
    expect(rowActions(machine(), TODAY).map(a => a.kind)).not.toContain('edit');
  });

  it('放行到期后回到「放行」按钮（可以再放一次）', () => {
    // Arrange
    const row = machine({
      manual: { symbol: '600606.SH', action: 'allow', reason: '', note: '', expire: '2026-09-01', operator: '', created_at: '', updated_at: '' },
    });

    // Act / Assert
    expect(rowActions(row, TODAY).map(a => a.kind)).toEqual(['allow']);
  });
});

describe('rowBadge', () => {
  it('拦买中的机器行标「拦买」而不是「已放行」', () => {
    // Act / Assert
    expect(rowBadge(machine(), TODAY).label).toBe('拦买');
  });

  it('放行行标「已放行」并把理由放进悬停', () => {
    // Arrange
    const row = machine({ blocking: false, manual: { symbol: '600606.SH', action: 'allow', reason: '已重组', note: '', expire: null, operator: '', created_at: '', updated_at: '' } });

    // Act
    const badge = rowBadge(row, TODAY);

    // Assert
    expect(badge.label).toBe('已放行');
    expect(badge.title).toContain('已重组');
  });

  it('窗口已过期的机器行标「已失效」而不是「拦买」', () => {
    // Arrange：unlock 类条目带 expire，过期后不再拦买
    const row = machine({ blocking: false, expired: true, expire: '2026-09-01' });

    // Act / Assert
    expect(rowBadge(row, TODAY).label).toBe('已失效');
  });

  it('机器行的悬停带上来源与理由（凭什么拦我）', () => {
    // Act
    const badge = rowBadge(machine(), TODAY);

    // Assert
    expect(badge.title).toContain('基本面长期排除名单');
    expect(badge.title).toContain('连续 3 年亏损');
  });
});

describe('summarize', () => {
  const meta: BlacklistListMeta = {
    asof: '2026-09-18',
    stale_days: 2,
    stale: false,
    blocking_now: 1809,
    counts: { total: 1810, by_source: {} },
    overlay: { updated_at: '2026-09-20T00:00:00Z', manual: 3, allow: 1, allow_miss: 2 },
  };

  it('拦买只数取 blocking_now（合并放行后的真实值），不是名单总条数', () => {
    // Act
    const s = summarize(meta, true);

    // Assert
    expect(s.total).toBe(1810);
    expect(s.blocking).toBe(1809);
    expect(s.manual).toBe(3);
    expect(s.allow).toBe(1);
    expect(s.allowMiss).toBe(2);
  });

  it('名单未导入 → imported=false 且条数为 0，不假装「已过滤、一只都没命中」', () => {
    // Act
    const s = summarize(null, false, '名单文件未导入');

    // Assert
    expect(s.imported).toBe(false);
    expect(s.reason).toBe('名单文件未导入');
    expect(s.blocking).toBe(0);
  });

  it('meta 缺字段时全部落 0 而不是 NaN（NaN 会渲染成 "NaN" 三个字母）', () => {
    // Act
    const s = summarize({}, true);

    // Assert
    expect(s.blocking).toBe(0);
    expect(Number.isNaN(s.total)).toBe(false);
  });
});

describe('staleNote', () => {
  it('陈旧时给出基准日与天数', () => {
    // Arrange
    const s = summarize({ asof: '2026-09-01', stale: true, stale_days: 19 }, true);

    // Act
    const note = staleNote(s);

    // Assert
    expect(note).toContain('2026-09-01');
    expect(note).toContain('19');
  });

  it('未导入时不谈陈旧（没有基准日可谈）', () => {
    // Act / Assert
    expect(staleNote(summarize(null, false))).toBeNull();
  });

  it('新鲜时无提示', () => {
    // Act / Assert
    expect(staleNote(summarize({ asof: '2026-09-19', stale: false }, true))).toBeNull();
  });
});

describe('allowMissNote', () => {
  it('有放空炮的放行时提示', () => {
    // Act
    const note = allowMissNote(summarize({ overlay: { allow_miss: 2 } }, true));

    // Assert
    expect(note).toContain('2');
  });

  it('没有放空炮时无提示（不制造假警报）', () => {
    // Act / Assert
    expect(allowMissNote(summarize({ overlay: { allow_miss: 0 } }, true))).toBeNull();
  });
});

describe('sourceBreakdown', () => {
  it('逐源列出标签与理由', () => {
    // Act
    const text = sourceBreakdown(machine());

    // Assert
    expect(text).toContain('基本面长期排除名单');
    expect(text).toContain('连续 3 年亏损');
  });

  it('无 by_source 时退回合并理由，不留空', () => {
    // Act
    const text = sourceBreakdown(machine({ by_source: undefined }));

    // Assert
    expect(text).toBe('连续 3 年亏损');
  });
});

describe('reasonView', () => {
  it('机器行只有机器那层，没有可编辑的「我的理由」', () => {
    // Act
    const view = reasonView(machine(), TODAY);

    // Assert
    expect(view.mine).toBeNull();
    expect(view.machine).toContain('连续 3 年亏损');
  });

  it('本人排除的行两层都在：我的理由可编辑、机器理由只读', () => {
    // Arrange
    const row = withManual({
      manual: { symbol: '600606.SH', action: 'block', reason: '本人不买银行', note: '', expire: null, operator: '10000001', created_at: '', updated_at: '' },
    });

    // Act
    const view = reasonView(row, TODAY);

    // Assert：两层必须分开给出——合成一段用户会去改一段自己改不动的文字
    expect(view.mine).toEqual({
      action: 'block',
      text: '本人不买银行',
      expire: null,
      expired: false,
    });
    expect(view.machine).toContain('基本面长期排除名单');
  });

  it('放行行的 mine.action 是 allow（徽章据此上绿色）', () => {
    // Arrange
    const row = machine({
      blocking: false,
      manual: { symbol: '600606.SH', action: 'allow', reason: '已重组', note: '', expire: null, operator: '', created_at: '', updated_at: '' },
    });

    // Act / Assert
    expect(reasonView(row, TODAY).mine?.action).toBe('allow');
  });

  it('没填理由时给占位文案而不是空串（空白看起来像界面坏了）', () => {
    // Arrange
    const row = withManual({
      manual: { symbol: '600606.SH', action: 'block', reason: '   ', note: '', expire: null, operator: '10000001', created_at: '', updated_at: '' },
    });

    // Act / Assert
    expect(reasonView(row, TODAY).mine?.text).toBe('（未填理由）');
  });

  it('到期已过的本人改动仍原样展示，但标 expired', () => {
    // Arrange
    const row = machine({
      manual: { symbol: '600606.SH', action: 'allow', reason: '临时放行', note: '', expire: '2026-09-01', operator: '', created_at: '', updated_at: '' },
    });

    // Act
    const view = reasonView(row, TODAY);

    // Assert：理由要留着（用户得知道自己上一条写了什么），但明确标出已过期
    expect(view.mine?.text).toBe('临时放行');
    expect(view.mine?.expired).toBe(true);
  });
});

describe('sourceLines', () => {
  it('逐源保留原话（弹窗要能对得上源文件）', () => {
    // Arrange：两个来源各带自己的原话
    const row = machine({
      by_source: {
        fundamental_flags: { label: '基本面长期排除名单', reason: '连续3年亏损；资产负债率92%', expire: null },
        risk_block: { label: '事件风险清单', reason: '股价1.32元低于2.0元预警线', expire: '2026-09-25' },
      },
    });

    // Act
    const lines = sourceLines(row);

    // Assert：合并理由去过重，这里不去 —— 逐源明细必须与源文件逐字一致
    expect(lines).toEqual([
      { label: '基本面长期排除名单', reason: '连续3年亏损；资产负债率92%' },
      { label: '事件风险清单', reason: '股价1.32元低于2.0元预警线' },
    ]);
  });

  it('缺显示名时回落到来源键，缺理由时留空串', () => {
    // Arrange：只有键、没有 label / reason
    const row = machine({ by_source: { risk_block_watch: { reason: '' } } });

    // Act
    const lines = sourceLines(row);

    // Assert：不编造一句「无理由」——那会让人以为系统查过并给出了结论
    expect(lines).toEqual([{ label: 'risk_block_watch', reason: '' }]);
  });

  it('没有逐源明细时返回空数组（弹窗据此不渲染那一段）', () => {
    // Act / Assert
    expect(sourceLines(machine({ by_source: {} }))).toEqual([]);
  });
});
