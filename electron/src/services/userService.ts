import { apiClient } from './api-client';
import { API_ENDPOINTS } from './config';
import { portfolioService } from './portfolioService';
import type {
  BusinessNotification,
  NotificationListResponse,
} from '../types/notification';

// 定义后端响应结构
interface BackendResponse<T> {
  code: number;
  message: string;
  data: T;
}

// 定义服务层返回结构 (保持与原有 consumers 兼容)
export interface ServiceResponse<T = unknown> {
  success: boolean;
  data?: T;
  error?: string;
  degraded?: boolean;
  timestamp: string;
}

// 别名以兼容旧代码引用
export type ApiResponse<T> = ServiceResponse<T>;

// 用户数据接口定义
export interface UserProfile {
  id: string;
  username: string;
  email?: string;
  avatar?: string;
  role: string;
  createdAt: string;
  lastLoginAt?: string;
}

export interface UserPreferences {
  theme: 'light' | 'dark';
  language: 'zh-CN' | 'en-US';
  refreshInterval: number;
  notifications: {
    email: boolean;
    push: boolean;
    sound: boolean;
  };
  dashboard: {
    layout: string;
    widgets: string[];
  };
}

export interface FundData {
  totalAsset: number;
  availableBalance: number;
  frozenBalance: number;
  todayPnL: number;
  dailyReturn?: number;
  totalPnL: number;
  totalReturn: number;
  initialCapital: number;
  initialCapitalEstimated?: boolean;
  winRate: number;
  maxDrawdown: number;
  sharpeRatio: number;
  monthlyPnL?: number;  // 本月盈亏
  initialCapitalAvailable?: boolean;
  todayPnLAvailable?: boolean;
  dailyReturnAvailable?: boolean;
  totalPnLAvailable?: boolean;
  totalReturnAvailable?: boolean;
  monthlyPnLAvailable?: boolean;
  metricsSource?: string;
  metricsMeta?: Record<string, unknown>;
  returnRate?: number;  // 收益率
  accountOnline?: boolean; // 模拟账户是否在线上报
  lastUpdate: string;
}

export type UserNotification = BusinessNotification;

