import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import { isMarketEnabled } from '../../config/marketFlags';
import { isLiveTradingEnabled } from '../../config/tradingFlags';

export type AppMarket = 'CN' | 'US' | 'HK' | 'CRYPTO' | 'FUTURES';

export type UiMode = 'simple' | 'professional';

export interface UIState {
  theme: 'light' | 'dark';
  sidebarOpen: boolean;
  notifications: any[];
  tradingMode: 'real' | 'simulation';
  currentMarket: AppMarket;
  /** T-FE-02 简单/专业模式：简单是默认视图（不是功能裁剪，机构能力收敛进专业模式与下钻层） */
  uiMode: UiMode;
}

const TRADING_MODE_PREF_KEY = 'qm:trading_mode_pref';
const MARKET_PREF_KEY = 'qm:current_market';
const UI_MODE_PREF_KEY = 'qm:ui_mode_pref';

const savedMode = localStorage.getItem(TRADING_MODE_PREF_KEY);
// 未显式保存过偏好时默认模拟盘：实盘态须由用户主动切换一次（切换会写入偏好）。
// 实盘开关关闭时**即便 localStorage 存着 'real' 也不认**。
const initialTradingMode: 'real' | 'simulation' =
  savedMode === 'real' && isLiveTradingEnabled() ? 'real' : 'simulation';
// 把失效的偏好键回写掉（与下方市场偏好同款）。
//
// 这里**必须写掉而不是只忽略**：偏好会跨版本存活，若把 'real' 留在 localStorage 里，
// 那么运维哪天把开关打开，所有存量用户会在下一次刷新时**自动落进实盘态**——
// 切到真钱必须是一次当场的显式动作，不能由一个陈年偏好替用户做决定。
if (savedMode === 'real' && !isLiveTradingEnabled()) {
  localStorage.setItem(TRADING_MODE_PREF_KEY, 'simulation');
}

const savedMarket = localStorage.getItem(MARKET_PREF_KEY);
const validMarkets: AppMarket[] = (['CN', 'US', 'HK', 'CRYPTO', 'FUTURES'] as AppMarket[]).filter((m) => isMarketEnabled(m));
const initialMarket: AppMarket =
  validMarkets.includes(savedMarket as AppMarket) ? (savedMarket as AppMarket) : 'CN';
// 确保市场偏好键始终有值：策略库/回测中心等按 localStorage 过滤策略列表的模块，
// 键缺失时会退化为「不过滤」从而把港股策略混进 A 股视图
if (!validMarkets.includes(savedMarket as AppMarket)) {
  localStorage.setItem(MARKET_PREF_KEY, initialMarket);
}

// 未显式保存过偏好时默认简单模式（前端设计 §一.2：简单模式为默认视图）
const savedUiMode = localStorage.getItem(UI_MODE_PREF_KEY);
const initialUiMode: UiMode = savedUiMode === 'professional' ? 'professional' : 'simple';

const initialState: UIState = {
  theme: 'light',
  sidebarOpen: true,
  notifications: [],
  tradingMode: initialTradingMode,
  currentMarket: initialMarket,
  uiMode: initialUiMode,
};

const uiSlice = createSlice({
  name: 'ui',
  initialState,
  reducers: {
    setTheme: (state, action: PayloadAction<'light' | 'dark'>) => {
      state.theme = action.payload;
    },
    toggleSidebar: (state) => {
      state.sidebarOpen = !state.sidebarOpen;
    },
    addNotification: (state, action: PayloadAction<any>) => {
      state.notifications.push(action.payload);
    },
    removeNotification: (state, action: PayloadAction<string>) => {
      state.notifications = state.notifications.filter(n => n.id !== action.payload);
    },
    setTradingMode: (state, action: PayloadAction<'real' | 'simulation'>) => {
      // 实盘开关关闭时钳制为模拟盘：reducer 是最后一道，绕过 hook 直接 dispatch
      // （或持久化状态回放）都不该能把界面带进实盘态
      state.tradingMode =
        action.payload === 'real' && !isLiveTradingEnabled() ? 'simulation' : action.payload;
    },
    setMarket: (state, action: PayloadAction<AppMarket>) => {
      const market = isMarketEnabled(action.payload) ? action.payload : 'CN';
      state.currentMarket = market;
      localStorage.setItem(MARKET_PREF_KEY, market);
    },
    setUiMode: (state, action: PayloadAction<UiMode>) => {
      state.uiMode = action.payload === 'professional' ? 'professional' : 'simple';
      localStorage.setItem(UI_MODE_PREF_KEY, state.uiMode);
    },
  },
});

export const { setTheme, toggleSidebar, addNotification, removeNotification, setTradingMode, setMarket, setUiMode } = uiSlice.actions;

// Selectors
export const selectCurrentMarket = (state: { ui: UIState }) => state.ui.currentMarket;
export const selectTradingMode = (state: { ui: UIState }) => state.ui.tradingMode;
export const selectUiMode = (state: { ui: UIState }) => state.ui.uiMode;

export default uiSlice.reducer;
