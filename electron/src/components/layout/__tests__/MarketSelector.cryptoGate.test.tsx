/**
 * `MarketSelector` 的市场屏蔽态：CRYPTO 关掉后「区块链」必须**整项消失**。
 *
 * 为什么单开一个文件：`vi.mock` 是文件级的，屏蔽态与默认态不能在同一个文件里共存。
 * 为什么值得钉：选项表若被谁改回写死的五连（绕过 `marketFlags`），生产构建里
 * 「区块链」会重新冒出来 —— 后端 `ENABLE_CRYPTO=false` 时那是个查不到数据、
 * 下单也没有通道的死市场，用户只会看到一屏空。
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render } from '@testing-library/react';
import { Provider } from 'react-redux';
import React from 'react';

vi.mock('../../../config/marketFlags', () => ({
    ENABLE_CRYPTO: false,
    isMarketEnabled: (market: string) => market !== 'CRYPTO',
}));

import store from '../../../store';
import { setMarket } from '../../../store/slices/uiSlice';
import { MarketSelector } from '../MarketSelector';

const labels = () =>
    Array.from(document.querySelectorAll<HTMLElement>('[role="radio"]')).map((el) =>
        (el.textContent || '').trim(),
    );

describe('MarketSelector 市场屏蔽态', () => {
    afterEach(() => {
        store.dispatch(setMarket('CN'));
        localStorage.clear();
    });

    it('CRYPTO 被屏蔽时「区块链」不出现，其余市场照常', () => {
        render(
            <Provider store={store}>
                <MarketSelector />
            </Provider>,
        );

        expect(labels()).toEqual(['A股', '港股', '美股', '期货']);
    });
});
