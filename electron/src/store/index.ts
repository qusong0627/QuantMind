/**
 * Redux Store 配置
 * 使用 Redux Toolkit 进行状态管理
 */

import { configureStore } from '@reduxjs/toolkit';
import { useDispatch, useSelector, TypedUseSelectorHook } from 'react-redux';

// 导入各个切片
import authSlice from './slices/authSlice';
import aiStrategySlice from './slices/aiStrategySlice';
import marketDataSlice from './slices/marketDataSlice';
import backtestSlice from './slices/backtestSlice';
import uiSlice from './slices/uiSlice';
import toastSlice from './slices/toastSlice';

// 导入用户中心模块切片
import profileReducer from '../features/user-center/store/profileSlice';
import strategiesReducer from '../features/user-center/store/strategiesSlice';
import portfoliosReducer from '../features/user-center/store/portfoliosSlice';
import configReducer from '../features/user-center/store/configSlice';

// 导入OpenClaw模块切片
import { chatReducer, taskReducer } from '../features/quantbot/store';

// 配置 Store
const store = configureStore({
  reducer: {
    auth: authSlice,
    aiStrategy: aiStrategySlice,
    marketData: marketDataSlice,
    backtest: backtestSlice,
    ui: uiSlice,
    toasts: toastSlice,
    // 用户中心模块
    profile: profileReducer,
    strategies: strategiesReducer,
    portfolios: portfoliosReducer,
    config: configReducer,
    // QuantBot模块
    quantbotChat: chatReducer,
    quantbotTask: taskReducer,
  },
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware({
      serializableCheck: {
        ignoredActions: [
          'persist/PERSIST',
          'persist/REHYDRATE',
        ],
        ignoredPaths: [
          'register',
          'rehydrate',
        ],
      },
    }),
  devTools: process.env.NODE_ENV !== 'production',
});

export default store;

// 类型定义
export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;

// 类型化的 hooks
export const useAppDispatch = () => useDispatch<AppDispatch>();
export const useAppSelector: TypedUseSelectorHook<RootState> = useSelector;

// 注意: store 已通过默认导出直接导出上方的 configureStore 返回值
