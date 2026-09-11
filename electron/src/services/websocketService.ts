// WebSocket连接状态
export enum WebSocketStatus {
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
  DISCONNECTED = 'disconnected',
  RECONNECTING = 'reconnecting',
  ERROR = 'error'
}

// 导入服务配置
import { SERVICE_URLS } from '../config/services';
import { authService } from '../features/auth/services/authService';

// WebSocket消息类型
export enum MessageType {
  MARKET_DATA = 'market_data',
  TRADE_SIGNAL = 'trade_signal',
  TRADE_UPDATE = 'trade_update',
  STRATEGY_UPDATE = 'strategy_update',
  NOTIFICATION = 'notification',
  SYSTEM_ALERT = 'system_alert',
  HEARTBEAT = 'heartbeat',
  SUBSCRIBE = 'subscribe',
  UNSUBSCRIBE = 'unsubscribe'
}

// WebSocket消息接口
export interface WebSocketMessage {
  type: MessageType;
  timestamp: string;
  data: unknown;
  id?: string;
}

// 订阅配置
export interface SubscriptionConfig {
  symbols?: string[];
  channels?: string[];
  frequency?: number;
}

class WebSocketService {
  private ws: WebSocket | null = null;
  private connectPromise: Promise<void> | null = null;
  private status: WebSocketStatus = WebSocketStatus.DISCONNECTED;
  private reconnectAttempts = 0;
  private reconnectDelay = 3000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private authReconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private authReconnectAttempts = 0;
  private manualDisconnect = false;
  private heartbeatInterval: ReturnType<typeof setInterval> | null = null;
  private symbolSubscriptions = new Set<string>();
  private channelSubscriptions = new Set<string>();
  private messageHandlers = new Map<MessageType, Set<(data: unknown) => void>>();
  private statusHandlers = new Set<(status: WebSocketStatus) => void>();

  private getStoredToken(): string | null {
    const raw = authService.getAccessToken();
    if (!raw) return null;
    return raw.startsWith('Bearer ') ? raw.slice(7).trim() : raw.trim();
  }

  constructor(url?: string) {
    // 允许外部覆盖；默认值在 connect 时动态解析
    if (url) {
      this.customUrl = url;
    }
  }

  private customUrl?: string;

  private resolveWebSocketUrl(): string {
    return this.customUrl || SERVICE_URLS.WEBSOCKET_MARKET;
  }

