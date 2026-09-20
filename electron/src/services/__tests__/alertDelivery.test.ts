import { describe, test, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  deliverNewAlerts,
  previewAlertSound,
  resetAlertDeliveryState,
} from '../alertDelivery';
import { DEFAULT_ALERT_CONFIG, type HoldingAlertItem } from '../holdingAlertService';

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

/** 最小 AudioContext 替身：只验证「有没有走到发声这条路」，不验证音色 */
class FakeAudioContext {
  state: 'running' | 'suspended' = 'running';
  currentTime = 0;
  destination = {};
  started: number[] = [];
  createOscillator() {
    return {
      type: 'sine',
      frequency: { setValueAtTime: () => undefined },
      connect: () => undefined,
      start: (at: number) => { this.started.push(at); },
      stop: () => undefined,
    };
  }
  createGain() {
    return {
      gain: { setValueAtTime: () => undefined, exponentialRampToValueAtTime: () => undefined },
      connect: () => undefined,
    };
  }
  resume() {
    this.state = 'running';
    return Promise.resolve();
  }
}

let notifySpy: ReturnType<typeof vi.fn>;
let lastCtx: FakeAudioContext | null;

beforeEach(() => {
  resetAlertDeliveryState();
  notifySpy = vi.fn().mockResolvedValue(undefined);
  lastCtx = null;
  (window as unknown as { electronAPI?: unknown }).electronAPI = { showNotification: notifySpy };
  (window as unknown as { AudioContext?: unknown }).AudioContext = class extends FakeAudioContext {
    constructor() {
      super();
      lastCtx = this;
    }
  };
});

afterEach(() => {
  delete (window as unknown as { electronAPI?: unknown }).electronAPI;
  delete (window as unknown as { AudioContext?: unknown }).AudioContext;
});

describe('投递去重（按 id 单调）', () => {
  test('首次调用只播种：不把历史预警全轰一遍', () => {
    // Arrange：挂载时库里已有 5 条历史预警

    // Act
    const outcome = deliverNewAlerts(
      [mkItem({ id: 3 }), mkItem({ id: 5 }), mkItem({ id: 1 })],
      DEFAULT_ALERT_CONFIG,
    );

    // Assert
    expect(outcome.delivered).toBe(0);
    expect(notifySpy).not.toHaveBeenCalled();
  });

  test('播种之后只有 id 更大的才播，且按 id 升序', () => {
    deliverNewAlerts([mkItem({ id: 5 })], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts(
      [mkItem({ id: 9, title: '九' }), mkItem({ id: 6, title: '六' }), mkItem({ id: 5 })],
      DEFAULT_ALERT_CONFIG,
    );

    expect(outcome.delivered).toBe(2);
    expect(notifySpy).toHaveBeenCalledTimes(2);
    expect(notifySpy.mock.calls[0][0]).toContain('六');
    expect(notifySpy.mock.calls[1][0]).toContain('九');
  });

  test('同一批重复喂只播一次（面板轮询 + 投递轮询并发也只会响一声）', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    deliverNewAlerts([mkItem({ id: 7 })], DEFAULT_ALERT_CONFIG);
    deliverNewAlerts([mkItem({ id: 7 })], DEFAULT_ALERT_CONFIG);
    deliverNewAlerts([mkItem({ id: 7 })], DEFAULT_ALERT_CONFIG);

    expect(notifySpy).toHaveBeenCalledTimes(1);
  });

  test('水位只升不降：乱序喂旧数据不会回退', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);
    deliverNewAlerts([mkItem({ id: 9 })], DEFAULT_ALERT_CONFIG);
    expect(notifySpy).toHaveBeenCalledTimes(1);

    // 列表接口只回 active：旧的被忽略后从列表消失，水位不能跟着降回去
    const outcome = deliverNewAlerts([mkItem({ id: 2 })], DEFAULT_ALERT_CONFIG);
    expect(outcome.delivered).toBe(0);
    expect(notifySpy).toHaveBeenCalledTimes(1);
  });
});

