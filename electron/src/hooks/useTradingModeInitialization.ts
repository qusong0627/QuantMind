import { useEffect, useCallback, useRef } from 'react';
import { useAppDispatch, useAppSelector } from '../store';
import { setTradingMode } from '../store/slices/uiSlice';

export const useTradingModeInitialization = () => {
    const dispatch = useAppDispatch();
    const tradingMode = useAppSelector((state) => state.ui.tradingMode);
    const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
    const initializedRef = useRef(false);

    const initializeMode = useCallback(() => {
        if (!isAuthenticated || initializedRef.current) return;

        // 实盘入口已在前端隐藏，统一固定为模拟盘，
        // 忽略历史 localStorage 中可能存在的 real 偏好，避免页面仍停留于实盘态。
        if (tradingMode !== 'simulation') {
            dispatch(setTradingMode('simulation'));
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
