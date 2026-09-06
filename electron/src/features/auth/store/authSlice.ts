/**
 * 认证状态管理 Redux Slice
 */

import { createSlice, createAsyncThunk, PayloadAction } from '@reduxjs/toolkit';
import type {
  AuthState,
  User,
  LoginCredentials,
  RegisterData,
  FormErrors,
  LoginFormState,
  RegisterFormState,
  PasswordResetRequest,
  PasswordResetConfirm,
} from '../types/auth.types';
import { authService } from '../services/authService';

// 初始状态
const initialState: AuthState & {
  loginForm: LoginFormState;
  registerForm: RegisterFormState;
} = {
  // 认证状态
  isAuthenticated: false,
  isLoading: false,
  isInitialized: false,
  serverUnreachable: false,
  user: null,
  token: null,
  refreshToken: null,
  error: null,
  lastActivity: Date.now(),
  tokenExpiryTime: null,

  // 登录表单状态
  loginForm: {
    email_or_username: '',
    password: '',
    remember_me: false,
    errors: {},
    isSubmitting: false,
  },

  // 注册表单状态
  registerForm: {
    email: '',
    password: '',
    confirmPassword: '',
    full_name: '',
    phone: '',
    sms_verification_code: '',
    errors: {},
    isSubmitting: false,
  },
};

// ============ 异步 Actions ============

// 初始化认证状态
export const initializeAuth = createAsyncThunk(
  'auth/initialize',
  async () => {
    try {
      // 检查本地存储的令牌
      if (authService.isAuthenticated()) {
        // 验证令牌是否有效
        if (authService.isTokenExpired()) {
          // 令牌过期，尝试刷新
          const refreshToken = authService.getRefreshToken();
          if (refreshToken) {
            try {
              const newToken = await Promise.race([
                authService.refreshAuthToken(refreshToken),
                new Promise((_, reject) => setTimeout(() => reject(new Error('刷新Token超时')), 5000))
              ]) as string;
              localStorage.setItem('access_token', newToken);
            } catch (error) {
              console.error('刷新Token失败:', error);
              authService.clearTokens();
              return { user: null, token: null };
            }
          } else {
            authService.clearTokens();
            return { user: null, token: null };
          }
        }

        // 获取当前用户信息（添加超时保护）
        try {
          const user = await Promise.race([
            authService.getCurrentUser({ suppressUnauthorizedLog: true }),
            new Promise<null>((_, reject) =>
              setTimeout(() => reject(new Error('获取用户信息超时')), 5000)
            )
          ]);
          const token = authService.getAccessToken();
          if (!user) {
            // token 已被 401/403 清掉 → 未登录；token 还在却拿不到用户 →
            // 服务器不可达（getCurrentUser 网络失败返回 null 而非抛错），不得放行。
            if (token) {
              return { user: null, token: null, serverUnreachable: true };
            }
            return { user: null, token: null };
          }

          return {
            user,
            token,
          };
        } catch (error) {
          console.error('获取用户信息失败（含超时）:', error);
          // 仅 401/403 清除 token（已在 getCurrentUser 内部处理）。
          // 网络错误/超时 = 服务器不可达：绝不拿本地缓存冒充已登录，
          // 打标后由路由层展示离线页，token 保留以便恢复后免登。
          const status = (error as any)?.response?.status;
          if (status === 401 || status === 403) {
            authService.clearTokens();
          } else {
            return { user: null, token: null, serverUnreachable: true };
          }
          return { user: null, token: null };
        }
      }
    } catch (error) {
      console.error('初始化认证状态失败:', error);
      authService.clearTokens();
    }

    return { user: null, token: null };
  },
  {
    condition: (_, { getState }) => {
      const { auth } = getState() as { auth: AuthState };
      // 如果已经初始化过，或正在加载中，则跳过
      if (auth.isInitialized || auth.isLoading) {
        return false;
      }
      return true;
    }
  }
);

// 用户登录
export const login = createAsyncThunk(
  'auth/login',
  async (credentials: LoginCredentials, { rejectWithValue }) => {
    try {
      const tokenResponse = await authService.login(credentials);
      // 防御性写入，避免登录后本地令牌为空导致被判未登录
      try {
        localStorage.setItem('access_token', tokenResponse.access_token);
        localStorage.setItem('refresh_token', tokenResponse.refresh_token);
        localStorage.setItem('user', JSON.stringify(tokenResponse.user));
      } catch { }
      return {
        user: tokenResponse.user,
        token: tokenResponse.access_token,
        refreshToken: tokenResponse.refresh_token,
        tokenExpiryTime: Date.now() + tokenResponse.expires_in * 1000,
        require_mfa: tokenResponse.require_mfa,
        tempToken: tokenResponse.temp_token,
      };
    } catch (error: any) {
      let debugMsg = error.message || '登录失败';
      if (error.response?.status || error.code) {
        const parts = [];
        if (error.response?.status) parts.push(`HTTP ${error.response.status}`);
        if (error.code) parts.push(`Code: ${error.code}`);
        if (error.config?.url) parts.push(`API: ${error.config.url.split('?')[0]}`);
        debugMsg += ` [${parts.join(' | ')}]`;
      }
      return rejectWithValue(debugMsg);
    }
  }
);

