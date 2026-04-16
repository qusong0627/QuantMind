/**
 * 认证相关类型定义
 */

// ============ 用户认证类型 ============

export interface LoginCredentials {
  /**
   * 多租户隔离所需：租户ID。
   * 未显式传入时，前端会使用 VITE_TENANT_ID 或默认值。
   */
  tenant_id?: string;
  email_or_username: string;
  password: string;
  remember_me?: boolean;
}

export interface RegisterData {
  tenant_id?: string;
  email: string;
  password: string;
  confirmPassword: string;
  full_name?: string;
  phone?: string;
  sms_verification_code?: string;
}

export interface User {
  id: number | string;
  username: string;
  email: string;
  full_name?: string;
  is_active: boolean;
  is_admin: boolean;
  created_at: string;
  updated_at: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;

  // 可选：多因素认证与临时令牌支持
  require_mfa?: boolean;
  temp_token?: string;
}

export interface PasswordResetRequest {
  email: string;
}

export interface PasswordResetConfirm {
  token: string;
  new_password: string;
}

// ============ API响应类型 ============

export interface ApiResponse<T = any> {
  success: boolean;
  code: number;
  message: string;
  data?: T;
}

// ============ 表单状态类型 ============

export interface FormErrors {
  [key: string]: string;
}

export interface LoginFormState {
  email_or_username: string;
  password: string;
  remember_me: boolean;
  errors: FormErrors;
  isSubmitting: boolean;
}

export interface RegisterFormState {
  email: string;
  password: string;
  confirmPassword: string;
  full_name: string;
  phone?: string;
  sms_verification_code?: string;
  errors: FormErrors;
  isSubmitting: boolean;
}

// ============ 认证状态类型 ============

export interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  isInitialized: boolean;
  user: User | null;
  token: string | null;
  refreshToken: string | null;
  error: string | null;
  lastActivity: number;
  tokenExpiryTime: number | null;
}

// ============ 密码强度类型 ============

export interface PasswordStrength {
  is_valid: boolean;
  errors: string[];
  strength: 'weak' | 'medium' | 'strong';
}

// ============ 登录尝试限制类型 ============

export interface LoginAttemptStatus {
  is_locked: boolean;
  remaining_attempts: number;
  lockout_time_remaining?: number; // 秒
}
