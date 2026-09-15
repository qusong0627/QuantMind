import { describe, it, expect, beforeEach, vi } from 'vitest';

const PREF_KEY = 'qm:trading_mode_pref';

const loadInitialMode = async () => {
    const { default: uiReducer } = await import('../slices/uiSlice');
    return uiReducer(undefined, { type: '@@INIT' }).tradingMode;
};

describe('uiSlice 交易模式初始值', () => {
    beforeEach(() => {
        vi.resetModules();
        localStorage.clear();
    });

    it('未保存偏好时默认模拟盘', async () => {
        await expect(loadInitialMode()).resolves.toBe('simulation');
    });

    it('保存过 real 偏好时恢复实盘', async () => {
        localStorage.setItem(PREF_KEY, 'real');

        await expect(loadInitialMode()).resolves.toBe('real');
    });
});