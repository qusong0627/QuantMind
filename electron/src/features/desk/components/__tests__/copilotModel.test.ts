import { describe, expect, it } from 'vitest';
import {
  actionLine,
  adviceOutcomeText,
  adviceOutcomeTone,
  adviceStatsLine,
  adviceStatusMeta,
  alertTypeLabel,
  COPILOT_STALE_SECONDS,
  dataAgeSeconds,
  eventTimeLabel,
  outcomeMeta,
  panelMetrics,
  severityMeta,
  stalenessLabel,
  type CopilotAdvice,
  type CopilotEvent,
} from '../copilotModel';

const baseEvent: CopilotEvent = {
  alert_id: 'a1',
  ts: '2026-09-17T16:00:00+08:00',
  alert_type: 'news:risk_event',
  severity: 'critical',
  market: 'CN',
  symbol: '600036.SH',
  title: '某银行被立案调查',
};

describe('copilotModel', () => {
  it('severityMeta 分级映射', () => {
    expect(severityMeta('critical')).toEqual({ tone: 'red', label: '严重' });
    expect(severityMeta('warn')).toEqual({ tone: 'amber', label: '关注' });
    expect(severityMeta('info')).toEqual({ tone: 'slate', label: '提示' });
    expect(severityMeta('bogus').tone).toBe('slate');
  });

  it('alertTypeLabel 中文标签与回退', () => {
    expect(alertTypeLabel('news:risk_event')).toBe('新闻风险');
    expect(alertTypeLabel('anomaly:volume_surge')).toBe('异常放量');
    expect(alertTypeLabel('custom:thing')).toBe('custom:thing');
  });

  it('outcomeMeta：标注优先于自动回填', () => {
    expect(outcomeMeta({ ...baseEvent, annotation: 'false_positive', hit: true, outcome_status: 'filled' }))
      .toEqual({ label: '标注:误报', tone: 'red' });
    expect(outcomeMeta({ ...baseEvent, hit: true, outcome_status: 'filled' }))
      .toEqual({ label: 'T+1 命中', tone: 'green' });
    expect(outcomeMeta({ ...baseEvent, hit: false, outcome_status: 'filled' }))
      .toEqual({ label: 'T+1 未中', tone: 'red' });
    expect(outcomeMeta({ ...baseEvent, outcome_status: 'no_data' }))
      .toEqual({ label: '无数据', tone: 'slate' });
    expect(outcomeMeta(baseEvent)).toEqual({ label: '待回填', tone: 'slate' });
  });

  it('actionLine 文案（市价/限价）', () => {
    expect(actionLine({ symbol: '600036.SH', side: 'buy', quantity: 900 }))
      .toBe('买入 600036.SH × 900  市价');
    expect(actionLine({ symbol: '600036.SH', side: 'sell', quantity: 100, order_type: 'limit', price: 42.5 }))
      .toBe('卖出 600036.SH × 100  限价 42.5');
  });

  it('adviceStatusMeta 状态映射：执行类标签必须带「模拟」（一键执行=模拟盘，审计 C4）', () => {
    expect(adviceStatusMeta('pending')).toEqual({ label: '待决', tone: 'blue' });
    expect(adviceStatusMeta('executed')).toEqual({ label: '已模拟执行', tone: 'green' });
    expect(adviceStatusMeta('partial').label).toBe('部分模拟执行');
    expect(adviceStatusMeta('failed')).toEqual({ label: '模拟失败', tone: 'red' });
    expect(adviceStatusMeta('whatever').label).toBe('whatever');
  });

  it('panelMetrics 缺失如实 —（无假数据）', () => {
    const empty = panelMetrics(null);
    expect(empty).toEqual({ latencyP95: '—', latencyP95Ms: null, missRate: '—', events: 0, budgetText: '—' });

    const filled = panelMetrics({
      latency: { available: true, market_snapshot: { p95_ms: 1234.6 } },
      miss_rate: { available: true, miss_rate: 0.234 },
      events: { available: true, items: [baseEvent] },
      budget: { available: true, detail: { tdx_rss_mb: 700, engine_rss_mb: 200 } },
    });
    expect(filled).toEqual({ latencyP95: '1.2s', latencyP95Ms: 1234.6, missRate: '23.4%', events: 1, budgetText: 'tdx 700MB · 引擎 200MB' });

    const unavailable = panelMetrics({ events: { available: false, reason: 'db down' } });
    expect(unavailable.latencyP95).toBe('—');
    expect(unavailable.events).toBe(0);
  });

  it('数据时刻年龄与陈旧阈值（审计 H15）：as_of=数据时刻，缺失/坏值 → null 不冒充新鲜', () => {
    const now = Date.parse('2026-10-10T02:00:00Z');
    expect(COPILOT_STALE_SECONDS).toBe(300);
    expect(dataAgeSeconds('2026-10-10T01:59:30Z', now)).toBe(30);
    expect(dataAgeSeconds('2026-10-10T09:00:00+08:00', now)).toBe(3600); // 带偏移的旧形态同样可算
    expect(dataAgeSeconds('2026-10-10T02:00:05Z', now)).toBe(0); // 未来时刻不出负数
    expect(dataAgeSeconds(null, now)).toBeNull();
    expect(dataAgeSeconds(undefined, now)).toBeNull();
    expect(dataAgeSeconds('', now)).toBeNull();
    expect(dataAgeSeconds('not-a-date', now)).toBeNull();
  });

  it('stalenessLabel 人话分级', () => {
    expect(stalenessLabel(30)).toBe('30 秒前');
    expect(stalenessLabel(120)).toBe('2 分钟前');
    expect(stalenessLabel(5400)).toBe('1.5 小时前');
    expect(stalenessLabel(172800)).toBe('2 天前');
  });

  it('eventTimeLabel：同日 HH:MM:SS，跨日 MM-DD HH:MM，不可解析原样返回', () => {
    const d = new Date(2026, 9, 10, 9, 30, 5); // 本地时区构造，断言与运行机时区无关
    expect(eventTimeLabel(d.toISOString(), d.getTime() + 3_600_000)).toBe('09:30:05');
    const nextDay = new Date(2026, 9, 11, 10, 0, 0);
    expect(eventTimeLabel(d.toISOString(), nextDay.getTime())).toBe('10-10 09:30');
    expect(eventTimeLabel('garbage', d.getTime())).toBe('garbage');
  });

  it('建议战绩行：含胜率与平均超额；无兑现如实标注', () => {
    const filled = adviceStatsLine({
      available: true,
      days: 90,
      total: 5,
      decided: 4,
      executed: 3,
      rejected: 1,
      scored: 2,
      by_horizon: {
        '1': { n: 4, hits: 3, hit_rate: 0.75, avg_excess: 0.012 },
        '3': { n: 0, hits: 0, hit_rate: null, avg_excess: null },
        '5': { n: 0, hits: 0, hit_rate: null, avg_excess: null },
      },
    });
    expect(filled).toContain('已决 4（模拟 3 / 拒绝 1）');
    expect(filled).toContain('T+1 胜率 75%');
    expect(filled).toContain('平均超额 +1.2%');

    const waiting = adviceStatsLine({
      available: true,
      days: 90,
      total: 2,
      decided: 0,
      executed: 0,
      rejected: 0,
      scored: 0,
    });
    expect(waiting).toContain('兑现回填次日凌晨产出');
    expect(adviceStatsLine(null)).toContain('暂不可用');
  });

  it('单卡兑现文本与色调（决策日收盘口径）', () => {
    const item: CopilotAdvice = {
      advice_id: 'x',
      source: 'quantbot',
      title: 't',
      rationale: '',
      actions: [],
      status: 'executed',
      created_at: '',
      outcome: {
        base_date: '2026-09-08',
        benchmark: '000300.SH',
        summary: {
          '1': { n: 1, hits: 1, avg_excess: 0.016 },
          '3': { n: 1, hits: 0, avg_excess: -0.008 },
        },
      },
      outcome_status: 'partial',
    };
    expect(adviceOutcomeText(item)).toContain('T+1 +1.6%（1/1 命中）');
    expect(adviceOutcomeText(item)).toContain('T+3 -0.8%（0/1 命中）');
    expect(adviceOutcomeTone(item)).toBe('good');

    const bad: CopilotAdvice = {
      ...item,
      outcome: { summary: { '1': { n: 1, hits: 0, avg_excess: -0.02 } } },
    };
    expect(adviceOutcomeTone(bad)).toBe('bad');
    expect(adviceOutcomeText({ ...item, outcome: null })).toBe('');
    expect(adviceOutcomeTone({ ...item, outcome: null })).toBe('flat');
  });
});
