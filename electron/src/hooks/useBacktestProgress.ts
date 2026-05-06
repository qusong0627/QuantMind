/**
 * WebSocket 实时回测进度 Hook
 *
 * 用于监听后端回测进度推送，实时更新前端显示
 *
 * @example
 * ```typescript
 * const { progress, message, result, error, isConnected } = useBacktestProgress(backtestId);
 * ```
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { SERVICE_URLS } from '../config/services';

export interface BacktestProgressData {
  type: 'connected' | 'progress' | 'result' | 'error';
  backtest_id?: string;
  progress: number;
  message: string;
  data?: unknown;
  timestamp?: number;
}

export interface UseBacktestProgressReturn {
  /** 当前进度 (0-1) */
  progress: number;
  /** 进度描述消息 */
  message: string;
  /** 回测结果（完成时） */
  result: any | null;
  /** 错误信息 */
  error: string | null;
  /** WebSocket 是否已连接 */
  isConnected: boolean;
  /** 手动重连 */
  reconnect: () => void;
  /** 手动断开 */
  disconnect: () => void;
}

/**
 * 使用 WebSocket 监听回测进度
 *
 * @param backtestId 回测任务 ID
 * @param enabled 是否启用（默认 true）
 * @param autoReconnect 是否自动重连（默认 true）
 * @param maxReconnectAttempts 最大重连次数（默认 5）
 * @returns 进度状态和控制方法
 */
export function useBacktestProgress(
  backtestId: string | null,
  enabled: boolean = true,
  autoReconnect: boolean = true,
  maxReconnectAttempts: number = 5
): UseBacktestProgressReturn {
  const [progress, setProgress] = useState(0);
  const [message, setMessage] = useState('准备开始...');
  const [result, setResult] = useState<any | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCountRef = useRef(0);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const isManualDisconnectRef = useRef(false);

  // 清理重连定时器
  const clearReconnectTimeout = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
  }, []);

  // 连接 WebSocket
  const connect = useCallback(() => {
    if (!backtestId || !enabled || isManualDisconnectRef.current) {
      return;
    }

    // 如果已连接，先断开
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    try {
      // 从环境变量或配置获取 WebSocket URL
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsHost = SERVICE_URLS.ENGINE_SERVICE.replace(/^http(s)?:\/\//, '');
      const wsUrl = `${wsProtocol}//${wsHost}/ws/backtest/${backtestId}`;

      console.log(`🔗 连接 WebSocket: ${wsUrl}`);
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        console.log('✅ WebSocket 已连接');
        setIsConnected(true);
        setError(null);
        reconnectCountRef.current = 0; // 重置重连计数
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as Partial<BacktestProgressData> & {
            status?: string;
            progress?: number;
            error_message?: string;
          };
          console.log('📨 收到进度更新:', data);

          const status = data.status;
          const pct = typeof data.progress === 'number' ? data.progress : data.progress === 0 ? 0 : undefined;

          if (status === 'completed') {
            setProgress(1.0);
            setMessage('回测完成');
            setResult(data.data || data);
            ws.close();
          } else if (status === 'failed') {
            const msg = data.message || data.error_message || '回测失败';
            setError(msg);
            setMessage(`错误: ${msg}`);
            ws.close();
          } else if (pct !== undefined) {
            // 后端 progress 为 0-1
            setProgress(pct);
            setMessage(data.message || `进度: ${(pct * 100).toFixed(1)}%`);
          } else if (data.type === 'connected') {
            setMessage(data.message || '已连接');
          }
        } catch (err) {
          console.error('解析 WebSocket 消息失败:', err);
        }
      };

      ws.onerror = (event) => {
        console.error('❌ WebSocket 错误:', event);
        setError('WebSocket 连接错误');
        setIsConnected(false);
      };

      ws.onclose = (event) => {
        console.log(`🔌 WebSocket 已断开 (code: ${event.code})`);
        setIsConnected(false);
        wsRef.current = null;

        // 自动重连逻辑
        if (
          !isManualDisconnectRef.current &&
          autoReconnect &&
          reconnectCountRef.current < maxReconnectAttempts &&
          event.code !== 1000 // 1000 = 正常关闭
        ) {
          reconnectCountRef.current++;
          const delay = Math.min(1000 * Math.pow(2, reconnectCountRef.current - 1), 10000);

          console.log(`🔄 将在 ${delay}ms 后重连 (${reconnectCountRef.current}/${maxReconnectAttempts})`);
          setMessage(`连接断开，${delay / 1000}秒后重连...`);

          clearReconnectTimeout();
          reconnectTimeoutRef.current = setTimeout(() => {
            connect();
          }, delay);
        } else if (reconnectCountRef.current >= maxReconnectAttempts) {
          setError(`重连失败，已达最大尝试次数 (${maxReconnectAttempts})`);
          setMessage('连接失败');
        }
      };
    } catch (err) {
      console.error('创建 WebSocket 连接失败:', err);
      setError('无法建立 WebSocket 连接');
      setIsConnected(false);
    }
  }, [backtestId, enabled, autoReconnect, maxReconnectAttempts, clearReconnectTimeout]);

  // 手动断开
  const disconnect = useCallback(() => {
    isManualDisconnectRef.current = true;
    clearReconnectTimeout();

    if (wsRef.current) {
      wsRef.current.close(1000, 'Manual disconnect'); // 1000 = 正常关闭
      wsRef.current = null;
    }

    setIsConnected(false);
  }, [clearReconnectTimeout]);

  // 手动重连
  const reconnect = useCallback(() => {
    isManualDisconnectRef.current = false;
    reconnectCountRef.current = 0;
    disconnect();

    // 延迟一下再连接，确保断开完成
    setTimeout(() => {
      connect();
    }, 100);
  }, [connect, disconnect]);

  // 初始连接和清理
  useEffect(() => {
    if (backtestId && enabled) {
      isManualDisconnectRef.current = false;
      connect();
    }

    return () => {
      clearReconnectTimeout();
      if (wsRef.current) {
        wsRef.current.close(1000, 'Component unmounted');
        wsRef.current = null;
      }
    };
  }, [backtestId, enabled, connect, clearReconnectTimeout]);

  return {
    progress,
    message,
    result,
    error,
    isConnected,
    reconnect,
    disconnect,
  };
}
