import { describe, test, expect } from 'vitest';
import {
  alertActionTarget,
  alertAgeText,
  kindLabel,
  panelCounts,
  parseAlertTime,
  scoreTransition,
  sentinelHeadline,
  severityMeta,
  shouldMarkExecuted,
  SENTINEL_STALE_SECONDS,
  formatScore,
} from '../alertModel';
import type { HoldingAlertItem, HoldingSentinelStatus } from '../../../services/holdingAlertService';

const mkItem = (over: Partial<HoldingAlertItem> = {}): HoldingAlertItem => ({
  id: 1,
  symbol: 'SZ000001',
  stockName: '平安银行',
  kind: 'score_cross_zero',
  severity: 'critical',
  title: '平安银行 分数转负',
  content: '+0.401 → -0.153',
  detail: {},
  scorePrev: 0.4012,
  scoreNow: -0.153,
  scoreAsOf: '2026-09-20',
  status: 'active',
  actionUrl: '/trading?tab=position&symbol=SZ000001',
  createdAt: '2026-09-20T02:30:00+00:00',
  resolvedAt: null,
  ...over,
});

describe('展示口径', () => {
  test('未知 kind 原样回显而不是显示空白', () => {
    expect(kindLabel('score_cross_zero')).toBe('分数转负');
    expect(kindLabel('unknown_kind')).toBe('unknown_kind');
    expect(kindLabel('')).toBe('预警');
  });

  test('未知 severity 退到 info（不冒充危急）', () => {
    expect(severityMeta('critical').tone).toBe('red');
    expect(severityMeta('warning').tone).toBe('amber');
    expect(severityMeta('nonsense').label).toBe('提示');
  });

  test('分数带符号三位小数，空值显示占位符', () => {
    expect(formatScore(0.4012)).toBe('+0.401');
    expect(formatScore(-0.153)).toBe('-0.153');
    expect(formatScore(0)).toBe('+0.000');
    expect(formatScore(null)).toBe('—');
    expect(formatScore(undefined)).toBe('—');
  });

  test('分数迁移：两侧都有画箭头，缺一侧只显示现存那侧', () => {
    expect(scoreTransition({ scorePrev: 0.4012, scoreNow: -0.153 })).toBe('+0.401 → -0.153');
    expect(scoreTransition({ scorePrev: null, scoreNow: -0.153 })).toBe('现 -0.153');
    expect(scoreTransition({ scorePrev: 0.2, scoreNow: null })).toBe('曾 +0.200');
    expect(scoreTransition({ scorePrev: null, scoreNow: null })).toBe('—');
  });
});

describe('时间解析', () => {
  test('带时区的按原时区，不带时区的按 UTC（不能按浏览器本地时区）', () => {
    // Arrange
    const withZone = '2026-09-20T02:30:00+00:00';
    const naive = '2026-09-20T02:30:00';

    // Act + Assert：两者应当解析成同一时刻
    expect(parseAlertTime(withZone)?.getTime()).toBe(parseAlertTime(naive)?.getTime());
  });

  test('Z 结尾与非法值', () => {
    expect(parseAlertTime('2026-09-20T02:30:00Z')?.toISOString()).toBe('2026-09-20T02:30:00.000Z');
    expect(parseAlertTime('')).toBeNull();
    expect(parseAlertTime(null)).toBeNull();
    expect(parseAlertTime('not-a-date')).toBeNull();
  });

  test('相对时间按传入的 now 计算（不依赖真实时钟）', () => {
    const base = Date.parse('2026-09-20T02:30:00Z');
    expect(alertAgeText('2026-09-20T02:30:00Z', base + 30_000)).toBe('30 秒前');
    expect(alertAgeText('2026-09-20T02:30:00Z', base + 5 * 60_000)).toBe('5 分钟前');
    expect(alertAgeText('2026-09-20T02:30:00Z', base + 3 * 3600_000)).toBe('3 小时前');
    expect(alertAgeText('2026-09-20T02:30:00Z', base + 2 * 86_400_000)).toBe('2 天前');
    // 时钟回拨也不能算出负数
    expect(alertAgeText('2026-09-20T02:30:00Z', base - 5_000)).toBe('0 秒前');
    expect(alertAgeText(null)).toBe('—');
  });
});

