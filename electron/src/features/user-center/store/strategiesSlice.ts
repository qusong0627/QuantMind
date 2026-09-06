/**
 * 用户策略Redux Slice
 */

import { createSlice, createAsyncThunk, PayloadAction } from '@reduxjs/toolkit';
import { strategyManagementService } from '../../../services/strategyManagementService';
import type {
  UserStrategy,
  StrategyUpdate,
  StrategyStatus,
  LoadingState,
  PaginatedResponse,
} from '../types';

// ============ 状态接口定义 ============

export interface StrategiesState {
  strategies: UserStrategy[];
  currentStrategy: UserStrategy | null;
  total: number;
  page: number;
  pageSize: number;
  isLoading: boolean;
  error: string | null;
  createStatus: LoadingState;
  updateStatus: LoadingState;
  deleteStatus: LoadingState;
  filters: {
    status?: StrategyStatus;
    search?: string;
  };
}

// ============ 初始状态 ============

const initialState: StrategiesState = {
  strategies: [],
  currentStrategy: null,
  total: 0,
  page: 1,
  pageSize: 10,
  isLoading: false,
  error: null,
  createStatus: 'idle',
  updateStatus: 'idle',
  deleteStatus: 'idle',
  filters: {},
};

// ============ 异步Thunk ============

/**
 * 获取用户策略列表
 */
export const fetchUserStrategies = createAsyncThunk(
  'strategies/fetchUserStrategies',
  async (
    {
      userId,
      page = 1,
      pageSize = 10,
      status,
    }: {
      userId: string;
      page?: number;
      pageSize?: number;
      status?: StrategyStatus;
    },
    { rejectWithValue }
  ) => {
    try {
      // 统一管理：唯一入口为 strategyManagementService（后台策略管理）
      const items = await strategyManagementService.loadStrategies();
      // 前端服务已按策略类型过滤，状态过滤在前端完成以保持兼容
      let filtered: any[] = items as any[];
      if (status) {
        filtered = filtered.filter((s: any) => String(s.status || '').toLowerCase() === String(status).toLowerCase());
      }
      // 映射为 UserStrategy 以兼容现有类型
      const mapped: UserStrategy[] = filtered.map((s: any) => ({
        id: String(s.id),
        user_id: String(userId),
        strategy_id: String(s.id),
        name: s.name,
        strategy_type: (s.parameters?.strategy_type as any) || 'custom',
        status: (s.status as StrategyStatus) || 'draft',
        is_favorite: false,
        performance_summary: { total_return: 0, total_return_pct: 0, sharpe_ratio: 0, max_drawdown: 0, win_rate: 0, profit_factor: 0, avg_trade_duration: 0, total_trades: 0 },
        tags: s.tags || [],
        created_at: s.created_at || new Date().toISOString(),
        updated_at: s.updated_at || s.created_at || new Date().toISOString(),
        code: s.code || '',
        is_verified: !!(s as any).is_verified,
        is_system: !!(s as any).is_system,
        parameters: s.parameters || {},
        execution_config: s.execution_config,
        cos_url: s.cos_url,
      } as unknown as UserStrategy));
      const start = (page - 1) * pageSize;
      const paged = mapped.slice(start, start + pageSize);
      const resp: PaginatedResponse<UserStrategy> = {
        items: paged,
        total: mapped.length,
        page,
        page_size: pageSize,
        total_pages: Math.max(1, Math.ceil(mapped.length / pageSize)),
      };
      return resp;
    } catch (error: any) {
      return rejectWithValue(error.message || 'Failed to fetch strategies');
    }
  }
);

/**
 * 获取单个策略详情
 */
export const fetchStrategyDetail = createAsyncThunk(
  'strategies/fetchStrategyDetail',
  async (
    { userId, strategyId }: { userId: string; strategyId: string },
    { rejectWithValue }
  ) => {
    try {
      const s: any = await strategyManagementService.getStrategy(strategyId);
      const mapped = {
        id: String(s.id),
        user_id: String(userId),
        strategy_id: String(s.id),
        name: s.name,
        strategy_type: (s.parameters?.strategy_type as any) || 'custom',
        status: (s.status as StrategyStatus) || 'draft',
        is_favorite: false,
        performance_summary: { total_return: 0, total_return_pct: 0, sharpe_ratio: 0, max_drawdown: 0, win_rate: 0, profit_factor: 0, avg_trade_duration: 0, total_trades: 0 },
        tags: s.tags || [],
        created_at: s.created_at || new Date().toISOString(),
        updated_at: s.updated_at || s.created_at || new Date().toISOString(),
        code: s.code || '',
        is_verified: !!s.is_verified,
        is_system: !!s.is_system,
        parameters: s.parameters || {},
        execution_config: s.execution_config,
        cos_url: s.cos_url,
      } as unknown as UserStrategy;
      return mapped;
    } catch (error: any) {
      return rejectWithValue(error.message || 'Failed to fetch strategy detail');
    }
  }
);