// 短信验证码登录
export const loginWithSmsCode = createAsyncThunk(
  'auth/loginWithSmsCode',
  async (
    { phoneNumber, code }: { phoneNumber: string; code: string },
    { rejectWithValue }
  ) => {
    try {
      const tokenResponse = await authService.loginWithSmsCode(phoneNumber, code);
      try {
        localStorage.setItem('access_token', tokenResponse.access_token);
        localStorage.setItem('refresh_token', tokenResponse.refresh_token);
        localStorage.setItem('user', JSON.stringify(tokenResponse.user));
      } catch { }
      return {
        user: tokenResponse.user,
        token: tokenResponse.access_token,
        refreshToken: tokenResponse.refresh_token,
        tokenExpiryTime: Date.now() + tokenResponse.expires_in * 1000,
        require_mfa: tokenResponse.require_mfa,
        tempToken: tokenResponse.temp_token,
      };
    } catch (error: any) {
      let debugMsg = error.message || '验证码登录失败';
      if (error.response?.status || error.code) {
        const parts = [];
        if (error.response?.status) parts.push(`HTTP ${error.response.status}`);
        if (error.code) parts.push(`Code: ${error.code}`);
        if (error.config?.url) parts.push(`API: ${error.config.url.split('?')[0]}`);
        debugMsg += ` [${parts.join(' | ')}]`;
      }
      return rejectWithValue(debugMsg);
    }
  }
);

// 用户注册
export const register = createAsyncThunk(
  'auth/register',
  async (userData: RegisterData, { rejectWithValue }) => {
    try {
      const tokenResponse = await authService.register(userData);
      try {
        localStorage.setItem('access_token', tokenResponse.access_token);
        localStorage.setItem('refresh_token', tokenResponse.refresh_token);
        localStorage.setItem('user', JSON.stringify(tokenResponse.user));
      } catch { }
      return {
        user: tokenResponse.user,
        token: tokenResponse.access_token,
        refreshToken: tokenResponse.refresh_token,
        tokenExpiryTime: Date.now() + tokenResponse.expires_in * 1000,
        require_mfa: tokenResponse.require_mfa,
        tempToken: tokenResponse.temp_token,
      };
    } catch (error: any) {
      return rejectWithValue(error.message || '注册失败');
    }
  }
);

// 用户登出
export const logout = createAsyncThunk(
  'auth/logout',
  async (_, { rejectWithValue }) => {
    try {
      await authService.logout();
    } catch (error: any) {
      console.error('登出请求失败:', error);
      // 即使登出请求失败，也要清除本地状态
    }
  }
);

// 忘记密码
export const forgotPassword = createAsyncThunk(
  'auth/forgotPassword',
  async (request: PasswordResetRequest, { rejectWithValue }) => {
    try {
      await authService.forgotPassword(request.email);
      return request.email;
    } catch (error: any) {
      return rejectWithValue(error.message || '发送重置邮件失败');
    }
  }
);

// 重置密码
export const resetPassword = createAsyncThunk(
  'auth/resetPassword',
  async (request: PasswordResetConfirm, { rejectWithValue }) => {
    try {
      await authService.resetPassword(request.token, request.new_password);
      return true;
    } catch (error: any) {
      return rejectWithValue(error.message || '密码重置失败');
    }
  }
);

// 刷新令牌
export const refreshToken = createAsyncThunk(
  'auth/refreshToken',
  async (_, { getState, rejectWithValue }) => {
    try {
      const state = getState() as { auth: AuthState };
      const refreshToken = authService.getRefreshToken();

      if (!refreshToken) {
        throw new Error('没有刷新令牌');
      }

      const newToken = await authService.refreshAuthToken(refreshToken);
      const user = await authService.getCurrentUser();

      return {
        token: newToken,
        user,
        tokenExpiryTime: Date.now() + 24 * 60 * 60 * 1000, // 24小时
      };
    } catch (error: any) {
      return rejectWithValue(error.message || '令牌刷新失败');
    }
  }
);

