import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

const PREF_KEY = 'qm:trading_mode_pref';

const loadInitialMode = async () => {
    const { default: uiReducer } = await import('../slices/uiSlice');
    return uiReducer(undefined, { type: '@@INIT' }).tradingMode;
};

/** 取新模块里的 reducer，用于直接 dispatch 绕过 hook */
const loadReducer = async () => (await import('../slices/uiSlice')).default;

describe('uiSlice 交易模式初始值', () => {
    beforeEach(() => {
        vi.resetModules();
        localStorage.clear();
    });

    afterEach(() => {
        vi.unstubAllEnvs();
    });

    it('未保存偏好时默认模拟盘', async () => {
        await expect(loadInitialMode()).resolves.toBe('simulation');
    });

    it('保存过 real 偏好时恢复实盘', async () => {
        localStorage.setItem(PREF_KEY, 'real');

        await expect(loadInitialMode()).resolves.toBe('real');
    });

    it('实盘开关关闭时，即便存着 real 偏好也初始化为模拟盘', async () => {
        // 旧偏好可能来自开关打开时的会话，不清掉会让隐藏的实盘态在新构建里复活
        vi.stubEnv('VITE_ENABLE_REAL_TRADING', 'false');
        localStorage.setItem(PREF_KEY, 'real');

        await expect(loadInitialMode()).resolves.toBe('simulation');
    });

    it('开关关闭时把失效的 real 偏好回写掉，别留着等下个版本复活', async () => {
        vi.stubEnv('VITE_ENABLE_REAL_TRADING', 'false');
        localStorage.setItem(PREF_KEY, 'real');

        await loadInitialMode();

        expect(localStorage.getItem(PREF_KEY)).toBe('simulation');
    });
});

describe('uiSlice setTradingMode 钳制', () => {
    beforeEach(() => {
        vi.resetModules();
        localStorage.clear();
    });

    afterEach(() => {
        vi.unstubAllEnvs();
    });

    it('开关关闭时 dispatch("real") 被钳制为 simulation', async () => {
        // reducer 是最后一道：绕过 hook 直接 dispatch、或持久化状态回放，
        // 都不该能把界面带进实盘态
        vi.stubEnv('VITE_ENABLE_REAL_TRADING', 'false');
        const reducer = await loadReducer();

        const next = reducer(undefined, { type: 'ui/setTradingMode', payload: 'real' });

        expect(next.tradingMode).toBe('simulation');
    });

    it('开关打开时 dispatch("real") 正常生效', async () => {
        vi.stubEnv('VITE_ENABLE_REAL_TRADING', 'true');
        const reducer = await loadReducer();

        const next = reducer(undefined, { type: 'ui/setTradingMode', payload: 'real' });

        expect(next.tradingMode).toBe('real');
    });
});