/**
 * 创建新策略
 */
export const createStrategy = createAsyncThunk(
  'strategies/createStrategy',
  async (
    { userId, data }: { userId: string; data: Omit<UserStrategy, 'id' | 'created_at' | 'updated_at'> },
    { rejectWithValue }
  ) => {
    try {
      const s: any = await strategyManagementService.saveStrategy({
        name: (data as any).name,
        code: (data as any).code || '',
        description: (data as any).description || '',
        tags: (data as any).tags || [],
        parameters: (data as any).parameters || {},
      } as any);
      const mapped = {
        id: String(s.id),
        user_id: String(userId),
        strategy_id: String(s.id),
        name: s.name,
        strategy_type: (s.parameters?.strategy_type as any) || 'custom',
        status: 'draft' as StrategyStatus,
        is_favorite: false,
        performance_summary: { total_return: 0, total_return_pct: 0, sharpe_ratio: 0, max_drawdown: 0, win_rate: 0, profit_factor: 0, avg_trade_duration: 0, total_trades: 0 },
        tags: s.tags || [],
        created_at: s.created_at || new Date().toISOString(),
        updated_at: s.updated_at || new Date().toISOString(),
        code: s.code || (data as any).code || '',
        parameters: s.parameters || (data as any).parameters || {},
      } as unknown as UserStrategy;
      return mapped;
    } catch (error: any) {
      return rejectWithValue(error.message || 'Failed to create strategy');
    }
  }
);

/**
 * 更新策略
 */
export const updateStrategy = createAsyncThunk(
  'strategies/updateStrategy',
  async (
    {
      userId,
      strategyId,
      data,
    }: {
      userId: string;
      strategyId: string;
      data: StrategyUpdate;
    },
    { rejectWithValue }
  ) => {
    try {
      const s: any = await strategyManagementService.updateStrategy(strategyId, {
        name: (data as any).name,
        description: (data as any).description,
        code: (data as any).code,
        tags: (data as any).tags,
      } as any);
      // 兼容后端返回空的情况，回退为本地合并
      if (s && s.id) {
        return {
          id: String(s.id),
          user_id: String(userId),
          strategy_id: String(s.id),
          name: s.name,
          strategy_type: (s.parameters?.strategy_type as any) || 'custom',
          status: (s.status as StrategyStatus) || 'draft',
          is_favorite: false,
          performance_summary: { total_return: 0, total_return_pct: 0, sharpe_ratio: 0, max_drawdown: 0, win_rate: 0, profit_factor: 0, avg_trade_duration: 0, total_trades: 0 },
          tags: s.tags || [],
          created_at: s.created_at || new Date().toISOString(),
          updated_at: s.updated_at || new Date().toISOString(),
          code: s.code || (data as any).code || '',
          parameters: s.parameters || {},
        } as unknown as UserStrategy;
      }
      return { id: strategyId, user_id: String(userId), strategy_id: String(strategyId), is_favorite: false, performance_summary: { total_return: 0, total_return_pct: 0, sharpe_ratio: 0, max_drawdown: 0, win_rate: 0, profit_factor: 0, avg_trade_duration: 0, total_trades: 0 }, ...(data as any) } as unknown as UserStrategy;
    } catch (error: any) {
      return rejectWithValue(error.message || 'Failed to update strategy');
    }
  }
);

/**
 * 删除策略
 */
export const deleteStrategy = createAsyncThunk(
  'strategies/deleteStrategy',
  async (
    { userId, strategyId }: { userId: string; strategyId: string },
    { rejectWithValue }
  ) => {
    try {
      await strategyManagementService.deleteStrategy(strategyId);
      return { strategyId };
    } catch (error: any) {
      return rejectWithValue(error.message || 'Failed to delete strategy');
    }
  }
);

/**
 * 启用策略
 */
