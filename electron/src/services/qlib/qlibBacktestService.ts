/**
 * Qlib专用回测服务
 * 包含完整的错误处理、重试逻辑和WebSocket管理
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import { SERVICE_URLS } from '../../config/services';
import {
  QlibBacktestConfig,
  QlibBacktestResult
} from '../../types/backtest/qlib';

/**
 * 错误码到中文提示的映射
 */
const ERROR_MESSAGES: Record<string, string> = {
  // 网络错误
  'NETWORK_ERROR': '无法连接到回测服务，请检查网络或服务状态',
  'TIMEOUT_ERROR': '请求超时，回测可能正在运行，请稍后查询结果',
  'REQUEST_ERROR': '请求配置错误，请联系技术支持',

  // 验证错误
  'VALIDATION_ERROR': '参数验证失败，请检查输入',
  'INVALID_DATE_RANGE': '日期范围无效，结束日期必须晚于开始日期',
  'INVALID_SYMBOL': '股票代码格式错误，请使用正确格式（如：600519.SH）',
  'INVALID_CAPITAL': '初始资金必须大于0',

  // 服务错误
  'QLIB_NOT_INITIALIZED': 'Qlib服务未初始化，请稍后重试或联系管理员',
  'STRATEGY_NOT_FOUND': '策略文件不存在或已被删除',
  'STRATEGY_COMPILE_ERROR': '策略代码编译失败，请检查代码语法',
  'DATA_NOT_AVAILABLE': '数据源不可用，请检查数据服务状态',
  'INSUFFICIENT_DATA': '指定日期范围内数据不足，请调整日期范围',

  // 回测执行错误
  'BACKTEST_FAILED': '回测执行失败，请检查策略逻辑',
  'BACKTEST_NOT_FOUND': '回测记录不存在或已被删除',
  'BACKTEST_TIMEOUT': '回测执行超时，请缩短日期范围或简化策略',

  // HTTP状态码
  'HTTP_400': '请求参数错误，请检查输入',
  'HTTP_401': '未授权，请先登录',
  'HTTP_403': '权限不足，无法访问该资源',
  'HTTP_404': '请求的资源不存在',
  'HTTP_429': '请求过于频繁，请稍后再试',
  'HTTP_500': '服务器内部错误，请稍后重试',
  'HTTP_502': '网关错误，服务暂时不可用',
  'HTTP_503': '服务暂时不可用，请稍后重试',
  'HTTP_504': '网关超时，请求处理时间过长',

  // WebSocket错误
  'WS_CONNECTION_ERROR': 'WebSocket连接失败',
  'WS_MESSAGE_ERROR': 'WebSocket消息格式错误',
  'WS_MAX_RETRY': 'WebSocket重连失败，已达到最大重试次数',
};

/**
 * 获取友好的错误提示
 */
function getFriendlyErrorMessage(code?: string, originalMessage?: string): string {
  if (code && ERROR_MESSAGES[code]) {
    return ERROR_MESSAGES[code];
  }
  return originalMessage || '未知错误，请稍后重试';
}

/**
 * 自定义错误类
 */
export class QlibBacktestError extends Error {
  public readonly friendlyMessage: string;

  constructor(
    message: string,
    public code?: string,
    public details?: any
  ) {
    super(message);
    this.name = 'QlibBacktestError';
    this.friendlyMessage = getFriendlyErrorMessage(code, message);
  }

  /**
   * 获取用户友好的错误信息
   */
  getUserMessage(): string {
    return this.friendlyMessage;
  }
}

/**
 * WebSocket连接管理器（优化版 - 指数退避重连）
 */
class WebSocketManager {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5; // 增加到5次
  private baseReconnectDelay = 1000; // 基础延迟1秒
  private maxReconnectDelay = 30000; // 最大延迟30秒
  private heartbeatInterval: NodeJS.Timeout | null = null;
  private reconnectTimeout: NodeJS.Timeout | null = null;
  private manualClose = false; // 标记是否手动关闭