// ============ Slice ============

const authSlice = createSlice({
  name: 'auth',
  initialState,
  reducers: {
    // 登录表单操作
    updateLoginForm: (state, action: PayloadAction<{ field: string; value: any }>) => {
      const { field, value } = action.payload;
      (state.loginForm as any)[field] = value;
      // 清除该字段的错误
      if (state.loginForm.errors[field]) {
        delete state.loginForm.errors[field];
      }
    },

    setLoginFormErrors: (state, action: PayloadAction<FormErrors>) => {
      state.loginForm.errors = action.payload;
    },

    clearLoginFormErrors: (state) => {
      state.loginForm.errors = {};
    },

    resetLoginForm: (state) => {
      state.loginForm = {
        email_or_username: '',
        password: '',
        remember_me: false,
        errors: {},
        isSubmitting: false,
      };
    },

    // 注册表单操作
    updateRegisterForm: (state, action: PayloadAction<{ field: string; value: any }>) => {
      const { field, value } = action.payload;
      (state.registerForm as any)[field] = value;
      // 清除该字段的错误
      if (state.registerForm.errors[field]) {
        delete state.registerForm.errors[field];
      }
    },

    setRegisterFormErrors: (state, action: PayloadAction<FormErrors>) => {
      state.registerForm.errors = action.payload;
    },

    clearRegisterFormErrors: (state) => {
      state.registerForm.errors = {};
    },

    resetRegisterForm: (state) => {
      state.registerForm = {
        email: '',
        password: '',
        confirmPassword: '',
        full_name: '',
        phone: '',
        sms_verification_code: '',
        errors: {},
        isSubmitting: false,
      };
    },

    // 通用操作
    clearError: (state) => {
      state.error = null;
    },

    // 离线重试：重置初始化标记，useAuth 的 effect 会自动重跑 initializeAuth。
    // 注意不清除 token，服务器恢复后可直接免登。
    retryAuthInit: (state) => {
      state.isInitialized = false;
      state.isLoading = false;
      state.serverUnreachable = false;
      state.error = null;
    },

    updateLastActivity: (state) => {
      state.lastActivity = Date.now();
    },

    // 手动设置认证状态（用于初始化）
    setAuthState: (state, action: PayloadAction<{
      isAuthenticated: boolean;
      user: User | null;
      token: string | null;
    }>) => {
      state.isAuthenticated = action.payload.isAuthenticated;
      state.user = action.payload.user;
      state.token = action.payload.token;
      state.isLoading = false;
      state.error = null;
    },

    // 设置认证凭据
    setCredentials: (state, action: PayloadAction<{
      accessToken: string;
      refreshToken: string;
    }>) => {
      state.token = action.payload.accessToken;
      state.refreshToken = action.payload.refreshToken;
      state.isAuthenticated = true;
      state.isLoading = false;
      state.error = null;
      state.lastActivity = Date.now();
    },

    // 设置用户信息
    setUser: (state, action: PayloadAction<User>) => {
      state.user = action.payload;
      state.isAuthenticated = true;
      state.isLoading = false;
      state.error = null;
      state.lastActivity = Date.now();
    },
  },

  extraReducers: (builder) => {
    // 初始化认证状态
    builder
      .addCase(initializeAuth.pending, (state) => {
        // 仅在首次初始化时展示全局加载状态
        if (!state.isInitialized) {
          state.isLoading = true;
        }
        state.error = null;
      })
      .addCase(initializeAuth.fulfilled, (state, action) => {
        state.isLoading = false;
        state.user = action.payload.user;
        state.token = action.payload.token;
        state.isAuthenticated = !!action.payload.token;
        state.serverUnreachable = !!(action.payload as { serverUnreachable?: boolean }).serverUnreachable;
        state.error = null;
        state.isInitialized = true;
      })
      .addCase(initializeAuth.rejected, (state) => {
        state.isLoading = false;
        state.isAuthenticated = false;
        state.user = null;
        state.token = null;
        state.refreshToken = null;
        state.isInitialized = true;
      });

    // 登录
    builder
      .addCase(login.pending, (state) => {
        state.isLoading = true;
        state.loginForm.isSubmitting = true;
        state.error = null;
      })
      .addCase(login.fulfilled, (state, action) => {
        state.isLoading = false;
        state.loginForm.isSubmitting = false;
        state.loginForm.errors = {};
        state.lastActivity = Date.now();
        state.error = null;
        state.isInitialized = true;

        if (action.payload.require_mfa) {
          state.isAuthenticated = false;
          state.user = action.payload.user;
          state.token = null;
          state.refreshToken = null;
          state.tokenExpiryTime = null;
          return;
        }

        state.isAuthenticated = true;
        state.user = action.payload.user;
        state.token = action.payload.token;
        state.refreshToken = action.payload.refreshToken;
        state.tokenExpiryTime = action.payload.tokenExpiryTime;
        state.serverUnreachable = false;
      })
      .addCase(login.rejected, (state, action) => {
        state.isLoading = false;
        state.loginForm.isSubmitting = false;
        state.error = action.payload as string;
      });
    // 短信验证码登录
    builder
      .addCase(loginWithSmsCode.pending, (state) => {
        state.isLoading = true;
        state.error = null;
      })
      .addCase(loginWithSmsCode.fulfilled, (state, action) => {
        state.isLoading = false;
        state.isAuthenticated = true;
        state.user = action.payload.user;
        state.token = action.payload.token;
        state.refreshToken = action.payload.refreshToken;
        state.tokenExpiryTime = action.payload.tokenExpiryTime;
        state.lastActivity = Date.now();
        state.error = null;
        state.isInitialized = true;
      })
      .addCase(loginWithSmsCode.rejected, (state, action) => {
        state.isLoading = false;
        state.error = action.payload as string;
      });

    // 注册
    builder
      .addCase(register.pending, (state) => {
        state.isLoading = true;
        state.registerForm.isSubmitting = true;
        state.error = null;
      })
      .addCase(register.fulfilled, (state, action) => {
        state.isLoading = false;
        state.isAuthenticated = true;
        state.user = action.payload.user;
        state.token = action.payload.token;
        state.refreshToken = action.payload.refreshToken;
        state.tokenExpiryTime = action.payload.tokenExpiryTime;
        state.registerForm.isSubmitting = false;
        state.registerForm.errors = {};
        state.lastActivity = Date.now();
        state.error = null;
        state.isInitialized = true;
      })
      .addCase(register.rejected, (state, action) => {
        state.isLoading = false;
        state.registerForm.isSubmitting = false;
        state.error = action.payload as string;
      });

    // 登出
    builder
      .addCase(logout.pending, (state) => {
        state.isLoading = true;
      })
      .addCase(logout.fulfilled, (state) => {
        state.isAuthenticated = false;
        state.user = null;
        state.token = null;
        state.refreshToken = null;
        state.tokenExpiryTime = null;
        state.isLoading = false;
        state.error = null;
        state.isInitialized = true;
        state.serverUnreachable = false;
        // 重置表单
        state.loginForm = initialState.loginForm;
        state.registerForm = initialState.registerForm;
      })
      .addCase(logout.rejected, (state) => {
        // 即使登出失败，也要清除本地状态
        state.isAuthenticated = false;
        state.user = null;
        state.token = null;
        state.refreshToken = null;
        state.tokenExpiryTime = null;
        state.isLoading = false;
        state.isInitialized = true;
      });

    // 忘记密码
    builder
      .addCase(forgotPassword.pending, (state) => {
        state.isLoading = true;
        state.error = null;
      })
      .addCase(forgotPassword.fulfilled, (state) => {
        state.isLoading = false;
        state.error = null;
      })
      .addCase(forgotPassword.rejected, (state, action) => {
        state.isLoading = false;
        state.error = action.payload as string;
      });

    // 重置密码
    builder
      .addCase(resetPassword.pending, (state) => {
        state.isLoading = true;
        state.error = null;
      })
      .addCase(resetPassword.fulfilled, (state) => {
        state.isLoading = false;
        state.error = null;
      })
      .addCase(resetPassword.rejected, (state, action) => {
        state.isLoading = false;
        state.error = action.payload as string;
      });

    // 刷新令牌
    builder
      .addCase(refreshToken.fulfilled, (state, action) => {
        state.token = action.payload.token;
        state.user = action.payload.user;
        state.tokenExpiryTime = action.payload.tokenExpiryTime;
        state.lastActivity = Date.now();
      })
      .addCase(refreshToken.rejected, (state) => {
        // 刷新失败，清除认证状态
        state.isAuthenticated = false;
        state.user = null;
        state.token = null;
        state.refreshToken = null;
        state.tokenExpiryTime = null;
      });
  },
});

// 导出 actions
export const {
  updateLoginForm,
  setLoginFormErrors,
  clearLoginFormErrors,
  resetLoginForm,
  updateRegisterForm,
  setRegisterFormErrors,
  clearRegisterFormErrors,
  resetRegisterForm,
  clearError,
  updateLastActivity,
  setAuthState,
  setCredentials,
  setUser,
  retryAuthInit,
} = authSlice.actions;

// 导出 reducer
export default authSlice.reducer;