describe('配置门控', () => {
  test('总开关关闭：一条都不播', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts(
      [mkItem({ id: 2 })],
      { ...DEFAULT_ALERT_CONFIG, enabled: false },
    );

    expect(outcome).toEqual({ delivered: 0, desktop: 0, sound: 0 });
    expect(notifySpy).not.toHaveBeenCalled();
  });

  test('低于最低级别只留痕不打扰', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts(
      [mkItem({ id: 2, severity: 'info' })],
      { ...DEFAULT_ALERT_CONFIG, min_severity: 'warning' },
    );

    expect(outcome.delivered).toBe(0);
    expect(notifySpy).not.toHaveBeenCalled();
  });

  test('最低级别调成 info 之后同级也播', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts(
      [mkItem({ id: 2, severity: 'info' })],
      { ...DEFAULT_ALERT_CONFIG, min_severity: 'info' },
    );

    expect(outcome.delivered).toBe(1);
  });

  test('桌面通道关掉：不弹通知（但水位照走，之后再开不会补播旧的）', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts(
      [mkItem({ id: 2 })],
      { ...DEFAULT_ALERT_CONFIG, notify_desktop: false, notify_sound: false },
    );

    expect(outcome.desktop).toBe(0);
    expect(outcome.sound).toBe(0);
    expect(notifySpy).not.toHaveBeenCalled();

    const later = deliverNewAlerts([mkItem({ id: 3 })], DEFAULT_ALERT_CONFIG);
    expect(notifySpy).toHaveBeenCalledTimes(1);
    expect(later.delivered).toBe(1);
  });

  test('声音通道关掉：桌面照弹，声音不响', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts(
      [mkItem({ id: 2 })],
      { ...DEFAULT_ALERT_CONFIG, notify_sound: false },
    );

    expect(outcome.desktop).toBe(1);
    expect(outcome.sound).toBe(0);
    expect(lastCtx?.started.length ?? 0).toBe(0);
  });

  test('config 缺失时按「开启」处理，不静默失声', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts([mkItem({ id: 2 })], null);

    expect(outcome.desktop).toBe(1);
    expect(outcome.sound).toBe(1);
  });
});

describe('声音', () => {
  test('危急两声、警告一声', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    deliverNewAlerts([mkItem({ id: 2, severity: 'critical' })], DEFAULT_ALERT_CONFIG);
    const afterCritical = lastCtx?.started.length ?? 0;
    expect(afterCritical).toBe(2);

    deliverNewAlerts([mkItem({ id: 3, severity: 'warning' })], DEFAULT_ALERT_CONFIG);
    expect((lastCtx?.started.length ?? 0) - afterCritical).toBe(1);
  });

  test('环境没有 AudioContext 时不抛错，只是没声音', () => {
    delete (window as unknown as { AudioContext?: unknown }).AudioContext;
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts([mkItem({ id: 2 })], DEFAULT_ALERT_CONFIG);

    expect(outcome.sound).toBe(0);
    expect(outcome.desktop).toBe(1);
  });

  test('试听不依赖水位，可反复播', () => {
    expect(previewAlertSound('critical')).toBe(true);
    expect(previewAlertSound('critical')).toBe(true);
    expect(lastCtx?.started.length).toBe(4);
  });
});

describe('桌面通知调用形状', () => {
  test('标题/正文取自预警本身', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    deliverNewAlerts(
      [mkItem({ id: 2, title: '平安银行 盘中利空', content: '交易所问询函' })],
      DEFAULT_ALERT_CONFIG,
    );

    expect(notifySpy).toHaveBeenCalledWith('平安银行 盘中利空', '交易所问询函');
  });

  test('缺标题/正文时用股票名兜底，不送空字符串', () => {
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    deliverNewAlerts(
      [mkItem({ id: 2, title: '', content: '', stockName: '平安银行', symbol: 'SZ000001' })],
      DEFAULT_ALERT_CONFIG,
    );

    const [title, body] = notifySpy.mock.calls[0];
    expect(title).toContain('平安银行');
    expect(body).toContain('SZ000001');
  });

  test('桌面通道报错被吞掉，不影响其它预警继续播', () => {
    notifySpy.mockImplementation(() => { throw new Error('IPC 挂了'); });
    deliverNewAlerts([], DEFAULT_ALERT_CONFIG);

    const outcome = deliverNewAlerts([mkItem({ id: 2 }), mkItem({ id: 3 })], DEFAULT_ALERT_CONFIG);

    expect(outcome.desktop).toBe(0);
    expect(outcome.sound).toBe(2);
  });
});