  // 连接WebSocket
  connect(): Promise<void> {
    if (this.connectPromise) {
      return this.connectPromise;
    }

    this.manualDisconnect = false;
    this.clearReconnectTimer();
    this.clearAuthReconnectTimer();

    let shouldClearConnectPromise = false;
    const connectPromise = new Promise<void>((resolve, reject) => {
      const disableWebSocket =
        String((import.meta as any).env?.VITE_DISABLE_WEBSOCKET || '').toLowerCase() === 'true';
      if (disableWebSocket) {
        this.setStatus(WebSocketStatus.DISCONNECTED);
        shouldClearConnectPromise = true;
        console.info('WebSocket已禁用，跳过连接');
        resolve();
        return;
      }
      if (this.ws?.readyState === WebSocket.OPEN) {
        shouldClearConnectPromise = true;
        resolve();
        return;
      }
      if (this.ws?.readyState === WebSocket.CONNECTING) {
        shouldClearConnectPromise = true;
        resolve();
        return;
      }

      this.setStatus(WebSocketStatus.CONNECTING);

      let wsUrl = this.resolveWebSocketUrl();
      try {
        const urlObj = new URL(wsUrl);
        const token = this.getStoredToken();
        if (!token) {
          console.info('未检测到 access token，跳过 WebSocket 连接');
          this.scheduleAuthReconnect();
          shouldClearConnectPromise = true;
          resolve();
          return;
        }
        urlObj.searchParams.append('token', token);

        const tenantId = localStorage.getItem('tenant_id');
        if (tenantId) urlObj.searchParams.append('tenant_id', tenantId);

        const userStr = localStorage.getItem('user');
        if (userStr) {
          try {
            const userObj = JSON.parse(userStr);
            const userId = userObj.id || userObj.user_id;
            if (userId) urlObj.searchParams.append('user_id', String(userId));
          } catch (e) {
            // ignore
          }
        }
        wsUrl = urlObj.toString();
      } catch (e) {
        console.warn('无法解析和补充WebSocket鉴权参数:', e);
      }

      console.log('正在连接WebSocket:', wsUrl.replace(/token=[^&]+/, 'token=***'));

      try {
        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
          console.log('WebSocket连接成功');
          this.setStatus(WebSocketStatus.CONNECTED);
          this.reconnectAttempts = 0;
          this.authReconnectAttempts = 0;
          this.clearAuthReconnectTimer();
          this.startHeartbeat();
          this.resubscribeAll();
          this.connectPromise = null;
          resolve();
        };

        this.ws.onmessage = (event) => {
          this.handleMessage(event.data);
        };

        this.ws.onclose = (event) => {
          console.log('WebSocket连接关闭:', event.code, event.reason);
          this.ws = null;
          this.connectPromise = null;
          this.stopHeartbeat();
          if (!this.manualDisconnect) {
            this.attemptReconnect();
          } else {
            this.setStatus(WebSocketStatus.DISCONNECTED);
          }
        };

        this.ws.onerror = (error) => {
          // 连接拒绝等网络瞬断降为 debug，避免前端不停弹 error
          const msg = String((error as any)?.message || '');
          if (msg.includes('ERR_CONNECTION_REFUSED') || this.reconnectAttempts > 2) {
            console.debug('WebSocket错误(退避):', error);
          } else {
            console.error('WebSocket错误:', error);
          }
          this.setStatus(WebSocketStatus.ERROR);
          this.connectPromise = null;
          reject(error);
        };

      } catch (error) {
        console.error('创建WebSocket连接失败:', error);
        this.setStatus(WebSocketStatus.ERROR);
        this.connectPromise = null;
        reject(error);
      }
    });
    this.connectPromise = shouldClearConnectPromise ? null : connectPromise;
    return connectPromise;
  }

  // 断开连接
  disconnect(): void {
    this.manualDisconnect = true;
    this.clearReconnectTimer();
    this.clearAuthReconnectTimer();
    this.authReconnectAttempts = 0;
    if (this.ws) {
      this.ws.close(1000, '主动断开连接');
      this.ws = null;
    }
    this.connectPromise = null;
    this.stopHeartbeat();
    this.setStatus(WebSocketStatus.DISCONNECTED);
  }

  // 发送消息
  send(message: WebSocketMessage): boolean {
    const disableWebSocket =
      String((import.meta as any).env?.VITE_DISABLE_WEBSOCKET || '').toLowerCase() === 'true';
    if (disableWebSocket) return false;
    if (this.ws?.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify(message));
        return true;
      } catch (error) {
        console.error('发送WebSocket消息失败:', error);
        return false;
      }
    }
    return false;
  }

  // 订阅数据
  subscribe(config: SubscriptionConfig): void {
    let sent = false;

    if (config.symbols?.length) {
      config.symbols.forEach(symbol => this.symbolSubscriptions.add(symbol));
      sent = this.send({
        type: MessageType.SUBSCRIBE,
        timestamp: new Date().toISOString(),
        data: undefined,
        action: 'subscribe' as any,
        symbols: config.symbols,
      } as WebSocketMessage & { action: string; symbols: string[] }) || sent;
    }

    if (config.channels?.length) {
      config.channels.forEach(channel => this.channelSubscriptions.add(channel));
      config.channels.forEach((channel) => {
        const channelSent = this.send({
          type: MessageType.SUBSCRIBE,
          timestamp: new Date().toISOString(),
          data: undefined,
          topic: channel,
        } as WebSocketMessage & { topic: string });
        sent = channelSent || sent;
      });
    }
    if (!sent) return;
  }

  // 取消订阅
  unsubscribe(symbols: string[]): void {
    symbols.forEach((symbol) => {
      const key = String(symbol);
      let sent = false;
      if (
        key.startsWith('trade.updates.') ||
        key.startsWith('notification.') ||
        key.startsWith('stock.')
      ) {
        sent = this.send({
          type: MessageType.UNSUBSCRIBE,
          timestamp: new Date().toISOString(),
          data: undefined,
          topic: key,
        } as WebSocketMessage & { topic: string });
      }
      if (!sent) {
        sent = this.send({
          type: MessageType.UNSUBSCRIBE,
          timestamp: new Date().toISOString(),
          data: undefined,
          action: 'unsubscribe' as any,
          symbols: [key],
        } as WebSocketMessage & { action: string; symbols: string[] });
      }
      if (sent) {
        this.symbolSubscriptions.delete(key);
        this.channelSubscriptions.delete(key);
      }
    });
  }

  // 添加消息处理器
  addMessageHandler(type: MessageType, handler: (data: unknown) => void): void {
    if (!this.messageHandlers.has(type)) {
      this.messageHandlers.set(type, new Set());
    }
    this.messageHandlers.get(type)!.add(handler);
  }

  // 移除消息处理器
  removeMessageHandler(type: MessageType, handler: (data: unknown) => void): void {
    this.messageHandlers.get(type)?.delete(handler);
  }

  // 添加状态变化处理器
  addStatusHandler(handler: (status: WebSocketStatus) => void): void {
    this.statusHandlers.add(handler);
  }

  // 移除状态变化处理器
  removeStatusHandler(handler: (status: WebSocketStatus) => void): void {
    this.statusHandlers.delete(handler);
  }

  // 获取当前状态
  getStatus(): WebSocketStatus {
    return this.status;
  }

  // 获取连接信息
  getConnectionInfo() {
    return {
      status: this.status,
      url: this.resolveWebSocketUrl(),
      subscriptions: [...this.symbolSubscriptions, ...this.channelSubscriptions],
      reconnectAttempts: this.reconnectAttempts,
      isConnected: this.status === WebSocketStatus.CONNECTED
    };
  }

  // 私有方法：设置状态
  private setStatus(status: WebSocketStatus): void {
    if (this.status !== status) {
      this.status = status;
      this.statusHandlers.forEach(handler => handler(status));
    }
  }

  // 私有方法：处理消息
  private handleMessage(data: string): void {
    try {
      const message: WebSocketMessage = JSON.parse(data);

      // 处理心跳消息
      if (message.type === MessageType.HEARTBEAT || (message as any).type === 'pong') {
        this.handleHeartbeat();
        return;
      }

      // 分发消息给对应的处理器
      const handlers = this.messageHandlers.get(message.type);
      if (handlers) {
        handlers.forEach(handler => {
          try {
            handler(message.data);
          } catch (error) {
            console.error('消息处理器执行失败:', error);
          }
        });
      }

    } catch (error) {
      console.error('解析WebSocket消息失败:', error);
    }
  }

  // 私有方法：重连（指数退避 + 抖动，避免刷屏）
  private attemptReconnect(): void {
    const disableWebSocket =
      String((import.meta as any).env?.VITE_DISABLE_WEBSOCKET || '').toLowerCase() === 'true';
    if (disableWebSocket) {
      this.setStatus(WebSocketStatus.DISCONNECTED);
      return;
    }
    if (this.manualDisconnect || this.reconnectTimer) {
      return;
    }
    // 页面不可见时暂停重连，恢复可见时再试
    if (typeof document !== 'undefined' && document.hidden) {
      this.setStatus(WebSocketStatus.DISCONNECTED);
      return;
    }
    this.reconnectAttempts++;
    this.setStatus(WebSocketStatus.RECONNECTING);

    const base = this.reconnectDelay;
    const exp = Math.min(base * Math.pow(1.8, this.reconnectAttempts - 1), 30000);
    const jitter = Math.random() * 1000;
    const delay = Math.round(exp + jitter);
    if (this.reconnectAttempts <= 3) {
      console.log(`WebSocket断链，将在 ${delay}ms 后第 ${this.reconnectAttempts} 次重试`);
    } else {
      console.debug(`WebSocket重连退避 ${delay}ms (第${this.reconnectAttempts}次)`);
    }

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect().catch(error => {
        // 网络瞬断时降为 debug，避免控制台刷屏
        if (String(error?.message || '').includes('ERR_CONNECTION_REFUSED') || this.reconnectAttempts > 3) {
          console.debug('重连失败(退避中):', error?.message || error);
        } else {
          console.error('重连失败:', error);
        }
      });
    }, delay);
  }

  // 私有方法：开始心跳
  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatInterval = setInterval(() => {
      const heartbeatMessage: WebSocketMessage = {
        // 后端 ws_core 协议使用 ping/pong
        type: 'ping' as MessageType,
        timestamp: new Date().toISOString(),
        data: { ping: true }
      };
      this.send(heartbeatMessage);
    }, 30000); // 30秒心跳
  }

  // 私有方法：停止心跳
  private stopHeartbeat(): void {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  // 私有方法：处理心跳响应
  private handleHeartbeat(): void {
    // 心跳响应处理逻辑
    console.debug('收到心跳响应');
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private scheduleAuthReconnect(): void {
    const disableWebSocket =
      String((import.meta as any).env?.VITE_DISABLE_WEBSOCKET || '').toLowerCase() === 'true';
    if (disableWebSocket || this.manualDisconnect) {
      this.setStatus(WebSocketStatus.DISCONNECTED);
      return;
    }

    if (this.authReconnectTimer) {
      return;
    }

    this.authReconnectAttempts += 1;
    this.setStatus(WebSocketStatus.RECONNECTING);

    const delay = Math.min(this.reconnectDelay * Math.max(1, this.authReconnectAttempts), 15000);
    console.log(`WebSocket鉴权未就绪，将在 ${delay}ms 后第 ${this.authReconnectAttempts} 次重试`);

    this.authReconnectTimer = setTimeout(() => {
      this.authReconnectTimer = null;
      this.connect().catch(error => {
        console.error('WebSocket鉴权补连失败:', error);
      });
    }, delay);
  }

  private clearAuthReconnectTimer(): void {
    if (this.authReconnectTimer) {
      clearTimeout(this.authReconnectTimer);
      this.authReconnectTimer = null;
    }
  }

  private resubscribeAll(): void {
    if (this.symbolSubscriptions.size > 0) {
      this.send({
        type: MessageType.SUBSCRIBE,
        timestamp: new Date().toISOString(),
        data: undefined,
        action: 'subscribe' as any,
        symbols: Array.from(this.symbolSubscriptions),
      } as WebSocketMessage & { action: string; symbols: string[] });
    }

    this.channelSubscriptions.forEach((channel) => {
      this.send({
        type: MessageType.SUBSCRIBE,
        timestamp: new Date().toISOString(),
        data: undefined,
        topic: channel,
      } as WebSocketMessage & { topic: string });
    });
  }
}

// 导出单例实例
export const websocketService = new WebSocketService();
export { WebSocketService };
