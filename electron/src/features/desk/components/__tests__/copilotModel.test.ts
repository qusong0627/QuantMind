import { describe, expect, it } from 'vitest';
import {
  actionLine,
  adviceStatusMeta,
  alertTypeLabel,
  outcomeMeta,
  panelMetrics,
  severityMeta,
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

  it('adviceStatusMeta 状态映射', () => {
    expect(adviceStatusMeta('pending')).toEqual({ label: '待决', tone: 'blue' });
    expect(adviceStatusMeta('executed').tone).toBe('green');
    expect(adviceStatusMeta('failed').tone).toBe('red');
    expect(adviceStatusMeta('whatever').label).toBe('whatever');
  });

  it('panelMetrics 缺失如实 —（无假数据）', () => {
    const empty = panelMetrics(null);
    expect(empty).toEqual({ latencyP95: '—', missRate: '—', events: 0, budgetText: '—' });

    const filled = panelMetrics({
      latency: { available: true, market_snapshot: { p95_ms: 1234.6 } },
      miss_rate: { available: true, miss_rate: 0.234 },
      events: { available: true, items: [baseEvent] },
      budget: { available: true, detail: { tdx_rss_mb: 700, engine_rss_mb: 200 } },
    });
    expect(filled).toEqual({ latencyP95: '1235ms', missRate: '23.4%', events: 1, budgetText: 'tdx 700MB · 引擎 200MB' });

    const unavailable = panelMetrics({ events: { available: false, reason: 'db down' } });
    expect(unavailable.latencyP95).toBe('—');
    expect(unavailable.events).toBe(0);
  });
});
