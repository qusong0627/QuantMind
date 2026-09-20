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

/**
 * 业务通知类型的**唯一事实源**（运行时常量 → 类型）。
 *
 * WS 实时推送要走白名单校验，白名单与类型联合此前是两份手写清单：漏加一个类型不会报错，
 * 只会把通知**静默降级成 `system`**（图标错、按类型分派的提醒通道不响）。
 * 这里由常量派生类型，加类型只改一处。
 */
export const BUSINESS_NOTIFICATION_TYPES = [
  'system',
  'trading',
  'market',
  'strategy',
  'health',
  // 持仓哨兵：分数跌破/盘中利空/名单新增（后端 publish_notification type="holding_alert"）
  'holding_alert',
] as const;

export type BusinessNotificationType = (typeof BUSINESS_NOTIFICATION_TYPES)[number];
export type BusinessNotificationLevel = 'info' | 'warning' | 'error' | 'success';

/** 运行时白名单校验（WS 推送入口用；非法值回退 `system`） */
export function normalizeNotificationType(value: unknown): BusinessNotificationType {
  const raw = String(value ?? '');
  return (BUSINESS_NOTIFICATION_TYPES as readonly string[]).includes(raw)
    ? (raw as BusinessNotificationType)
    : 'system';
}

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
