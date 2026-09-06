/**
 * 认证API服务
 * 与后端用户中心认证API通信
 */

import axios, { AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios';
import type {
  LoginCredentials,
  RegisterData,
  TokenResponse,
  PasswordResetRequest,
  PasswordResetConfirm,
  ApiResponse,
  User,
} from '../types/auth.types';
import { handleError, handleAuthError, handleNetworkError, handleServerError } from '../utils/errorHandler';
import { performanceMonitor } from '../utils/performance';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../../../config/services';

interface AuthRequestConfig extends AxiosRequestConfig {
  _skipAuthRefresh?: boolean;
  _suppressAuthErrorLog?: boolean;
}

/**
 * 认证API服务类
 */
class AuthService {
  private axiosInstance: AxiosInstance;
  private readonly rawBaseURL = resolveWebSafeServiceBase(
    (import.meta as any).env?.VITE_USER_API_URL,
    SERVICE_ENDPOINTS.USER_SERVICE,
  );
  private readonly baseURL: string;
  private readonly apiPrefix: string;
  private readonly disableAuth: boolean;
  private refreshRetryCount = 0;
  private readonly maxRefreshRetries = 2;
  private refreshRetryResetTimer: ReturnType<typeof setTimeout> | null = null;
  private refreshInFlight: Promise<string> | null = null;

  constructor() {
    this.disableAuth = String((import.meta as any).env?.VITE_DISABLE_AUTH || '').toLowerCase() === 'true';
    const { baseURL, apiPrefix } = this.normalizeBaseURL(this.rawBaseURL);
    this.baseURL = baseURL;
    this.apiPrefix = apiPrefix;
    this.axiosInstance = axios.create({
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    this.axiosInstance.interceptors.request.use((config) => {
      config.baseURL = this.getRuntimeBaseURL();
      return config;
    });

    this.setupInterceptors();

    if (this.disableAuth) {
      const existingUser = this.getStoredUser();
      if (!existingUser) {
        const now = new Date().toISOString();
        const devAdmin = {
          id: 1,
          username: 'admin',
          email: 'admin@example.com',
          full_name: 'Administrator',
          is_active: true,
          is_admin: true,
          created_at: now,
          updated_at: now,
        } as User;
        localStorage.setItem('user', JSON.stringify(devAdmin));
        localStorage.setItem('access_token', 'dev-admin-token');
        localStorage.setItem('refresh_token', '');
      }
    }
  }

  /**
   * 规范化基础URL，分离域名和路径前缀
   */
  private normalizeBaseURL(url: string): { baseURL: string; apiPrefix: string } {
    try {
      const parsed = new URL(url);
      let apiPrefix = parsed.pathname.replace(/\/$/, '');
      if (apiPrefix === '/') apiPrefix = '';
      parsed.pathname = '';
      parsed.search = '';
      parsed.hash = '';
      let baseURL = parsed.toString();
      if (baseURL.endsWith('/')) baseURL = baseURL.slice(0, -1);
      return { baseURL, apiPrefix };
    } catch {
      return { baseURL: url.replace(/\/$/, ''), apiPrefix: '' };
    }
  }

  private getRuntimeBaseURL(): string {
    return this.normalizeBaseURL(resolveWebSafeServiceBase(
      (import.meta as any).env?.VITE_USER_API_URL,
      SERVICE_ENDPOINTS.USER_SERVICE,
    )).baseURL;
  }

  private getResolvedRequestUrl(path: string): string {
    return `${this.getRuntimeBaseURL()}${this.buildApiPath(path)}`;
  }

  /**
   * 获取租户ID（按优先级：已存储用户 > localStorage > 环境变量 > 默认）
   */
  public getTenantId(): string {
    // 1. 从已存储用户中获取
    try {
      const storedUser = this.getStoredUser() as any;
      const userTenant = String(storedUser?.tenant_id || '').trim();
      if (userTenant) return userTenant;
    } catch { }

    // 2. 从 localStorage 获取
    const cachedTenant = String(localStorage.getItem('tenant_id') || '').trim();
    if (cachedTenant) return cachedTenant;

    // 3. 从环境配置获取
    const fromEnv = String((import.meta as any).env?.VITE_TENANT_ID || '').trim();
    return fromEnv || 'default';
  }

  private async sleep(ms: number) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  private recordRefreshFailure() {
    this.refreshRetryCount += 1;
    if (this.refreshRetryResetTimer) {
      clearTimeout(this.refreshRetryResetTimer);
    }
    // 在短时间内持续失败时阻止刷新风暴，30 秒后自动解锁
    this.refreshRetryResetTimer = setTimeout(() => {
      this.refreshRetryCount = 0;
      this.refreshRetryResetTimer = null;
    }, 30000);
  }

  public async getRefreshedToken(refreshToken: string): Promise<string> {
    if (!this.refreshInFlight) {
      this.refreshInFlight = this.refreshAuthToken(refreshToken)
        .then((token) => {
          this.refreshRetryCount = 0;
          return token;
        })
        .catch((err) => {
          this.recordRefreshFailure();
          throw err;
        })
        .finally(() => {
          this.refreshInFlight = null;
        });
    }
    return this.refreshInFlight;
  }

  private isRefreshRequest(url?: string): boolean {
    if (!url) return false;
    return url.includes('/auth/refresh') || url.endsWith('/refresh');
  }

  private async requestWithRetry<T>(
    fn: () => Promise<T>,
    retries = 2,
    delayMs = 500
  ): Promise<T> {
    let attempt = 0;
    while (true) {
      try {
        return await fn();
      } catch (err) {
        attempt += 1;
        const isLast = attempt > retries;
        // 仅对网络错误/超时做重试，其他直接抛出
        const code = (err as any)?.code || '';
        const status = (err as any)?.response?.status;
        const isNetworkError = code === 'ECONNABORTED' || code === 'ERR_NETWORK' || status === undefined;
        if (isLast || !isNetworkError) {
          throw err;
        }
        await this.sleep(delayMs * attempt);
      }
    }
  }

  /**
   * 构建完整API路径（支持可选前缀）
   */
  private buildApiPath(path: string): string {
    const normalized = path.startsWith('/') ? path : `/${path}`;
    // 如果是非认证类接口（如 /api/**），不追加认证前缀
    if (normalized.startsWith('/api/')) return normalized;
    if (!this.apiPrefix) return normalized;
    return `${this.apiPrefix}${normalized}`.replace(/\/{2,}/g, '/');
  }

  /**
   * 设置拦截器
   */
  private setupInterceptors(): void {
    // 请求拦截器
    this.axiosInstance.interceptors.request.use(
      (config) => {
        console.log(`[Auth Request] ${config.method?.toUpperCase()} ${config.url}`, config.data);
        return config;
      },
      (error) => {
        console.error('[Auth Request Error]', error);
        return Promise.reject(error);
      }
    );

    // 响应拦截器
    this.axiosInstance.interceptors.response.use(
      (response: AxiosResponse) => {
        console.log(`[Auth Response] ${response.config.url}`, response.data);
        return response;
      },
      async (error) => {
        return this.handle401Error(error, this.axiosInstance);
      }
    );
  }

  /**
   * 通用的401错误处理方法（支持外部 Axios 实例调用）
   * 实现逻辑：遇到 401 时自动刷新 Token 并重试原始请求
   */
  public async handle401Error(error: any, axiosInstance: AxiosInstance): Promise<any> {
    const config = error.config;
    if (!config) return Promise.reject(this.handleError(error));

    // 后台轮询（QLIB/WS 重连期）网络瞬断静默，不刷全局 Auth Error
    const isBackgroundPoll = typeof config.url === 'string' && (
      config.url.includes('/qlib/') || config.url.includes('/ws/')
    );
    const isTransientNetwork = error?.code === 'ERR_NETWORK' || !error?.response;
    if (isBackgroundPoll && isTransientNetwork) {
      return Promise.reject(error);
    }

    const status = error.response?.status;
    const shouldSuppressErrorLog = !!config._suppressAuthErrorLog && status === 401;
    if (!shouldSuppressErrorLog) {
      console.error(`[Auth] 处理错误: ${config.url}`, status || error.code || error.message);
    }

    const isRefreshRequest = this.isRefreshRequest(config.url);
    const skipAuthRefresh = !!config._skipAuthRefresh || isRefreshRequest;

    const shouldHandle401 =
      !this.disableAuth &&
      error.response?.status === 401 &&
      !config._retry &&
      !skipAuthRefresh;

    if (shouldHandle401 && this.refreshRetryCount < this.maxRefreshRetries) {
      const refreshToken = localStorage.getItem('refresh_token');
      if (refreshToken) {
        try {
          // 使用单例 Promise 确保并发请求只触发一次刷新
          const newToken = await this.getRefreshedToken(refreshToken);
          localStorage.setItem('access_token', newToken);

          // 重试原请求
          config._retry = true;
          if (config.headers) {
            config.headers.Authorization = `Bearer ${newToken}`;
          }
          console.log(`[Auth] Token刷新成功，重试请求: ${config.url}`);
          return axiosInstance.request(config);
        } catch (refreshError) {
          console.error('[Auth] 自动重试过程中令牌刷新失败:', refreshError);
          this.recordRefreshFailure();
          this.clearTokens();
          // 返回原始错误
          return Promise.reject(this.handleError(error));
        }
      } else {
        this.recordRefreshFailure();
      }
    }

    if (isRefreshRequest && error.response?.status === 401) {
      this.recordRefreshFailure();
      this.clearTokens();
    }

    return Promise.reject(this.handleError(error));
  }

  /**
   * 处理错误
   */
  private handleError(error: any, context?: string): Error {
    const standardError = handleError(error, { showNotification: false, showMessage: false, context });
    const msg = standardError.details && standardError.details !== standardError.message
      ? `${standardError.message}：${standardError.details}`
      : standardError.message;
    const wrapped = new Error(msg);
    // 保留原始响应信息，便于上层根据状态码执行降级或回退
    (wrapped as any).response = error?.response;
    (wrapped as any).code = error?.code;
    (wrapped as any).config = error?.config;
    return wrapped;
  }

  private async postWithFallback(
    paths: string[],
    data?: any,
    config?: AxiosRequestConfig
  ): Promise<AxiosResponse> {
    for (const p of paths) {
      try {
        let payload = data;
        let cfg: AxiosRequestConfig | undefined = config;
        if (p.endsWith('/token')) {
          const form = new URLSearchParams();
          if (data?.username) form.append('username', data.username);
          if (data?.password) form.append('password', data.password);
          if (data?.remember_me !== undefined) form.append('remember_me', String(data.remember_me));
          form.append('grant_type', 'password');
          payload = form;
          cfg = {
            ...(config || {}),
            headers: { ...(config?.headers || {}), 'Content-Type': 'application/x-www-form-urlencoded' }
          };
        }
        const resp = await this.requestWithRetry(
          () => this.axiosInstance.post(this.buildApiPath(p), payload, cfg),
          2,
          500
        );
        return resp;
      } catch (err: any) {
        const status = err?.response?.status;
        if (status === 404 || status === 415) {
          continue;
        }
        throw err;
      }
    }
    throw new Error('接口不存在');
  }

  /**
   * 解包API响应（兼容直接返回数据或标准结构）
   */
  private unwrapResponse<T>(payload: ApiResponse<T> | T | undefined, context: string): T {
    if (!payload) {
      throw new Error(`${context}：响应为空`);
    }
    // Handle standard response with 'code' and 'data'
    if (typeof payload === 'object' && 'code' in (payload as Record<string, unknown>)) {
      const apiPayload = payload as any;
      if (apiPayload.code === 200) {
        return apiPayload.data;
      }
      throw new Error(apiPayload.message || `${context}失败`);
    }
    // Handle legacy response with 'success'
    if (typeof payload === 'object' && 'success' in (payload as Record<string, unknown>)) {
      const apiPayload = payload as ApiResponse<T>;
      if (!apiPayload.success || apiPayload.data == null) {
        throw new Error(apiPayload.message || `${context}失败`);
      }
      return apiPayload.data;
    }
    return payload as T;
  }

  /**
   * 适配后端返回的TokenResponse结构
   */
  private normalizeTokenResponse(payload: any): TokenResponse {
    if (!payload) {
      throw new Error('登录响应无效');
    }
    if ('user' in payload) {
      const resp = payload as TokenResponse;
      if (resp.user && !resp.user.id && (resp.user as any).user_id) {
        resp.user.id = (resp.user as any).user_id;
      }
      return resp;
    }
    if ('user_info' in payload) {
      const info = payload.user_info ?? {};
      return {
        access_token: payload.access_token,
        refresh_token: payload.refresh_token ?? null,
        token_type: payload.token_type ?? 'bearer',
        expires_in: payload.expires_in ?? 0,
        user: {
          id: info.user_id ?? info.id ?? '',
          username: info.username ?? '',
          email: info.email ?? '',
          full_name: info.full_name ?? info.username ?? '',
          is_active: info.is_active ?? true,
          is_admin: typeof info.is_admin === 'boolean'
            ? info.is_admin
            : Array.isArray(info.roles) ? info.roles.includes('admin') : false,
          created_at: info.created_at ?? '',
          updated_at: info.last_login ?? info.created_at ?? '',
        },
      };
    }
    throw new Error('无法解析登录响应');
  }

  /**
   * 发送验证码
   */
  async sendVerificationCode(identifier: string, type: 'email' | 'sms', purpose: 'register' | 'reset_password' = 'register'): Promise<void> {
    try {
      const response = await this.axiosInstance.post(this.buildApiPath('/auth/send-verification'), {
        identifier,
        type,
        purpose,
      });

      if (response.data && 'message' in response.data) {
        // Success
        return;
      }
    } catch (error) {
      console.error('验证码发送失败:', error);
      throw this.handleError(error, 'send_verification_code');
    }
  }

  /**
   * 从注册表单推导后端要求的 username（字母和数字，≥3位）。
   * 优先用“用户名”栏的字母数字部分，否则用邮箱前缀；都不够 3 位时抛错提示用户补充。
   */
  private deriveRegisterUsername(userData: RegisterData): string {
    const alnum = (s: string) => (s || '').replace(/[^A-Za-z0-9]/g, '');
    const fromName = alnum(userData.full_name || '');
    if (fromName.length >= 3) return fromName;
    const emailPrefix = alnum((userData.email || '').split('@')[0] || '');
    if (emailPrefix.length >= 3) return emailPrefix;
    throw new Error('用户名至少需要3个字母或数字，请在用户名栏补充');
  }

  /**
   * 用户注册（OSS版：邮箱直注，无手机/邮箱验证码，后端在线即可）。
   */
  async register(userData: RegisterData): Promise<TokenResponse> {
    const startTime = Date.now();
    try {
      const tenantId = (userData.tenant_id || this.getTenantId()).trim();
      const email = (userData.email || '').trim();
      if (!email) throw new Error('请输入邮箱地址');
      const baseUsername = this.deriveRegisterUsername(userData);
      const basePayload = {
        tenant_id: tenantId,
        email,
        password: userData.password,
        full_name: userData.full_name?.trim() || undefined,
      };

      // 用户名冲突时自动加 4 位数字后缀重试一次，避免直注卡死
      let response;
      try {
        response = await this.postWithFallback(['/auth/register'], {
          ...basePayload,
          username: baseUsername,
        });
      } catch (err: any) {
        const msg = err?.response?.data?.detail || err?.message || '';
        if (typeof msg === 'string' && msg.includes('用户名已存在')) {
          const retryUsername = `${baseUsername}${Math.floor(1000 + Math.random() * 9000)}`;
          response = await this.postWithFallback(['/auth/register'], {
            ...basePayload,
            username: retryUsername,
          });
        } else {
          throw err;
        }
      }
      const endTime = Date.now();

      // 记录性能指标
      if (performanceMonitor['isMonitoring']) {
        performanceMonitor.recordApiRequest(
          this.getResolvedRequestUrl('/register'),
          'POST',
          startTime,
          endTime,
          true
        );
      }

      const tokenData = this.normalizeTokenResponse(
        this.unwrapResponse<TokenResponse | any>(response.data, '注册失败')
      );

      // 存储令牌
      localStorage.setItem('access_token', tokenData.access_token);
      localStorage.setItem('refresh_token', tokenData.refresh_token);
      localStorage.setItem('user', JSON.stringify(tokenData.user));

      return tokenData;
    } catch (error) {
      const endTime = Date.now();

      // 记录性能指标
      if (performanceMonitor['isMonitoring']) {
        performanceMonitor.recordApiRequest(
          this.getResolvedRequestUrl('/register'),
          'POST',
          startTime,
          endTime,
          false,
          error instanceof Error ? error.message : String(error)
        );
      }

      console.error('注册失败:', error);
      throw this.handleError(error, 'user_register');
    }
  }

  /**
   * 发送注册短信验证码
   */
  async requestRegisterSmsCode(phoneNumber: string): Promise<void> {
    try {
      const tenantId = this.getTenantId();
      await this.axiosInstance.post(this.buildApiPath('/sms/send'), {
        phone: phoneNumber,
        tenant_id: tenantId,
        type: 'register',
      });
    } catch (error) {
      throw this.handleError(error, 'request_register_sms_code');
    }
  }

  /**
   * 用户登录
   */
  async login(credentials: LoginCredentials): Promise<TokenResponse> {
    const startTime = Date.now();
    try {
      if (this.disableAuth) {
        const now = new Date().toISOString();
        const devAdmin: User = {
          id: 1,
          username: 'admin',
          email: 'admin@example.com',
          full_name: 'Administrator',
          is_active: true,
          is_admin: true,
          created_at: now,
          updated_at: now,
        };
        const tokenData: TokenResponse = {
          access_token: 'dev-admin-token',
          refresh_token: '',
          token_type: 'bearer',
          expires_in: 7 * 24 * 60 * 60,
          user: devAdmin,
        };
        localStorage.setItem('access_token', tokenData.access_token);
        localStorage.setItem('refresh_token', tokenData.refresh_token);
        localStorage.setItem('user', JSON.stringify(tokenData.user));
        if (credentials.remember_me) localStorage.setItem('remember_login', 'true');
        return tokenData;
      }
      const tenantId = (credentials.tenant_id || this.getTenantId()).trim();
      const response = await this.postWithFallback(
        ['/auth/login', '/auth/token'],
        {
          tenant_id: tenantId,
          username: credentials.email_or_username,
          email: credentials.email_or_username,
          login: credentials.email_or_username,
          email_or_username: credentials.email_or_username,
          password: credentials.password,
          remember_me: credentials.remember_me || false,
        },
        { headers: { Accept: 'application/json' } }
      );
      const endTime = Date.now();

      // 记录性能指标
      if (performanceMonitor['isMonitoring']) {
        performanceMonitor.recordApiRequest(
          this.getResolvedRequestUrl('/login'),
          'POST',
          startTime,
          endTime,
          true
        );
      }

      const tokenData = this.normalizeTokenResponse(
        this.unwrapResponse<TokenResponse | any>(response.data, '登录失败')
      );
      if (tokenData) {
        localStorage.setItem('access_token', tokenData.access_token);
        localStorage.setItem('refresh_token', tokenData.refresh_token);
        localStorage.setItem('user', JSON.stringify(tokenData.user));
        if (credentials.remember_me) localStorage.setItem('remember_login', 'true');
      }
      return tokenData;
    } catch (error) {
      const endTime = Date.now();

      // 记录性能指标
      if (performanceMonitor['isMonitoring']) {
        performanceMonitor.recordApiRequest(
          this.getResolvedRequestUrl('/login'),
          'POST',
          startTime,
          endTime,
          false,
          error instanceof Error ? error.message : String(error)
        );
      }

      console.error('登录失败:', error);
      throw this.handleError(error, 'user_login');
    }
  }

  /**
   * 用户登出
   */
  async logout(): Promise<void> {
    try {
      if (this.disableAuth) {
        this.clearTokens();
        return;
      }
      const token = localStorage.getItem('access_token');
      if (token) {
        await this.postWithFallback(
          ['/auth/logout', '/logout'],
          {},
          { headers: { Authorization: `Bearer ${token}` } }
        );
      }
    } catch (error) {
      console.error('登出请求失败:', error);
    } finally {
      // 无论请求是否成功，都清除本地令牌
      this.clearTokens();
    }
  }

  /**
   * 刷新访问令牌
   */
  async refreshAuthToken(refreshToken: string): Promise<string> {
    try {
      // 优先尝试标准路径 /auth/refresh
      const response = await this.postWithFallback(
        ['/auth/refresh', '/refresh'],
        { refresh_token: refreshToken },
        { _skipAuthRefresh: true } as AxiosRequestConfig
      );

      const data = this.unwrapResponse<{ access_token: string; refresh_token?: string }>(
        response.data,
        '令牌刷新失败'
      );

      // 如果后端返回了新的刷新令牌（令牌轮换），也进行保存
      if (data.refresh_token) {
        localStorage.setItem('refresh_token', data.refresh_token);
        console.log('[Auth] Refresh token rotated');
      }

      return data.access_token;
    } catch (error) {
      console.error('令牌刷新失败:', error);
      if ((error as any)?.response?.status === 401) {
        this.recordRefreshFailure();
        // 让调用者（handle401Error 或 initializeAuth）决定是否清除令牌
        // this.clearTokens();
      }
      throw error;
    }
  }

  async requestSmsCode(phoneNumber: string): Promise<void> {
    const tenantId = this.getTenantId();
    try {
      // 统一走后端短信服务（阿里云短信），由网关代理到 user_service。
      await this.axiosInstance.post(this.buildApiPath('/sms/send'), {
        phone: phoneNumber,
        tenant_id: tenantId,
        type: 'login',
      });
    } catch (error) {
      throw new Error(error instanceof Error ? error.message : '验证码发送失败');
    }
  }

  async loginWithSmsCode(phoneNumber: string, code: string): Promise<TokenResponse> {
    const tenantId = this.getTenantId();
    try {
      const response = await this.postWithFallback(
        ['/auth/login/phone'],
        { phone: phoneNumber, code, tenant_id: tenantId }
      );

      const tokenData = this.normalizeTokenResponse(
        this.unwrapResponse<TokenResponse | any>(response.data, '登录失败')
      );
      localStorage.setItem('access_token', tokenData.access_token);
      localStorage.setItem('refresh_token', tokenData.refresh_token);
      localStorage.setItem('user', JSON.stringify(tokenData.user));
      return tokenData;
    } catch (error) {
      throw new Error(error instanceof Error ? error.message : '短信验证码登录失败');
    }
  }

  /**
   * 检查用户名/手机号/邮箱可用性
   */
  async checkAvailability(type: 'username' | 'phone' | 'email', value: string): Promise<boolean> {
    const tenantId = this.getTenantId();
    try {
      const response = await this.axiosInstance.post(this.buildApiPath('/auth/check-availability'), {
        type,
        value,
        tenant_id: tenantId,
      });

      const data = this.unwrapResponse<{ available: boolean }>(response.data, '检查可用性失败');
      return data.available;
    } catch (error) {
      console.error(`检查${type}可用性失败:`, error);
      // 检查失败时默认为可用，以免阻塞用户，或者根据需要抛出错误
      return true;
    }
  }

  /**
   * 忘记密码 (手机验证码方式)
   */
  async forgotPasswordByPhone(phone: string, code: string, newPassword: string): Promise<void> {
    const tenantId = this.getTenantId();
    try {
      const response = await this.axiosInstance.post(this.buildApiPath('/auth/password/reset/phone'), {
        phone,
        code,
        new_password: newPassword,
        tenant_id: tenantId,
      });

      if (response.data && response.data.code !== 200) {
        throw new Error(response.data.message || '重置密码失败');
      }
    } catch (error) {
      console.error('手机重置密码失败:', error);
      throw error;
    }
  }

  /**
   * 发送重置密码短信验证码
   */
  async requestResetPasswordSmsCode(phoneNumber: string): Promise<void> {
    const tenantId = this.getTenantId();
    try {
      await this.axiosInstance.post(this.buildApiPath('/sms/send'), {
        phone: phoneNumber,
        tenant_id: tenantId,
        type: 'reset_password',
      });
    } catch (error) {
      throw new Error(error instanceof Error ? error.message : '验证码发送失败');
    }
  }

  /**
   * 忘记密码
   */
  async forgotPassword(email: string): Promise<void> {
    try {
      const response = await this.axiosInstance.post<ApiResponse>(this.buildApiPath('/forgot-password'), {
        email,
      });

      if (response.data && 'success' in response.data && !response.data.success) {
        throw new Error(response.data.message || '发送重置邮件失败');
      }
    } catch (error) {
      console.error('忘记密码失败:', error);
      throw error;
    }
  }

  /**
   * 重置密码
   */
  async resetPassword(token: string, newPassword: string): Promise<void> {
    try {
      const response = await this.axiosInstance.post<ApiResponse>(this.buildApiPath('/reset-password'), {
        token,
        new_password: newPassword,
      });

      if (response.data && 'success' in response.data && !response.data.success) {
        throw new Error(response.data.message || '密码重置失败');
      }
    } catch (error) {
      console.error('密码重置失败:', error);
      throw error;
    }
  }

  /**
   * 获取当前用户信息
   */
  async getCurrentUser(options?: { suppressUnauthorizedLog?: boolean }): Promise<User | null> {
    const suppressUnauthorizedLog = options?.suppressUnauthorizedLog === true;
    try {
      if (this.disableAuth) {
        return this.getStoredUser();
      }
      const token = this.getAccessToken();
      if (!token) {
        return null;
      }
      const response = await this.axiosInstance.get<ApiResponse<User> | User>(this.buildApiPath('/users/me'), {
        headers: {
          Authorization: `Bearer ${token}`,
        },
        _suppressAuthErrorLog: suppressUnauthorizedLog,
      } as AuthRequestConfig);

      const userData = this.unwrapResponse<User>(response.data, '获取用户信息失败');

      // 兼容后端返回 user_id 而前端期待 id 的差异
      if (!userData.id && (userData as any).user_id) {
        userData.id = (userData as any).user_id;
      }

      localStorage.setItem('user', JSON.stringify(userData));
      return userData;
    } catch (error) {
      const status = (error as any)?.response?.status;
      if (!(suppressUnauthorizedLog && status === 401)) {
        console.error('获取当前用户失败:', error);
      }
      // Only clear tokens on auth errors (401/403), not network/timeout issues
      if (status === 401 || status === 403) {
        this.clearTokens();
      }
      return null;
    }
  }

  /**
   * 清除本地令牌
   */
  clearTokens(): void {
    console.warn('[Auth] clearTokens called', new Error().stack);
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    localStorage.removeItem('user');
    localStorage.removeItem('remember_login');
  }

  /**
   * 检查是否已登录
   */
  isAuthenticated(): boolean {
    return !!this.getAccessToken();
  }

  /**
   * 获取存储的用户信息
   */
  getStoredUser(): User | null {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        return JSON.parse(userStr);
      } catch (error) {
        console.error('解析用户信息失败:', error);
        this.clearTokens();
      }
    }
    return null;
  }

  /**
   * 检查令牌是否过期
   */
  isTokenExpired(): boolean {
    if (this.disableAuth) {
      return false;
    }
    const token = localStorage.getItem('access_token');
    if (!token) {
      return true;
    }
    try {
      const payload = JSON.parse(atob(token.split('.')[1]));
      const currentTime = Math.floor(Date.now() / 1000);
      return payload.exp < currentTime;
    } catch (error) {
      console.error('令牌解析失败:', error);
      return true;
    }
  }

  /**
   * 获取访问令牌
   */
  getAccessToken(): string | null {
    const token = localStorage.getItem('access_token');
    if (!token) {
      return null;
    }
    if (this.disableAuth) {
      return token;
    }
    if (token === 'dev-admin-token' || token.split('.').length !== 3) {
      this.clearTokens();
      return null;
    }
    return token;
  }

  /**
   * 获取刷新令牌
   */
  getRefreshToken(): string | null {
    return localStorage.getItem('refresh_token');
  }

}

// 导出单例实例
export const authService = new AuthService();