describe('哨兵状态文案（如实优先）', () => {
  const running: HoldingSentinelStatus = {
    running: true,
    lastScanEpoch: 1_700_000_000,
    monitored: 106,
    mine: { monitored: 58 },
  };

  test('没在跑 → 红条并直说不会有预警', () => {
    const h = sentinelHeadline({ running: false, reason: 'Redis 不可用' });
    expect(h.warn).toBe(true);
    expect(h.tone).toBe('red');
    expect(h.text).toContain('未运行');
    expect(h.text).toContain('Redis 不可用');
  });

  test('心跳过期 → 琥珀条并说明可能滞后', () => {
    const nowMs = 1_700_000_000_000 + (SENTINEL_STALE_SECONDS + 120) * 1000;
    const h = sentinelHeadline(running, nowMs);
    expect(h.warn).toBe(true);
    expect(h.tone).toBe('amber');
    expect(h.text).toContain('心跳过期');
  });

  test('运行且新鲜 → 绿条，带上本人监控数', () => {
    const h = sentinelHeadline(running, 1_700_000_000_000 + 10_000);
    expect(h.warn).toBe(false);
    expect(h.tone).toBe('green');
    expect(h.text).toContain('58 只');
  });

  test('扫到你名下 0 只 → 不能算「一切正常」', () => {
    const h = sentinelHeadline({ ...running, mine: { monitored: 0 } }, 1_700_000_000_000 + 10_000);
    expect(h.warn).toBe(true);
    expect(h.text).toContain('没扫到');
  });

  test('无 per-user 明细时退回全局口径，不假装是「你的」', () => {
    const h = sentinelHeadline({ ...running, mine: null }, 1_700_000_000_000 + 10_000);
    expect(h.text).toContain('全局监控 106 只');
  });

  test('完全没状态（接口没回）也算未运行', () => {
    expect(sentinelHeadline(null).warn).toBe(true);
    expect(sentinelHeadline(undefined).text).toContain('未运行');
  });
});

describe('面板计数', () => {
  test('待处理取后端全量真值，危急只在已加载的那批里数', () => {
    // Arrange：后端说 active=9（比本页 3 条多），本页 3 条里 2 条危急
    const items = [
      mkItem({ id: 1, severity: 'critical' }),
      mkItem({ id: 2, severity: 'critical' }),
      mkItem({ id: 3, severity: 'warning' }),
    ];

    // Act
    const { active, criticalInPage } = panelCounts({ active: 9, total: 11 }, items);

    // Assert
    expect(active).toBe(9);
    expect(criticalInPage).toBe(2);
  });

  test('后端没给 counts 时退回本地统计，且不把已定局的算进危急', () => {
    const items = [
      mkItem({ id: 1, severity: 'critical', status: 'active' }),
      mkItem({ id: 2, severity: 'critical', status: 'executed' }),
    ];
    const { active, criticalInPage } = panelCounts(undefined, items);
    expect(active).toBe(1);
    expect(criticalInPage).toBe(1);
  });

  test('空输入不炸', () => {
    expect(panelCounts(null, [])).toEqual({ active: 0, criticalInPage: 0 });
  });
});

describe('卖出回执是否够格标「已卖出」', () => {
  test('真提交（executed/partial）才标', () => {
    expect(shouldMarkExecuted({ status: 'executed', summary: { succeeded: 1 } })).toBe(true);
    expect(shouldMarkExecuted({ status: 'partial', summary: { succeeded: 1, failed: 1 } })).toBe(true);
  });

  test('被拦/失败/预演/零成功一律不标（否则「已卖出」是假的）', () => {
    expect(shouldMarkExecuted({ status: 'blocked', summary: { succeeded: 0 } })).toBe(false);
    expect(shouldMarkExecuted({ status: 'failed', summary: { succeeded: 0, failed: 1 } })).toBe(false);
    expect(shouldMarkExecuted({ status: 'executed', dry_run: true, summary: { succeeded: 1 } })).toBe(false);
    expect(shouldMarkExecuted({ status: 'executed', summary: { succeeded: 0, skipped: 1 } })).toBe(false);
    expect(shouldMarkExecuted(null)).toBe(false);
  });
});

describe('深链目标', () => {
  test('站内路径直接用', () => {
    expect(alertActionTarget('/trading?tab=position&symbol=SZ000001')).toBe('/trading?tab=position&symbol=SZ000001');
  });

  test('外链/空值退回交易台（不把外链丢进 router）', () => {
    expect(alertActionTarget('https://evil.example.com')).toBe('/trading');
    expect(alertActionTarget('')).toBe('/trading');
    expect(alertActionTarget(null)).toBe('/trading');
  });
});
