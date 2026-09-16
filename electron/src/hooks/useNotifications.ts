/**
 * 通知中心 Hook
 *
 * 管理通知列表、未读计数及已读标记状态
 * 支持实时推送（WebSocket）和降级轮询
 *
 * @author QuantMind Team
 * @date 2025-02-13
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { userService } from '../services/userService';
import { shouldUpdateByFingerprint } from '../utils/dataChange';
import { refreshOrchestrator } from '../services/refreshOrchestrator';
import {
  MessageType,
  websocketService,
  WebSocketStatus,
} from '../services/websocketService';
import type {
  BusinessNotification,
  UseNotificationsOptions,
  UseNotificationsReturn,
  NotificationRouteTarget,
} from '../types/notification';

const NOTIFICATION_ROUTE_MAP: Record<string, NotificationRouteTarget> = {
  '/backtest': 'backtest-history',
  '/strategy': 'strategy',
  '/trading': 'trading',
  '/community': 'community',
  '/profile': 'profile',
  '/user-center': 'profile',
  '/ai-ide': 'ai-ide',
};

const DEFAULT_POLLING_INTERVAL = 30000;
const MIN_POLLING_INTERVAL = 800;
const MAX_POLLING_INTERVAL = 10000;
const RECONNECT_REFRESH_DELAY = 500;

export const useNotifications = (options: UseNotificationsOptions = {}): UseNotificationsReturn => {
  const {
    limit = 20,
    days,
    autoRefresh = true,
    refreshInterval = DEFAULT_POLLING_INTERVAL,
  } = options;

  const [notifications, setNotifications] = useState<BusinessNotification[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState<boolean>(false);
  const [total, setTotal] = useState<number>(0);
  const [unreadCount, setUnreadCount] = useState<number>(0);
  const [typeCounts, setTypeCounts] = useState<Record<string, number>>({});
  const [hasMore, setHasMore] = useState<boolean>(false);
  const [loadingMore, setLoadingMore] = useState<boolean>(false);
  const [realtimeStatus, setRealtimeStatus] = useState<'connected' | 'fallback' | 'disabled'>('disabled');
  
  const initializedRef = useRef<boolean>(false);
  const fingerprintRef = useRef<string | null>(null);
  const notificationsRef = useRef<BusinessNotification[]>([]);
  const subscribedTopicRef = useRef<string | null>(null);
  const realtimeHandlerRef = useRef<((data: unknown) => void) | null>(null);
  const degradedRef = useRef<boolean>(false);
  const totalRef = useRef<number>(0);
  const unreadCountRef = useRef<number>(0);
  const typeCountsRef = useRef<Record<string, number>>({});
  const hasMoreRef = useRef<boolean>(false);
  const loadingMoreRef = useRef<boolean>(false);
  const prevRealtimeStatusRef = useRef<'connected' | 'fallback' | 'disabled'>('disabled');

  const getCurrentUserId = useCallback(() => {
    try {
      const raw = localStorage.getItem('user');
      if (!raw) return '';
      const parsed = JSON.parse(raw);
      return String(parsed?.id || parsed?.user_id || '');
    } catch {
      return '';
    }
  }, []);

  const applyNotificationsSnapshot = useCallback((
    nextNotifications: BusinessNotification[],
    nextDegraded: boolean,
    nextTotal?: number,
    nextHasMore?: boolean,
    nextTypeCounts?: Record<string, number>,
    nextUnreadCount?: number,
  ) => {
    const computedTotal = nextTotal ?? nextNotifications.length;
    const computedHasMore = nextHasMore ?? (nextNotifications.length < computedTotal);
    const computedTypeCounts = nextTypeCounts ?? typeCountsRef.current;
    const computedUnreadCount = nextUnreadCount ?? unreadCountRef.current;
    const snapshot = {
      notifications: nextNotifications,
      degraded: nextDegraded,
      total: computedTotal,
      hasMore: computedHasMore,
      typeCounts: computedTypeCounts,
      unreadCount: computedUnreadCount,
    };
    const { changed, fingerprint } = shouldUpdateByFingerprint(
      fingerprintRef.current,
      snapshot,
    );
    if (changed) {
      setNotifications(nextNotifications);
      notificationsRef.current = nextNotifications;
      setDegraded(nextDegraded);
      setTotal(computedTotal);
      setHasMore(computedHasMore);
      setTypeCounts(computedTypeCounts);
      setUnreadCount(computedUnreadCount);
      degradedRef.current = nextDegraded;
      totalRef.current = computedTotal;
      hasMoreRef.current = computedHasMore;
      typeCountsRef.current = computedTypeCounts;
      unreadCountRef.current = computedUnreadCount;
      fingerprintRef.current = fingerprint;
    }
  }, []);

  const fetchData = useCallback(async (params?: { silent?: boolean; append?: boolean }) => {
    const silent = params?.silent ?? true;
    const append = params?.append ?? false;

    try {
      if (!silent && !initializedRef.current && !append) {
        setLoading(true);
      }
      if (append) {
        setLoadingMore(true);
        loadingMoreRef.current = true;
      }

      const currentOffset = append ? notificationsRef.current.length : 0;

      const response = await userService.getNotifications({ limit, days, offset: currentOffset });

      if (response.success && response.data) {
        const incoming = response.data.items || [];
        const merged = append
          ? [
              ...notificationsRef.current,
              ...incoming.filter((item) => !notificationsRef.current.some((old) => old.id === item.id)),
            ]
          : incoming;
        const nextTotal = Number(response.data.total ?? merged.length);
        const nextHasMore = Boolean(response.data.has_more) || merged.length < nextTotal;
        const nextTypeCounts = response.data.type_counts || {};
        const nextUnreadCount = Number(response.data.unread_count ?? 0);
        
        applyNotificationsSnapshot(
          merged,
          Boolean(response.degraded),
          nextTotal,
          nextHasMore,
          nextTypeCounts,
          nextUnreadCount,
        );
        setError(null);
      } else {
        setError(response.error || '获取通知失败');
        applyNotificationsSnapshot(
          notificationsRef.current,
          true,
          totalRef.current,
          hasMoreRef.current,
          typeCountsRef.current,
          unreadCountRef.current,
        );
      }
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : '未知错误';
      setError(errorMessage);
      applyNotificationsSnapshot(
        notificationsRef.current,
        true,
        totalRef.current,
        hasMoreRef.current,
        typeCountsRef.current,
        unreadCountRef.current,
      );
    } finally {
      initializedRef.current = true;
      if (append) {
        setLoadingMore(false);
        loadingMoreRef.current = false;
      } else {
        setLoading(false);
      }
    }
  }, [applyNotificationsSnapshot, days, limit]);

  const refresh = useCallback(async () => {
    await fetchData({ silent: true, append: false });
  }, [fetchData]);

  const loadMore = useCallback(async () => {
    if (loadingMoreRef.current || !hasMoreRef.current) {
      return;
    }
    await fetchData({ silent: true, append: true });
  }, [fetchData]);

  const clearNotifications = useCallback(async () => {
    applyNotificationsSnapshot([], degradedRef.current, 0, false, {}, 0);
    try {
      const response = await userService.clearNotifications(days);
      if (!response.success) {
        throw new Error(response.error || '清空通知失败');
      }
      await refreshOrchestrator.requestRefresh('notifications', 'module-action', true);
    } catch (error) {
      console.error('清空通知失败，回滚并刷新', error);
      await fetchData({ silent: true, append: false });
    }
  }, [applyNotificationsSnapshot, days, fetchData]);

  const markAsRead = useCallback(async (id: number) => {
    const nextNotifications = notificationsRef.current.map(n => n.id === id ? { ...n, is_read: true } : n);
    notificationsRef.current = nextNotifications;
    setNotifications(nextNotifications);
    const nextUnreadCount = Math.max(0, unreadCountRef.current - 1);
    setUnreadCount(nextUnreadCount);
    unreadCountRef.current = nextUnreadCount;

    try {
      await userService.markNotificationRead(id);
      await refreshOrchestrator.requestRefresh('notifications', 'module-action', true);
    } catch (err) {
      console.error('标记已读失败，回滚状态', err);
      fetchData({ silent: true });
    }
  }, [fetchData]);

  const markAllAsRead = useCallback(async () => {
    const nextNotifications = notificationsRef.current.map(n => ({ ...n, is_read: true }));
    notificationsRef.current = nextNotifications;
    setNotifications(nextNotifications);
    setUnreadCount(0);
    unreadCountRef.current = 0;

    try {
      await userService.markAllNotificationsRead();
      await refreshOrchestrator.requestRefresh('notifications', 'module-action', true);
    } catch (err) {
      console.error('全部标记已读失败，回滚状态', err);
      fetchData({ silent: true });
    }
  }, [fetchData]);

  const connectRealtime = useCallback(async () => {
    const userId = getCurrentUserId();
    if (!userId) {
      setRealtimeStatus('disabled');
      return;
    }

    const topic = `notification.${userId}`;
    const status = websocketService.getStatus();
    if (status === WebSocketStatus.DISCONNECTED || status === WebSocketStatus.ERROR) {
      try {
        await websocketService.connect();
      } catch (error) {
        console.warn('通知实时连接失败，回退轮询', error);
        setRealtimeStatus('fallback');
        return;
      }
    }

    websocketService.subscribe({ channels: [topic] });
    subscribedTopicRef.current = topic;
    setRealtimeStatus(websocketService.getStatus() === WebSocketStatus.CONNECTED ? 'connected' : 'fallback');
  }, [getCurrentUserId]);

  const disconnectRealtime = useCallback(() => {
    if (subscribedTopicRef.current) {
      websocketService.unsubscribe([subscribedTopicRef.current]);
      subscribedTopicRef.current = null;
    }
    setRealtimeStatus('disabled');
  }, []);

  useEffect(() => {
    fetchData({ silent: false });
  }, []);

  useEffect(() => {
    if (!autoRefresh) {
      return;
    }

    const effectiveInterval = realtimeStatus === 'connected'
      ? Infinity
      : Math.min(Math.max(refreshInterval, MIN_POLLING_INTERVAL), MAX_POLLING_INTERVAL);

    if (effectiveInterval === Infinity) {
      return;
    }

    const unregister = refreshOrchestrator.register(
      'notifications',
      async () => {
        await fetchData({ silent: true });
      },
      { minIntervalMs: effectiveInterval },
    );

    return unregister;
  }, [autoRefresh, refreshInterval, fetchData, realtimeStatus]);

  useEffect(() => {
    if (
      prevRealtimeStatusRef.current !== 'connected' &&
      realtimeStatus === 'connected' &&
      initializedRef.current
    ) {
      setTimeout(() => {
        fetchData({ silent: true });
      }, RECONNECT_REFRESH_DELAY);
    }
    prevRealtimeStatusRef.current = realtimeStatus;
  }, [realtimeStatus, fetchData]);

  useEffect(() => {
    realtimeHandlerRef.current = (payload: unknown) => {
      const raw = payload as { id?: number; title?: string; content?: string; type?: string; level?: string; action_url?: string; created_at?: string; is_read?: boolean };
      const nextNotification: BusinessNotification = {
        id: Number(raw?.id ?? 0),
        title: String(raw?.title ?? '系统通知'),
        content: String(raw?.content ?? ''),
        action_url: raw?.action_url ? String(raw.action_url) : undefined,
        type: (['system', 'trading', 'market', 'strategy', 'health'].includes(String(raw?.type))
          ? String(raw?.type)
          : 'system') as BusinessNotification['type'],
        level: (['info', 'warning', 'error', 'success'].includes(String(raw?.level))
          ? String(raw?.level)
          : 'info') as BusinessNotification['level'],
        is_read: Boolean(raw?.is_read),
        created_at: String(raw?.created_at ?? new Date().toISOString()),
      };
      const merged = [
        nextNotification,
        ...notificationsRef.current.filter((item) => item.id !== nextNotification.id),
      ];
      const nextUnreadCount = nextNotification.is_read 
        ? unreadCountRef.current 
        : unreadCountRef.current + 1;
      applyNotificationsSnapshot(
        merged,
        degradedRef.current,
        Math.max(totalRef.current + 1, merged.length),
        hasMoreRef.current || merged.length < Math.max(totalRef.current + 1, merged.length),
        undefined,
        nextUnreadCount,
      );
    };

    const handler = (data: unknown) => realtimeHandlerRef.current?.(data);
    const statusHandler = (status: WebSocketStatus) => {
      if (status === WebSocketStatus.CONNECTED && subscribedTopicRef.current) {
        websocketService.subscribe({ channels: [subscribedTopicRef.current] });
        setRealtimeStatus('connected');
      } else if (
        status === WebSocketStatus.DISCONNECTED ||
        status === WebSocketStatus.ERROR ||
        status === WebSocketStatus.RECONNECTING
      ) {
        setRealtimeStatus('fallback');
      }
    };

    websocketService.addMessageHandler(MessageType.NOTIFICATION, handler);
    websocketService.addStatusHandler(statusHandler);
    connectRealtime().catch(() => setRealtimeStatus('fallback'));

    return () => {
      websocketService.removeMessageHandler(MessageType.NOTIFICATION, handler);
      websocketService.removeStatusHandler(statusHandler);
      disconnectRealtime();
    };
  }, [applyNotificationsSnapshot, connectRealtime, disconnectRealtime]);

  return {
    notifications,
    unreadCount,
    total,
    typeCounts,
    loadedCount: notifications.length,
    hasMore,
    loading,
    loadingMore,
    error,
    degraded,
    realtimeStatus,
    refresh,
    loadMore,
    clearNotifications,
    markAsRead,
    markAllAsRead,
    connectRealtime,
    disconnectRealtime,
  };
};

export const resolveNotificationTarget = (
  notification: { type: string; title: string; content?: string; action_url?: string }
): NotificationRouteTarget | null => {
  const rawUrl = String(notification.action_url || '').trim();

  if (rawUrl) {
    if (rawUrl.startsWith('http://') || rawUrl.startsWith('https://')) {
      return { external: rawUrl };
    }
    
    const mappedRoute = NOTIFICATION_ROUTE_MAP[rawUrl];
    if (mappedRoute) {
      return mappedRoute;
    }
    
    if (rawUrl.startsWith('/')) {
      return { route: rawUrl };
    }
  }

  const text = `${notification.title} ${notification.content || ''}`.toLowerCase();
  
  if (notification.type === 'strategy' || text.includes('回测') || text.includes('backtest')) {
    return 'backtest-history';
  }
  if (notification.type === 'trading' || text.includes('订单') || text.includes('成交') || text.includes('持仓') || text.includes('交易')) {
    return 'trading';
  }
  if (notification.type === 'market' || text.includes('市场') || text.includes('行情')) {
    return 'dashboard';
  }
  if (text.includes('策略')) {
    return 'strategy';
  }
  if (text.includes('社区')) {
    return 'community';
  }
  if (text.includes('账户') || text.includes('安全') || text.includes('登录')) {
    return 'profile';
  }
  
  return null;
};

export const getNavigationHint = (target: NotificationRouteTarget | null): string | undefined => {
  if (!target) return undefined;
  if (typeof target === 'object' && 'external' in target) return '跳转到外部链接';
  if (typeof target === 'object' && 'route' in target) return '跳转到相关页面';
  
  const hints: Record<string, string> = {
    'backtest-history': '跳转到回测历史',
    'dashboard': '跳转到仪表盘',
    'strategy': '跳转到策略页',
    'trading': '跳转到交易页',
    'community': '跳转到社区',
    'profile': '跳转到个人中心',
    'notifications': '跳转到通知中心',
    'ai-ide': '跳转到 AI-IDE',
  };
  
  return hints[target as string] ?? '跳转到相关页面';
};