  connect(
    url: string,
    callbacks: {
      onOpen?: () => void;
      onMessage?: (data: any) => void;
      onError?: (error: Error) => void;
      onClose?: () => void;
    }
  ): WebSocket {
    if (this.ws) {
      this.cleanup();
    }

    this.manualClose = false;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      console.log('[WS] ✅ Connected:', url);
      this.reconnectAttempts = 0; // 重置重连计数
      callbacks.onOpen?.();
      this.startHeartbeat();
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // 忽略心跳响应
        if (data.type !== 'pong') {
          callbacks.onMessage?.(data);
        }
      } catch (error) {
        console.error('[WS] ❌ Parse error:', error);
        callbacks.onError?.(new Error('WebSocket消息解析失败'));
      }
    };

    this.ws.onerror = (event) => {
      console.error('[WS] ❌ Connection error:', event);
      callbacks.onError?.(new Error('WebSocket连接错误'));
    };

    this.ws.onclose = (event) => {
      const isNormalClose = event.code === 1000 || event.code === 1005;
      console.log(`[WS] 🔌 Closed: code=${event.code}, reason=${event.reason || 'none'}, normal=${isNormalClose}`);

      this.stopHeartbeat();
      callbacks.onClose?.();

      // 自动重连（非正常关闭 + 非手动关闭 + 未超过最大重试次数）
      if (!isNormalClose && !this.manualClose && this.reconnectAttempts < this.maxReconnectAttempts) {
        this.reconnectAttempts++;

        // 指数退避：delay = min(baseDelay * 2^(attempts-1), maxDelay)
        const delay = Math.min(
          this.baseReconnectDelay * Math.pow(2, this.reconnectAttempts - 1),
          this.maxReconnectDelay
        );

        console.log(
          `[WS] 🔄 Reconnecting in ${delay}ms... (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`
        );

        this.reconnectTimeout = setTimeout(() => {
          console.log(`[WS] 🔄 Attempting reconnect #${this.reconnectAttempts}...`);
          this.connect(url, callbacks);
        }, delay);
      } else if (this.reconnectAttempts >= this.maxReconnectAttempts) {
        console.error('[WS] ❌ Max reconnection attempts reached. Please refresh the page.');
        callbacks.onError?.(new Error('WebSocket重连失败，已达到最大重试次数'));
      }
    };

    return this.ws;
  }

  private startHeartbeat() {
    this.heartbeatInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(JSON.stringify({ type: 'ping' }));
        } catch (error) {
          console.error('[WS] ❌ Heartbeat send failed:', error);
        }
      }
    }, 30000); // 每30秒发送心跳
  }

  private stopHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  cleanup() {
    this.manualClose = true; // 标记为手动关闭
    this.stopHeartbeat();

    // 清理重连定时器
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }

    if (this.ws) {
      try {
        this.ws.close(1000, 'Client cleanup');
      } catch (error) {
        console.error('[WS] ❌ Close error:', error);
      }
      this.ws = null;
    }

    // 重置重连计数
    this.reconnectAttempts = 0;
  }

  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  getReconnectAttempts(): number {
    return this.reconnectAttempts;
  }
}

class QlibBacktestService {
  private client: AxiosInstance;
  private baseUrl: string;
  private wsManagers: Map<string, WebSocketManager> = new Map();

  constructor() {
    // 使用统一配置的服务地址（重定向到 Strategy Service 8003）
    this.baseUrl = `${SERVICE_URLS.QLIB_SERVICE}/api/v1`;
    this.client = axios.create({
      baseURL: this.baseUrl,
      timeout: 180000,
      headers: { 'Content-Type': 'application/json' },
    });

    // 添加请求拦截器
    this.client.interceptors.request.use(
      (config) => {
        console.log(`[API] ${config.method?.toUpperCase()} ${config.url}`);
        return config;
      },
      (error) => Promise.reject(error)
    );

    // 添加响应拦截器
    this.client.interceptors.response.use(
      (response) => response,
      (error) => this.handleAxiosError(error)
    );
  }

  /**
   * 统一错误处理
   */
  private handleAxiosError(error: AxiosError): Promise<never> {
    if (error.response) {
      // 服务器返回错误状态码
      const { status, data } = error.response;
      let message = '回测服务请求失败';
      const code = `HTTP_${status}`;

      if (typeof data === 'object' && data !== null) {
        message = (data as any).detail || (data as any).message || message;
      }

      throw new QlibBacktestError(message, code, data);
    } else if (error.request) {
      // 请求已发送但没有收到响应
      throw new QlibBacktestError(
        '回测服务无响应，请检查网络连接',
        'NETWORK_ERROR'
      );
    } else {
      // 请求配置错误
      throw new QlibBacktestError(
        error.message || '请求配置错误',
        'REQUEST_ERROR'
      );
    }
  }

