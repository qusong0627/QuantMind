import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WebSocketService, WebSocketStatus } from '../websocketService';
import { authService } from '../../features/auth/services/authService';

vi.mock('../../features/auth/services/authService', () => ({
  authService: {
    getAccessToken: vi.fn(() => 'test-token'),
  },
}));

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(code = 1000, reason = 'closed', wasClean = true): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason, wasClean } as CloseEvent);
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }
}

describe('WebSocketService', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
    localStorage.clear();
    vi.mocked(authService.getAccessToken).mockReturnValue('test-token');
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('断链后应在 3 秒（抖动上限 4 秒）内重试连接', async () => {
    const service = new WebSocketService('ws://example.test/ws');
    const connectPromise = service.connect();

    expect(FakeWebSocket.instances).toHaveLength(1);
    FakeWebSocket.instances[0].open();
    await connectPromise;

    expect(service.getStatus()).toBe(WebSocketStatus.CONNECTED);

    FakeWebSocket.instances[0].close(1006, 'network error', false);
    expect(service.getStatus()).toBe(WebSocketStatus.RECONNECTING);

    // 首次重连延迟 = 3000ms 基数 + 0~1000ms 随机抖动（见 getReconnectDelay），
    // 因此按「下界之前不重试、上界之内必重试」断言，不依赖 Math.random 的具体取值。
    vi.advanceTimersByTime(2999);
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(1001); // 累计 4000ms，覆盖抖动上限
    expect(FakeWebSocket.instances).toHaveLength(2);

    FakeWebSocket.instances[1].open();
    expect(service.getStatus()).toBe(WebSocketStatus.CONNECTED);
  });

  it('主动断开连接时不应触发自动重试', async () => {
    const service = new WebSocketService('ws://example.test/ws');
    const connectPromise = service.connect();

    FakeWebSocket.instances[0].open();
    await connectPromise;

    service.disconnect();
    expect(service.getStatus()).toBe(WebSocketStatus.DISCONNECTED);

    vi.advanceTimersByTime(30000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('重连成功后应自动恢复已有订阅', async () => {
    const service = new WebSocketService('ws://example.test/ws');
    const connectPromise = service.connect();

    FakeWebSocket.instances[0].open();
    await connectPromise;

    service.subscribe({
      symbols: ['000001.SZ'],
      channels: ['trade.updates.1001'],
    });

    expect(FakeWebSocket.instances[0].sent).toHaveLength(2);

    FakeWebSocket.instances[0].close(1006, 'network error', false);
    vi.advanceTimersByTime(30000);

    expect(FakeWebSocket.instances).toHaveLength(2);
    FakeWebSocket.instances[1].open();

    const replayedMessages = FakeWebSocket.instances[1].sent.map((payload) => JSON.parse(payload));
    expect(replayedMessages).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ action: 'subscribe', symbols: ['000001.SZ'] }),
        expect.objectContaining({ topic: 'trade.updates.1001' }),
      ])
    );
  });

  it('首次鉴权未就绪时应自动补连', async () => {
    vi.mocked(authService.getAccessToken)
      .mockReturnValueOnce(null)
      .mockReturnValue('test-token');

    const service = new WebSocketService('ws://example.test/ws');
    const connectPromise = service.connect();

    await connectPromise;
    expect(service.getStatus()).toBe(WebSocketStatus.RECONNECTING);
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(1);

    await vi.runOnlyPendingTimersAsync();
    expect(FakeWebSocket.instances).toHaveLength(1);

    FakeWebSocket.instances[0].open();
    expect(service.getStatus()).toBe(WebSocketStatus.CONNECTED);
  });
});