export const enableStrategy = createAsyncThunk(
  'strategies/enableStrategy',
  async (
    { userId, strategyId }: { userId: string; strategyId: string },
    { rejectWithValue }
  ) => {
    try {
      await strategyManagementService.activateStrategy(strategyId);
      const s: any = await strategyManagementService.getStrategy(strategyId);
      return {
        id: String(s.id),
        name: s.name,
        status: 'live_trading' as StrategyStatus,
        updated_at: s.updated_at || new Date().toISOString(),
      } as UserStrategy;
    } catch (error: any) {
      return rejectWithValue(error.message || 'Failed to enable strategy');
    }
  }
);

/**
 * 禁用策略
 */
export const disableStrategy = createAsyncThunk(
  'strategies/disableStrategy',
  async (
    { userId, strategyId }: { userId: string; strategyId: string },
    { rejectWithValue }
  ) => {
    try {
      await strategyManagementService.deactivateStrategy(strategyId);
      const s: any = await strategyManagementService.getStrategy(strategyId);
      return {
        id: String(s.id),
        name: s.name,
        status: 'draft' as StrategyStatus,
        updated_at: s.updated_at || new Date().toISOString(),
      } as UserStrategy;
    } catch (error: any) {
      return rejectWithValue(error.message || 'Failed to disable strategy');
    }
  }
);

// ============ Slice定义 ============

export const strategiesSlice = createSlice({
  name: 'strategies',
  initialState,
  reducers: {
    /**
     * 重置状态
     */
    resetStrategies: (state) => {
      return initialState;
    },

    /**
     * 清除错误
     */
    clearError: (state) => {
      state.error = null;
    },

    /**
     * 设置当前策略
     */
    setCurrentStrategy: (state, action: PayloadAction<UserStrategy | null>) => {
      state.currentStrategy = action.payload;
    },

    /**
     * 更新筛选条件
     */
    updateFilters: (
      state,
      action: PayloadAction<{ status?: StrategyStatus; search?: string }>
    ) => {
      state.filters = { ...state.filters, ...action.payload };
      state.page = 1; // 重置到第一页
    },

    /**
     * 清除筛选条件
     */
    clearFilters: (state) => {
      state.filters = {};
      state.page = 1;
    },

    /**
     * 设置分页
     */
    setPage: (state, action: PayloadAction<number>) => {
      state.page = action.payload;
    },

    /**
     * 重置创建状态
     */
    resetCreateStatus: (state) => {
      state.createStatus = 'idle';
    },

    /**
     * 重置更新状态
     */
    resetUpdateStatus: (state) => {
      state.updateStatus = 'idle';
    },

    /**
     * 重置删除状态
     */
    resetDeleteStatus: (state) => {
      state.deleteStatus = 'idle';
    },
  },
  extraReducers: (builder) => {
    // ===== 获取策略列表 =====
    builder
      .addCase(fetchUserStrategies.pending, (state) => {
        state.isLoading = true;
        state.error = null;
      })
      .addCase(fetchUserStrategies.fulfilled, (state, action) => {
        state.isLoading = false;
        state.strategies = action.payload.items;
        state.total = action.payload.total;
        state.page = action.payload.page;
        state.pageSize = action.payload.page_size;
        state.error = null;
      })
      .addCase(fetchUserStrategies.rejected, (state, action) => {
        state.isLoading = false;
        state.error = action.payload as string;
      });

    // ===== 获取策略详情 =====
    builder
      .addCase(fetchStrategyDetail.pending, (state) => {
        state.isLoading = true;
        state.error = null;
      })
      .addCase(fetchStrategyDetail.fulfilled, (state, action) => {
        state.isLoading = false;
        state.currentStrategy = action.payload;
        state.error = null;
      })
      .addCase(fetchStrategyDetail.rejected, (state, action) => {
        state.isLoading = false;
        state.error = action.payload as string;
      });

    // ===== 创建策略 =====
    builder
      .addCase(createStrategy.pending, (state) => {
        state.createStatus = 'loading';
        state.error = null;
      })
      .addCase(createStrategy.fulfilled, (state, action) => {
        state.createStatus = 'success';
        state.strategies.unshift(action.payload); // 添加到列表开头
        state.total += 1;
        state.error = null;
      })
      .addCase(createStrategy.rejected, (state, action) => {
        state.createStatus = 'error';
        state.error = action.payload as string;
      });

    // ===== 更新策略 =====
    builder
      .addCase(updateStrategy.pending, (state) => {
        state.updateStatus = 'loading';
        state.error = null;
      })
      .addCase(updateStrategy.fulfilled, (state, action) => {
        state.updateStatus = 'success';
        // 更新列表中的策略
        const index = state.strategies.findIndex((s) => s.id === action.payload.id);
        if (index !== -1) {
          state.strategies[index] = action.payload;
        }
        // 更新当前策略
        if (state.currentStrategy?.id === action.payload.id) {
          state.currentStrategy = action.payload;
        }
        state.error = null;
      })
      .addCase(updateStrategy.rejected, (state, action) => {
        state.updateStatus = 'error';
        state.error = action.payload as string;
      });

    // ===== 删除策略 =====
    builder
      .addCase(deleteStrategy.pending, (state) => {
        state.deleteStatus = 'loading';
        state.error = null;
      })
      .addCase(deleteStrategy.fulfilled, (state, action) => {
        state.deleteStatus = 'success';
        // 从列表中移除
        state.strategies = state.strategies.filter(
          (s) => s.id !== action.payload.strategyId
        );
        state.total -= 1;
        // 清除当前策略（如果是删除的策略）
        if (state.currentStrategy?.id === action.payload.strategyId) {
          state.currentStrategy = null;
        }
        state.error = null;
      })
      .addCase(deleteStrategy.rejected, (state, action) => {
        state.deleteStatus = 'error';
        state.error = action.payload as string;
      });

    // ===== 启用策略 =====
    builder
      .addCase(enableStrategy.pending, (state) => {
        state.updateStatus = 'loading';
        state.error = null;
      })
      .addCase(enableStrategy.fulfilled, (state, action) => {
        state.updateStatus = 'success';
        // 更新列表中的策略
        const index = state.strategies.findIndex((s) => s.id === action.payload.id);
        if (index !== -1) {
          state.strategies[index] = action.payload;
        }
        // 更新当前策略
        if (state.currentStrategy?.id === action.payload.id) {
          state.currentStrategy = action.payload;
        }
        state.error = null;
      })
      .addCase(enableStrategy.rejected, (state, action) => {
        state.updateStatus = 'error';
        state.error = action.payload as string;
      });

    // ===== 禁用策略 =====
    builder
      .addCase(disableStrategy.pending, (state) => {
        state.updateStatus = 'loading';
        state.error = null;
      })
      .addCase(disableStrategy.fulfilled, (state, action) => {
        state.updateStatus = 'success';
        // 更新列表中的策略
        const index = state.strategies.findIndex((s) => s.id === action.payload.id);
        if (index !== -1) {
          state.strategies[index] = action.payload;
        }
        // 更新当前策略
        if (state.currentStrategy?.id === action.payload.id) {
          state.currentStrategy = action.payload;
        }
        state.error = null;
      })
      .addCase(disableStrategy.rejected, (state, action) => {
        state.updateStatus = 'error';
        state.error = action.payload as string;
      });
  },
});

