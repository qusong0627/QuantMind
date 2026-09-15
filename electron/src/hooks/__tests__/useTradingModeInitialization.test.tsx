import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { Provider } from 'react-redux';
import React from 'react';
import store from '../../store';
import { setAuthState } from '../../features/auth/store/authSlice';
import { setTradingMode, selectTradingMode } from '../../store/slices/uiSlice';
import { useTradingModeInitialization } from '../useTradingModeInitialization';

const PREF_KEY = 'qm:trading_mode_pref';

const renderInit = () =>
    renderHook(() => useTradingModeInitialization(), {
        wrapper: ({ children }) => <Provider store={store}>{children}</Provider>,
    });

describe('useTradingModeInitialization', () => {
    beforeEach(() => {
        localStorage.clear();
        store.dispatch(setAuthState({ isAuthenticated: true, user: null, token: 'test-token' }));
        store.dispatch(setTradingMode('simulation'));
    });

    it('保存过 real 偏好时恢复为实盘', () => {
        localStorage.setItem(PREF_KEY, 'real');

        renderInit();

        expect(selectTradingMode(store.getState())).toBe('real');
    });

    it('保存过 simulation 偏好时恢复为模拟盘', () => {
        localStorage.setItem(PREF_KEY, 'simulation');
        store.dispatch(setTradingMode('real'));

        renderInit();

        expect(selectTradingMode(store.getState())).toBe('simulation');
    });

    it('偏好值非法时不做隐式切换', () => {
        localStorage.setItem(PREF_KEY, 'garbage');
        store.dispatch(setTradingMode('real'));

        renderInit();

        expect(selectTradingMode(store.getState())).toBe('real');
    });

    it('未登录时不恢复偏好', () => {
        localStorage.setItem(PREF_KEY, 'real');
        store.dispatch(setAuthState({ isAuthenticated: false, user: null, token: null }));

        renderInit();

        expect(selectTradingMode(store.getState())).toBe('simulation');
    });
});