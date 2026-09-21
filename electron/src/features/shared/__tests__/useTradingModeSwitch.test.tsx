/**
 * 交易模式切换的唯一收口点，在**实盘开关关闭**（生产默认态）时的行为。
 *
 * 这一层是「隐藏实盘」最容易漏的地方：`uiSlice` 的初始值与 reducer 已经钳制过一道，
 * 但顶栏与交易页两个入口都走本 hook，它若不归一，界面照样能把用户显示成实盘态。
 * 所以这里的断言都是「对外必须是 simulation」，而不是「内部字段等于什么」。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import React from 'react';

type TradingModePref = 'real' | 'simulation';

const PREF_KEY = 'qm:trading_mode_pref';

/**
 * 造一个 ui 里已经是 `real` 的 store。
 *
 * 不能 `dispatch(setTradingMode('real'))` —— 开关关闭时 reducer 会把那次 dispatch
 * 钳掉，用例就退化成「测钳制」而不是「测归一」。preloadedState 绕过 reducer，
 * 模拟的正是「store 里存着历史实盘态」这一种真实情形（持久化回放）。
 */
async function makeStoreWithMode(mode: TradingModePref) {
    const { default: uiReducer } = await import('../../../store/slices/uiSlice');
    return configureStore({
        reducer: { ui: uiReducer },
        preloadedState: {
            ui: {
                theme: 'light' as const,
                sidebarOpen: true,
                notifications: [],
                tradingMode: mode,
                currentMarket: 'CN' as const,
                uiMode: 'simple' as const,
            },
        },
    });
}

async function renderSwitch(flag: 'true' | 'false', storeMode: TradingModePref = 'simulation') {
    vi.resetModules();
    vi.stubEnv('VITE_ENABLE_REAL_TRADING', flag);
    const { useTradingModeSwitch } = await import('../useTradingModeSwitch');
    const store = await makeStoreWithMode(storeMode);
    const view = renderHook(() => useTradingModeSwitch(), {
        wrapper: ({ children }: { children: React.ReactNode }) => (
            <Provider store={store}>{children}</Provider>
        ),
    });
    return { ...view, store };
}

describe('useTradingModeSwitch（实盘开关关闭）', () => {
    beforeEach(() => localStorage.clear());
    afterEach(() => vi.unstubAllEnvs());

    it('store 里是 real 时对外仍报 simulation', async () => {
        // Arrange / Act
        const { result } = await renderSwitch('false', 'real');

        // Assert
        expect(result.current.tradingMode).toBe('simulation');
    });

    it('requestSwitch("real") 是空操作：不改状态、不写偏好', async () => {
        // Arrange
        const { result, store } = await renderSwitch('false');

        // Act
        act(() => result.current.requestSwitch('real'));

        // Assert
        expect(result.current.tradingMode).toBe('simulation');
        expect(store.getState().ui.tradingMode).toBe('simulation');
        // 关键：连确认卡都不该弹。弹一张「我已知悉，切换实盘」却切不过去的卡，
        // 比没反应更像坏了
        expect(result.current.confirmModal.props.open).toBe(false);
    });

    it('两个方向都是空操作，偏好键始终不被写', async () => {
        // Arrange
        const { result, store } = await renderSwitch('false');

        // Act：轮着切，两个方向都不该留下痕迹
        act(() => result.current.requestSwitch('simulation'));
        act(() => result.current.requestSwitch('real'));

        // Assert：切 simulation 走 `mode === tradingMode` 提前返回（本来就在模拟盘），
        // 切 real 被开关吞掉 —— 结果是**这个 hook 在开关关闭时完全不写任何状态**，
        // 比「只挡住 real 那一边」更强
        expect(localStorage.getItem(PREF_KEY)).toBeNull();
        expect(store.getState().ui.tradingMode).toBe('simulation');
    });
});

describe('useTradingModeSwitch（实盘开关打开）', () => {
    beforeEach(() => localStorage.clear());
    afterEach(() => vi.unstubAllEnvs());

    it('store 里是 real 时如实报 real', async () => {
        // Arrange / Act
        const { result } = await renderSwitch('true', 'real');

        // Assert
        expect(result.current.tradingMode).toBe('real');
    });

    it('切实盘先弹确认卡，确认前不动状态', async () => {
        // Arrange
        const { result, store } = await renderSwitch('true');

        // Act
        act(() => result.current.requestSwitch('real'));

        // Assert：两步确认——未确认前状态与偏好都不许变
        expect(result.current.confirmModal.props.open).toBe(true);
        expect(store.getState().ui.tradingMode).toBe('simulation');
        expect(localStorage.getItem(PREF_KEY)).toBeNull();
    });
});