  /**
   * 带重试的请求方法
   */
  private async requestWithRetry<T>(
    requestFn: () => Promise<T>,
    maxRetries = 2,
    retryDelay = 1000
  ): Promise<T> {
    let lastError: Error | null = null;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        return await requestFn();
      } catch (error) {
        lastError = error as Error;

        // 最后一次尝试失败，直接抛出错误
        if (attempt === maxRetries) {
          break;
        }

        // 仅对网络错误或5xx错误重试
        const shouldRetry =
          error instanceof QlibBacktestError &&
          (error.code === 'NETWORK_ERROR' || error.code?.startsWith('HTTP_5'));

        if (!shouldRetry) {
          break;
        }

        console.log(`[Retry] Attempt ${attempt + 1}/${maxRetries} failed, retrying...`);
        await new Promise(resolve => setTimeout(resolve, retryDelay * (attempt + 1)));
      }
    }

    throw lastError;
  }

  /**
   * 运行Qlib回测
   * @param config 回测配置
   * @returns 回测结果（包含backtest_id）
   * @throws {QlibBacktestError} 当配置无效或请求失败时
   */
  async runBacktest(config: QlibBacktestConfig): Promise<QlibBacktestResult> {
    // 验证配置
    this.validateConfig(config);

    return this.requestWithRetry(async () => {
      // 直接使用原始策略类型，不做强制降级
      // 后端支持 TopkDropout / WeightStrategy / CustomStrategy
      const strategyType = config.qlib_strategy_type || 'TopkDropout';

      // 处理股票池：如果symbol包含逗号，说明是多只股票
      let universe = 'csi300'; // 默认使用沪深300
      if (config.symbol) {
        // 如果指定了股票，使用自定义列表
        // 前端传入格式：000001.SZ,600519.SH
        // Qlib需要格式：SZ000001 SH600519（空格分隔，市场代码在前）
        const symbols = config.symbol.split(',').map(s => s.trim()).filter(s => s);
        if (symbols.length > 0) {
          // 转换格式：000001.SZ -> SZ000001
          universe = symbols.map(sym => {
            const parts = sym.split('.');
            if (parts.length === 2) {
              return `${parts[1]}${parts[0]}`; // 市场代码+股票代码
            }
            return sym;
          }).join(' '); // 空格分隔
        }
      }

      const payload: Record<string, any> = {
        strategy_type: strategyType,
        strategy_params: {
          ...config.qlib_strategy_params,
          signal: config.qlib_strategy_params?.signal || '<PRED>'
        },
        start_date: config.start_date,
        end_date: config.end_date,
        initial_capital: config.initial_capital,
        benchmark: config.benchmark_symbol || 'SH000300',
        universe: universe,
        open_cost: config.qlib_strategy_params?.buy_cost || config.commission || 0.00025,
        close_cost: config.qlib_strategy_params?.sell_cost || (config.commission ? config.commission * 3 : 0.00075),
        user_id: config.user_id,
        tenant_id: config.tenant_id || 'default',
        seed: config.seed,
        use_vectorized: config.use_vectorized ?? false
      };

      if (config.signal_lag_days != null) {
        payload.signal_lag_days = config.signal_lag_days;
      }
      if (typeof config.allow_feature_signal_fallback === 'boolean') {
        payload.allow_feature_signal_fallback = config.allow_feature_signal_fallback;
      }

      // CustomStrategy 必须携带 strategy_content
      if (config.strategy_content?.trim()) {
        payload.strategy_content = config.strategy_content;
      } else if (strategyType === 'CustomStrategy') {
        throw new QlibBacktestError(
          'CustomStrategy 需要提供策略代码（strategy_content）',
          'VALIDATION_ERROR'
        );
      }

      const response = await this.client.post<QlibBacktestResult>('/qlib/backtest', payload);

      return response.data;
    });
  }

  /**
   * 验证回测配置
   */
  private validateConfig(config: QlibBacktestConfig): void {
    const errors: string[] = [];

    if (!config.start_date) {
      errors.push('开始日期不能为空');
    }

    if (!config.end_date) {
      errors.push('结束日期不能为空');
    }

    if (config.start_date && config.end_date && config.start_date >= config.end_date) {
      errors.push('结束日期必须晚于开始日期');
    }

    if (!config.initial_capital || config.initial_capital <= 0) {
      errors.push('初始资金必须大于0');
    }

    if (!config.user_id?.trim()) {
      errors.push('用户ID不能为空');
    }

    if (errors.length > 0) {
      throw new QlibBacktestError(
        `配置验证失败: ${errors.join('; ')}`,
        'VALIDATION_ERROR',
        { errors }
      );
    }
  }

  /**
   * 获取回测结果
   * @param backtestId 回测ID
   * @returns 回测结果详情
   */
  async getResult(backtestId: string): Promise<QlibBacktestResult> {
    if (!backtestId?.trim()) {
      throw new QlibBacktestError('回测ID不能为空', 'VALIDATION_ERROR');
    }

    return this.requestWithRetry(async () => {
      const response = await this.client.get<QlibBacktestResult>(
        `/qlib/results/${backtestId}`
      );
      return response.data;
    });
  }

  /**
   * 获取回测历史
   * @param userId 用户ID
   * @param filters 过滤条件
   * @returns 回测历史列表
   */
  async getHistory(userId: string, filters?: {
    status?: string;
    page?: number;
    page_size?: number;
  }): Promise<QlibBacktestResult[]> {
    return this.requestWithRetry(async () => {
      if (!userId?.trim()) {
        throw new QlibBacktestError('用户ID不能为空', 'VALIDATION_ERROR');
      }

      const params = new URLSearchParams();
      if (filters?.status) params.append('status', filters.status);
      if (filters?.page) params.append('page', filters.page.toString());
      if (filters?.page_size) params.append('page_size', filters.page_size.toString());

      const query = params.toString();
      const response = await this.client.get<QlibBacktestResult[]>(
        `/qlib/history/${userId}${query ? `?${query}` : ''}`
      );
      return response.data || [];
    });
  }

  /**
   * 删除回测
   * @param backtestId 回测ID
   */
  async deleteBacktest(backtestId: string, userId: string): Promise<void> {
    if (!backtestId?.trim()) {
      throw new QlibBacktestError('回测ID不能为空', 'VALIDATION_ERROR');
    }
    if (!userId?.trim()) {
      throw new QlibBacktestError('用户ID不能为空', 'VALIDATION_ERROR');
    }

    await this.requestWithRetry(async () => {
      await this.client.delete(`/qlib/results/${backtestId}`, {
        params: { user_id: userId }
      });
    });
  }

  /**
   * 对比两个回测
   * @param id1 回测ID 1
   * @param id2 回测ID 2
   * @returns 对比结果
   */
  async compareBacktests(id1: string, id2: string, userId: string): Promise<any> {
    if (!id1?.trim() || !id2?.trim()) {
      throw new QlibBacktestError('回测ID不能为空', 'VALIDATION_ERROR');
    }
    if (!userId?.trim()) {
      throw new QlibBacktestError('用户ID不能为空', 'VALIDATION_ERROR');
    }

    return this.requestWithRetry(async () => {
      const response = await this.client.get(`/qlib/compare/${id1}/${id2}`, {
        params: { user_id: userId }
      });
      return response.data;
    });
  }

  /**
   * 运行参数优化
   * @param config 优化配置 (符合 QlibOptimizationRequest 结构)
   * @returns 优化结果
   */
  async optimizeParameters(config: any): Promise<any> {
    return this.requestWithRetry(async () => {
      // 如果配置中包含 genetic 算法特有参数，则调用遗传算法接口
      if (config.generations || config.population_size) {
        // 构造 QlibGeneticOptimizationRequest
        const geneticRequest = {
          base_request: {
            strategy_type: config.qlib_strategy_type,
            strategy_params: { ...config.qlib_strategy_params,
              ...config.qlib_strategy_params,
              signal: '<PRED>'
            },
            start_date: config.start_date,
            end_date: config.end_date,
            initial_capital: config.initial_capital,
            benchmark: 'SH000300',
            universe: 'csi300', // 简化处理，实际应解析 config.symbol
            open_cost: config.qlib_strategy_params?.buy_cost || 0.00026,
            close_cost: config.qlib_strategy_params?.sell_cost || 0.00076,
            user_id: config.user_id
          },
          param_ranges: config.param_ranges,
          optimization_target: config.optimization_target,
          population_size: config.population_size,
          generations: config.generations,
          mutation_rate: 0.1,
          max_parallel: 5
        };
        const response = await this.client.post('/qlib/optimize/genetic', geneticRequest);
        return response.data;
      }

      // 否则默认使用网格搜索
      const response = await this.client.post('/qlib/optimize', config);
      return response.data;
    });
  }

  /**
   * 创建WebSocket连接监听回测进度
   * @param backtestId 回测ID
   * @param callbacks 回调函数
   * @returns WebSocket实例
   */
  connectProgress(
    backtestId: string,
    callbacks: {
      onProgress?: (progress: number, message?: string) => void;
      onComplete?: (result: QlibBacktestResult) => void;
      onError?: (error: Error) => void;
      onOpen?: () => void;
    }
  ): WebSocket {
    if (!backtestId?.trim()) {
      throw new QlibBacktestError('回测ID不能为空', 'VALIDATION_ERROR');
    }

    // 清理旧连接
    if (this.wsManagers.has(backtestId)) {
      this.wsManagers.get(backtestId)?.cleanup();
      this.wsManagers.delete(backtestId);
    }

    // 构建WebSocket URL
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsBaseUrl = SERVICE_URLS.QLIB_SERVICE.replace(/^http(s)?:\/\//, '');
    const wsUrl = `${wsProtocol}//${wsBaseUrl}/api/v1/ws/backtest/${backtestId}`;

    console.log('[QlibBacktest] Connecting to:', wsUrl);

    const manager = new WebSocketManager();
    this.wsManagers.set(backtestId, manager);

    const ws = manager.connect(wsUrl, {
      onOpen: () => {
        console.log('[QlibBacktest] WebSocket connected');
        callbacks.onOpen?.();
      },

      onMessage: (data) => {
        console.log('[QlibBacktest] Message:', data);

        switch (data.status) {
          case 'completed':
            // 获取完整结果
            this.getResult(backtestId)
              .then(result => {
                callbacks.onComplete?.(result);
                manager.cleanup();
                this.wsManagers.delete(backtestId);
              })
              .catch(error => {
                callbacks.onError?.(error);
                manager.cleanup();
                this.wsManagers.delete(backtestId);
              });
            break;

          case 'failed':
            const error = new QlibBacktestError(
              data.error_message || '回测失败',
              'BACKTEST_FAILED',
              data
            );
            callbacks.onError?.(error);
            manager.cleanup();
            this.wsManagers.delete(backtestId);
            break;

          case 'running':
          case 'progress':
            if (data.progress !== undefined) {
              callbacks.onProgress?.(data.progress, data.message);
            }
            break;

          default:
            console.warn('[QlibBacktest] Unknown status:', data.status);
        }
      },

      onError: (error) => {
        console.error('[QlibBacktest] WebSocket error:', error);
        callbacks.onError?.(error);
      },

      onClose: () => {
        console.log('[QlibBacktest] WebSocket closed');
        manager.cleanup();
        this.wsManagers.delete(backtestId);
      }
    });

    return ws;
  }

  /**
   * 轮询回测状态（用于无WS场景）
   */
  pollStatus(
    backtestId: string,
    callbacks: {
      onProgress?: (progress: number, message?: string) => void;
      onComplete?: (result: QlibBacktestResult) => void;
      onError?: (error: Error) => void;
    },
    intervalMs = 2000
  ): () => void {
    let cancelled = false;
    const poll = async () => {
      if (cancelled) return;
      try {
        const response = await this.client.get<any>(`/qlib/backtest/${backtestId}/status`);
        const status = response.data?.status;
        const progress = typeof response.data?.progress === 'number' ? response.data.progress : 0;
        callbacks.onProgress?.(progress, status);
        if (status === 'completed') {
          const result = await this.getResult(backtestId);
          callbacks.onComplete?.(result);
          return;
        }
        if (status === 'failed') {
          callbacks.onError?.(new QlibBacktestError('回测失败', 'BACKTEST_FAILED', response.data));
          return;
        }
        setTimeout(poll, intervalMs);
      } catch (error: any) {
        callbacks.onError?.(error);
      }
    };

    poll();
    return () => {
      cancelled = true;
    };
  }

  /**
   * 断开特定回测的WebSocket连接
   * @param backtestId 回测ID
   */
  disconnectProgress(backtestId: string): void {
    const manager = this.wsManagers.get(backtestId);
    if (manager) {
      manager.cleanup();
      this.wsManagers.delete(backtestId);
    }
  }

  /**
   * 断开所有WebSocket连接
   */
  disconnectAll(): void {
    this.wsManagers.forEach(manager => manager.cleanup());
    this.wsManagers.clear();
  }

  /**
   * 检查WebSocket连接状态
   * @param backtestId 回测ID
   * @returns 是否已连接
   */
  isProgressConnected(backtestId: string): boolean {
    return this.wsManagers.get(backtestId)?.isConnected() ?? false;
  }
}

// 单例导出
export const qlibBacktestService = new QlibBacktestService();
export default qlibBacktestService;