class UserService {
  // 获取用户资料
  async getUserProfile(): Promise<ApiResponse<UserProfile>> {
    try {
      const res = await apiClient.get<BackendResponse<UserProfile>>(API_ENDPOINTS.USER_PROFILE);
      if (res.code === 200) {
        return {
          success: true,
          data: res.data,
          timestamp: new Date().toISOString()
        };
      }
      return {
        success: false,
        error: res.message || '获取用户资料失败',
        timestamp: new Date().toISOString()
      };
    } catch (error) {
      console.error('获取用户资料失败:', error);
      return {
        success: false,
        error: '获取用户资料失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 更新用户资料
  async updateUserProfile(data: Partial<UserProfile>): Promise<ApiResponse<UserProfile>> {
    try {
      const res = await apiClient.put<BackendResponse<UserProfile>>(`${API_ENDPOINTS.USER_PROFILE}/profile`, data as Record<string, unknown>);
      if (res.code === 200) {
        return {
          success: true,
          data: res.data,
          timestamp: new Date().toISOString()
        };
      }
      return {
        success: false,
        error: res.message || '更新用户资料失败',
        timestamp: new Date().toISOString()
      };
    } catch (error) {
      console.error('更新用户资料失败:', error);
      return {
        success: false,
        error: '更新用户资料失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 获取用户偏好设置
  async getUserPreferences(userId: string): Promise<ApiResponse<UserPreferences>> {
    try {
      const res = await apiClient.get<BackendResponse<UserPreferences>>(`${API_ENDPOINTS.USER_PREFERENCES}/${userId}`);
      if (res.code === 200) {
        return {
          success: true,
          data: res.data,
          timestamp: new Date().toISOString()
        };
      }
      throw new Error(res.message);
    } catch (error) {
      // 如果获取失败，返回默认配置
      console.warn('获取用户偏好失败，使用默认配置:', error);
      return {
        success: true,
        data: this.getDefaultPreferences(),
        timestamp: new Date().toISOString()
      };
    }
  }

  // 更新用户偏好设置
  async updateUserPreferences(userId: string, preferences: Partial<UserPreferences>): Promise<ApiResponse<UserPreferences>> {
    try {
      const res = await apiClient.put<BackendResponse<UserPreferences>>(`${API_ENDPOINTS.USER_PREFERENCES}/${userId}`, preferences as Record<string, unknown>);
      if (res.code === 200) {
        return {
          success: true,
          data: res.data,
          timestamp: new Date().toISOString()
        };
      }
      return {
        success: false,
        error: res.message,
        timestamp: new Date().toISOString()
      };
    } catch (error) {
      console.error('更新用户偏好失败:', error);
      return {
        success: false,
        error: '更新用户偏好失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // --- 安全设置 ---

  // 发送手机验证码
  async sendPhoneVerifyCode(purpose: 'bind_phone' | 'change_phone_old' | 'change_phone_new', phone?: string): Promise<ApiResponse<void>> {
    try {
      const res = await apiClient.post<BackendResponse<void>>(API_ENDPOINTS.PHONE_SEND_CODE, { purpose, phone });
      if (res.code === 200) {
        return { success: true, timestamp: new Date().toISOString() };
      }
      throw new Error(res.message);
    } catch (error) {
      return {
        success: false,
        error: (error as any).message || '发送验证码失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 绑定手机号
  async bindPhone(phone: string, code: string): Promise<ApiResponse<void>> {
    try {
      const res = await apiClient.post<BackendResponse<void>>(API_ENDPOINTS.PHONE_BIND, { phone, code });
      if (res.code === 200) {
        return { success: true, timestamp: new Date().toISOString() };
      }
      throw new Error(res.message);
    } catch (error) {
      return {
        success: false,
        error: (error as any).message || '绑定手机号失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 更换手机号
  async changePhone(oldCode: string, newPhone: string, newCode: string): Promise<ApiResponse<void>> {
    try {
      const res = await apiClient.post<BackendResponse<void>>(API_ENDPOINTS.PHONE_CHANGE, { old_code: oldCode, new_phone: newPhone, new_code: newCode });
      if (res.code === 200) {
        return { success: true, timestamp: new Date().toISOString() };
      }
      throw new Error(res.message);
    } catch (error) {
      return {
        success: false,
        error: (error as any).message || '更换手机号失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // --- 设备管理 ---

  async getDevices(): Promise<ApiResponse<any[]>> {
    try {
      const res = await apiClient.get<BackendResponse<any[]>>(API_ENDPOINTS.DEVICES);
      if (res.code === 200) {
        return { success: true, data: res.data, timestamp: new Date().toISOString() };
      }
      return { success: false, error: res.message, timestamp: new Date().toISOString() };
    } catch (error) {
      return {
        success: false,
        error: '获取设备列表失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  async revokeDevice(deviceId: string): Promise<ApiResponse<void>> {
    try {
      const res = await apiClient.delete<BackendResponse<void>>(`${API_ENDPOINTS.DEVICES}/${deviceId}`);
      if (res.code === 200) {
        return { success: true, timestamp: new Date().toISOString() };
      }
      return { success: false, error: res.message, timestamp: new Date().toISOString() };
    } catch (error) {
      return {
        success: false,
        error: '移除设备失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // --- 审计日志 ---

  async getAuditLogs(params: { page: number; pageSize: number }): Promise<ApiResponse<{ logs: any[]; total: number }>> {
    try {
      // 手动构建查询参数
      const queryParams = {
        limit: params.pageSize,
        offset: (params.page - 1) * params.pageSize
      };

      const res = await apiClient.get<BackendResponse<{ logs: any[]; total: number }>>(API_ENDPOINTS.AUDIT_LOGS, queryParams);

      if (res.code === 200) {
        return {
          success: true,
          data: {
            logs: res.data.logs || [],
            total: res.data.total || 0
          },
          timestamp: new Date().toISOString()
        };
      }
      return { success: false, error: res.message, timestamp: new Date().toISOString() };
    } catch (error) {
      return {
        success: false,
        error: '获取审计日志失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 获取资金数据（通过 portfolioService 获取）
  async getFundData(): Promise<ApiResponse<FundData>> {
    try {
      const { data } = await portfolioService.getFundOverview('default_user');
      return {
        success: true,
        data,
        timestamp: new Date().toISOString()
      };
    } catch (error) {
      console.error('获取资金数据失败:', error);
      // 降级：返回默认模拟账户数据
      return {
        success: true,
        data: portfolioService.getDefaultFundData(),
        timestamp: new Date().toISOString()
      };
    }
  }

  // 获取默认用户偏好
  getDefaultPreferences(): UserPreferences {
    return {
      theme: 'light',
      language: 'zh-CN',
      refreshInterval: 10000, // 10秒
      notifications: {
        email: true,
        push: true,
        sound: false
      },
      dashboard: {
        layout: 'default',
        widgets: ['market', 'fund', 'trades', 'strategies', 'charts', 'alerts']
      }
    };
  }

  // --- 通知中心 ---

  // 获取通知列表
  async getNotifications(params?: { is_read?: boolean; limit?: number; offset?: number; days?: number }): Promise<ApiResponse<NotificationListResponse>> {
    try {
      const queryParams: Record<string, unknown> = {};
      if (params?.limit) queryParams['limit'] = params.limit;
      if (params?.offset) queryParams['offset'] = params.offset;
      if (params?.days) queryParams['days'] = params.days;
      if (params?.is_read !== undefined) queryParams['is_read'] = params.is_read;

      const raw = await apiClient.get<any>(API_ENDPOINTS.NOTIFICATIONS, queryParams);
      const ok = raw?.code === 200 && raw?.data && Array.isArray(raw?.data?.items);
      if (!ok) {
        throw new Error(raw?.message || '获取通知列表失败');
      }
      const rawList: any[] = raw.data.items;
      const list: UserNotification[] = rawList.map((item) => ({
        id: Number(item?.id ?? 0),
        user_id: item?.user_id ? String(item.user_id) : undefined,
        tenant_id: item?.tenant_id ? String(item.tenant_id) : undefined,
        title: String(item?.title ?? ''),
        content: String(item?.content ?? ''),
        action_url: item?.action_url ? String(item.action_url) : undefined,
        type: (['system', 'trading', 'market', 'strategy', 'health'].includes(String(item?.type))
          ? String(item.type)
          : 'system') as UserNotification['type'],
        level: (['info', 'warning', 'error', 'success'].includes(String(item?.level))
          ? String(item.level)
          : 'info') as UserNotification['level'],
        is_read: Boolean(item?.is_read),
        created_at: String(item?.created_at ?? new Date().toISOString()),
        read_at: item?.read_at ? String(item.read_at) : undefined,
        expires_at: item?.expires_at ? String(item.expires_at) : undefined,
      }));

      return {
        success: true,
        data: {
          items: list,
          total: Number(raw?.data?.total ?? list.length),
          unread_count: Number(raw?.data?.unread_count ?? list.filter((item) => !item.is_read).length),
          type_counts: raw?.data?.type_counts || {},
          has_more: Boolean(raw?.data?.has_more),
        },
        timestamp: new Date().toISOString()
      };
    } catch (error) {
      console.error('获取通知列表失败:', error);
      return {
        success: false,
        error: error instanceof Error ? error.message : '获取通知列表失败',
        degraded: true,
        timestamp: new Date().toISOString()
      };
    }
  }

  // 标记单个已读
  async markNotificationRead(id: number): Promise<ApiResponse<void>> {
    try {
      const res = await apiClient.post<BackendResponse<void>>(API_ENDPOINTS.NOTIFICATION_READ(id));
      if (res.code === 200) {
        return {
          success: true,
          timestamp: new Date().toISOString()
        };
      }
      throw new Error(res.message);
    } catch (error) {
      return {
        success: false,
        error: '标记已读失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 标记全部已读
  async markAllNotificationsRead(): Promise<ApiResponse<{ count: number }>> {
    try {
      const res = await apiClient.post<BackendResponse<{ count: number }>>(API_ENDPOINTS.NOTIFICATIONS_READ_ALL);
      if (res.code === 200) {
        return {
          success: true,
          data: res.data,
          timestamp: new Date().toISOString()
        };
      }
      throw new Error(res.message);
    } catch (error) {
      return {
        success: false,
        error: '全部标记已读失败',
        timestamp: new Date().toISOString()
      };
    }
  }

  // 清空通知（支持按天数窗口）
  async clearNotifications(days?: number): Promise<ApiResponse<{ count: number }>> {
    try {
      const payload: Record<string, unknown> = {};
      if (typeof days === 'number') {
        payload.days = days;
      }
      const res = await apiClient.post<BackendResponse<{ count: number }>>(API_ENDPOINTS.NOTIFICATIONS_CLEAR, payload);
      if (res.code === 200) {
        return {
          success: true,
          data: res.data,
          timestamp: new Date().toISOString()
        };
      }
      throw new Error(res.message || '清空通知失败');
    } catch (error) {
      return {
        success: false,
        error: (error as any)?.message || '清空通知失败',
        timestamp: new Date().toISOString()
      };
    }
  }
}

export const userService = new UserService();
