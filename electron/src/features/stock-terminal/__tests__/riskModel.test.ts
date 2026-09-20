/**
 * 候选列表风险闸 · 纯逻辑测试（EXCLUDE_ON / riskChips / channelText）。
 *
 * 盯的是三类「错了也不报错」的口径：
 * 1. 开关判据：`undefined` 必须算开（否则「清空筛选」会静默放行全部风险股）；
 * 2. `pos_move` 必须**不出**徽章（涨停/大涨近 20 天命中全市场约 39%，出徽章等于没信息）；
 * 3. 利空与利好同时命中时两枚都要在（不做优先级吞并）。
 */
import { describe, expect, it } from 'vitest';
import { EXCLUDE_ON, channelText, riskChips } from '../riskModel';
import type { StockRisk } from '../../stock-terminal-shared/types';

const bucket = (tag: string, n: number, extra: Partial<{ last: string; samples: string[] }> = {}) => ({
  tag,
  n,
  ...extra,
});

describe('EXCLUDE_ON', () => {
  it('未设置算开启（清空筛选不得放行风险股）', () => {
    // Act / Assert
    expect(EXCLUDE_ON(undefined)).toBe(true);
  });

  it('显式 true 开启、显式 false 关闭', () => {
    // Act / Assert
    expect(EXCLUDE_ON(true)).toBe(true);
    expect(EXCLUDE_ON(false)).toBe(false);
  });
});

describe('channelText', () => {
  it('通道开启时给只数，关闭时给「放行」而非 0', () => {
    // Act / Assert
    expect(channelText(true, 1808)).toBe('1808');
    // 关掉时计数后端恒为 0 —— 显示 0 会被读成「这个通道一只都没命中」
    expect(channelText(false, 0)).toBe('放行');
  });
});

describe('riskChips', () => {
  it('无载荷返回空数组（调用方据此不渲染徽章区）', () => {
    // Act / Assert
    expect(riskChips(null)).toEqual([]);
    expect(riskChips(undefined)).toEqual([]);
    expect(riskChips({ excluded: false })).toEqual([]);
  });

  it('空桶不出徽章', () => {
    // Arrange：五档方向键都在、但都是空列表（后端固定桶的形态）
    const risk: StockRisk = {
      excluded: false,
      news: { risk: [], warn: [], weak: [], pos_strong: [], pos_move: [] },
    };

    // Act
    const chips = riskChips(risk);

    // Assert
    expect(chips).toEqual([]);
  });

  it('pos_move 单列时不出徽章（涨停/大涨命中全市场约 39%，无区分度）', () => {
    // Arrange
    const risk: StockRisk = { excluded: false, news: { pos_move: [bucket('涨停', 2)] } };

    // Act
    const chips = riskChips(risk);

    // Assert
    expect(chips).toEqual([]);
  });

  it('warn 与 weak 合成一枚「提示」，条数分别累计', () => {
    // Arrange
    const risk: StockRisk = {
      excluded: false,
      news: { warn: [bucket('减持', 2)], weak: [bucket('净利润下滑', 3)] },
    };

    // Act
    const chips = riskChips(risk);

    // Assert
    expect(chips).toHaveLength(1);
    expect(chips[0].label).toBe('提示 5');
    expect(chips[0].title).toContain('减持 ×2');
    expect(chips[0].title).toContain('净利润下滑 ×3');
  });

  it('利空与利好同时命中 → 两枚都在，不吞并', () => {
    // Arrange
    const risk: StockRisk = {
      excluded: true,
      news: { risk: [bucket('立案调查', 2)], pos_strong: [bucket('业绩预增', 1)] },
    };

    // Act
    const chips = riskChips(risk);

    // Assert
    expect(chips.map(c => c.label)).toEqual(['利空 2', '利好']);
  });

  it('利空徽章带证据标题（用户要能看到凭什么说它利空）', () => {
    // Arrange
    const risk: StockRisk = {
      excluded: true,
      news: { risk: [bucket('内幕交易', 2, { last: '2026-09-13T12:31:44Z', samples: ['雪天盐业：更正重组预案'] })] },
    };

    // Act
    const chips = riskChips(risk);

    // Assert
    expect(chips[0].title).toContain('内幕交易 ×2');
    expect(chips[0].title).toContain('09-13');
    expect(chips[0].title).toContain('雪天盐业：更正重组预案');
  });

  it('名单命中出灰章，reason 进悬停；未阻断时用浅灰以示区分', () => {
    // Arrange
    const blocking: StockRisk = {
      excluded: true,
      hits: [{ symbol: '600606.SH', sources: ['fundamental_flags'], source_labels: ['基本面长期排除名单'], reason: '连续3年亏损' }],
    };
    const nonBlocking: StockRisk = { excluded: false, hits: [{ symbol: '000001.SZ', sources: ['risk_block_warn'] }] };

    // Act
    const a = riskChips(blocking);
    const b = riskChips(nonBlocking);

    // Assert
    expect(a[0].label).toBe('名单');
    expect(a[0].cls).toContain('slate-200');
    expect(a[0].title).toContain('基本面长期排除名单');
    expect(a[0].title).toContain('连续3年亏损');
    expect(b[0].cls).toContain('slate-100');
    // sources 无 label 时退回名称，不留空括号
    expect(b[0].title).toContain('risk_block_warn');
  });

  it('n 为 1 时不显示条数后缀', () => {
    // Arrange
    const risk: StockRisk = { excluded: false, news: { pos_strong: [bucket('回购', 1)] } };

    // Act
    const chips = riskChips(risk);

    // Assert
    expect(chips[0].label).toBe('利好');
  });
});
