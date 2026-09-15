import { useEffect, useCallback, useRef } from 'react';
import { useAppDispatch, useAppSelector } from '../store';
import { setTradingMode } from '../store/slices/uiSlice';

const TRADING_MODE_PREF_KEY = 'qm:trading_mode_pref';

export const useTradingModeInitialization = () => {
    const dispatch = useAppDispatch();
    const tradingMode = useAppSelector((state) => state.ui.tradingMode);
    const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
    const initializedRef = useRef(false);

    const initializeMode = useCallback(() => {
        if (!isAuthenticated || initializedRef.current) return;

        // 只恢复用户显式保存的偏好，不再根据账户状态做隐式模式切换。
        const savedMode = localStorage.getItem(TRADING_MODE_PREF_KEY);
        if (savedMode === 'real' || savedMode === 'simulation') {
            if (tradingMode !== savedMode) {
                dispatch(setTradingMode(savedMode as 'real' | 'simulation'));
            }
        }

        initializedRef.current = true;
    }, [isAuthenticated, tradingMode, dispatch]);

    useEffect(() => {
        if (isAuthenticated) {
            initializeMode();
        } else {
            initializedRef.current = false;
        }
    }, [isAuthenticated, initializeMode]);
};
