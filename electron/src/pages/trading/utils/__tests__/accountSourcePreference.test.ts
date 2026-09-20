import { beforeEach, describe, expect, test, vi } from 'vitest';
import {
    getPreferredAccountSource,
    setPreferredAccountSource,
    subscribeAccountSourceChange,
} from '../accountSourcePreference';

describe('accountSourcePreference', () => {
    beforeEach(() => {
        localStorage.clear();
    });

    test('默认无偏好 → null（= 跟随当前交易券商）', () => {
        expect(getPreferredAccountSource('CN')).toBeNull();
    });

    test('设置后按市场分键读取，互不串台', () => {
        setPreferredAccountSource('tdx_bridge', 'CN');

        expect(getPreferredAccountSource('CN')).toBe('tdx_bridge');
        expect(getPreferredAccountSource('HK')).toBeNull();
        // 市场缺省按 CN 读（持仓监控只有 CN 有实盘源）
        expect(getPreferredAccountSource()).toBe('tdx_bridge');
    });

    test('传空值 = 清除偏好（回到跟随交易券商）', () => {
        setPreferredAccountSource('qmt_exec', 'CN');
        setPreferredAccountSource(null, 'CN');

        expect(getPreferredAccountSource('CN')).toBeNull();
        expect(localStorage.getItem('qm:trading:accountSource:CN')).toBeNull();
    });

    test('设置时广播，让正在轮询的页面立即重取（不必等 5s 周期）', () => {
        const listener = vi.fn();
        const unsubscribe = subscribeAccountSourceChange(listener);

        setPreferredAccountSource('qmt_exec', 'CN');
        expect(listener).toHaveBeenCalledTimes(1);

        unsubscribe();
        setPreferredAccountSource('tdx_bridge', 'CN');
        expect(listener).toHaveBeenCalledTimes(1); // 退订后不再通知
    });

    test('单个订阅者抛异常不影响其它订阅者', () => {
        const good = vi.fn();
        const unsubscribeBad = subscribeAccountSourceChange(() => {
            throw new Error('boom');
        });
        const unsubscribeGood = subscribeAccountSourceChange(good);

        expect(() => setPreferredAccountSource('qmt_exec', 'CN')).not.toThrow();
        expect(good).toHaveBeenCalledTimes(1);

        unsubscribeBad();
        unsubscribeGood();
    });
});