// ============ 导出Actions ============

export const {
  resetStrategies,
  clearError,
  setCurrentStrategy,
  updateFilters,
  clearFilters,
  setPage,
  resetCreateStatus,
  resetUpdateStatus,
  resetDeleteStatus,
} = strategiesSlice.actions;

// ============ Selectors ============

export const selectStrategies = (state: { strategies: StrategiesState }) =>
  state.strategies.strategies;
export const selectCurrentStrategy = (state: { strategies: StrategiesState }) =>
  state.strategies.currentStrategy;
export const selectStrategiesTotal = (state: { strategies: StrategiesState }) =>
  state.strategies.total;
export const selectStrategiesPage = (state: { strategies: StrategiesState }) =>
  state.strategies.page;
export const selectStrategiesPageSize = (state: { strategies: StrategiesState }) =>
  state.strategies.pageSize;
export const selectStrategiesLoading = (state: { strategies: StrategiesState }) =>
  state.strategies.isLoading;
export const selectStrategiesError = (state: { strategies: StrategiesState }) =>
  state.strategies.error;
export const selectCreateStatus = (state: { strategies: StrategiesState }) =>
  state.strategies.createStatus;
export const selectUpdateStatus = (state: { strategies: StrategiesState }) =>
  state.strategies.updateStatus;
export const selectDeleteStatus = (state: { strategies: StrategiesState }) =>
  state.strategies.deleteStatus;
export const selectFilters = (state: { strategies: StrategiesState }) =>
  state.strategies.filters;

// ============ 导出Reducer ============

export default strategiesSlice.reducer;
