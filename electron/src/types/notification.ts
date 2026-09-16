/**
 * 通知类型统一定义
 * 
 * 区分两种通知类型：
 * 1. ToastNotification - 临时 Toast 提示（成功/错误/警告/信息）
 * 2. BusinessNotification - 持久化业务通知（系统/交易/市场/策略）
 */

// ==================== Toast 通知（临时提示） ====================

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface ToastNotification {
  id: string;
  type: ToastType;
  title: string;
  message: string;
  timestamp: number;
  duration?: number;
}

// ==================== 业务通知（持久化） ====================

export type BusinessNotificationType = 'system' | 'trading' | 'market' | 'strategy' | 'health';
export type BusinessNotificationLevel = 'info' | 'warning' | 'error' | 'success';

export interface BusinessNotification {
  id: number;
  user_id?: string;
  tenant_id?: string;
  title: string;
  content: string;
  action_url?: string;
  type: BusinessNotificationType;
  level: BusinessNotificationLevel;
  is_read: boolean;
  created_at: string;
  read_at?: string;
  expires_at?: string;
}

export interface NotificationListResponse {
  items: BusinessNotification[];
  total: number;
  unread_count: number;
  type_counts: Record<BusinessNotificationType, number>;
  has_more: boolean;
}

// ==================== 通知路由目标 ====================

export type NotificationRouteTarget =
  | 'backtest-history'
  | 'dashboard'
  | 'strategy'
  | 'trading'
  | 'community'
  | 'profile'
  | 'notifications'
  | 'ai-ide'
  | { route: string }
  | { external: string };

// ==================== WebSocket 消息类型 ====================

export interface NotificationWebSocketMessage {
  type: 'notification';
  data: BusinessNotification;
}

// ==================== Hook 返回类型 ====================

export interface UseNotificationsOptions {
  limit?: number;
  days?: number;
  autoRefresh?: boolean;
  refreshInterval?: number;
}

export interface UseNotificationsReturn {
  notifications: BusinessNotification[];
  unreadCount: number;
  total: number;
  typeCounts: Record<string, number>;
  loadedCount: number;
  hasMore: boolean;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  degraded: boolean;
  realtimeStatus: 'connected' | 'fallback' | 'disabled';
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
  clearNotifications: () => Promise<void>;
  markAsRead: (id: number) => Promise<void>;
  markAllAsRead: () => Promise<void>;
  connectRealtime: () => Promise<void>;
  disconnectRealtime: () => void;
}
