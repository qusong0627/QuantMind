/**
 * `MarketSelector` 行为护栏（2026-10-08 补：此前本组件零测试，而「实盘交易」顶栏
 * 现在也挂了它一份，见 `features/local-live/LiveTradingPage.tsx`）。
 *
 * 钉的是**派发**不是外观：点一下就把整个控制台（顶栏账户、持仓监控、交易记录、
 * 设置里的券商卡）换到另一个市场，所以「点了真的落到全局 store」「高亮跟着 store
 * 走」必须有人看着，而不是靠肉眼看按钮变色。
 *
 * CRYPTO 屏蔽态在隔壁文件（`vi.mock` 是文件级的，两态不能共存于一个文件）。
 */

import { describe, it, expect, afterEach } from 'vitest';
import { render, fireEvent, cleanup, act } from '@testing-library/react';
import { Provider } from 'react-redux';
import React from 'react';

import store from '../../../store';
import { setMarket, selectCurrentMarket } from '../../../store/slices/uiSlice';
import { MarketSelector } from '../MarketSelector';

const renderSelector = () =>
    render(
        <Provider store={store}>
            <MarketSelector />
        </Provider>,
    );

const radios = () => Array.from(document.querySelectorAll<HTMLElement>('[role="radio"]'));
const labelOf = (el: HTMLElement) => (el.textContent || '').trim();
const labels = () => radios().map(labelOf);
const checkedLabel = () =>
    ((r) => (r ? labelOf(r) : null))(radios().find((el) => el.getAttribute('aria-checked') === 'true'));

const clickMarket = async (label: string) => {
    const el = radios().find((r) => labelOf(r) === label);
    if (!el) throw new Error(`切换器里没有「${label}」这一项`);
    // 异步 act：react-redux 的订阅更新落在微任务里，同步 fireEvent 之后才落地，
    // 不在这里收干净就会把「not wrapped in act」噪声打进 stderr
    await act(async () => {
        fireEvent.click(el);
    });
};

describe('MarketSelector 市场切换', () => {
    afterEach(() => {
        // 先卸载再还原：带着挂载中的订阅去 dispatch 又是一条 act 噪声
        cleanup();
        // store 是模块级单例：不还原会把市场偏好漏给同文件后续用例
        store.dispatch(setMarket('CN'));
        localStorage.clear();
    });

    it('开发构建下渲染全部启用市场，当前市场高亮', () => {
        // vitest 里 import.meta.env.PROD 为假 → ENABLE_CRYPTO 为真（生产构建会少「区块链」，
        // 那一态由 cryptoGate 文件覆盖）。顺序即用户看到的顺序。
        store.dispatch(setMarket('CN'));
        renderSelector();

        expect(labels()).toEqual(['A股', '港股', '美股', '区块链', '期货']);
        expect(checkedLabel()).toBe('A股');
    });

    it('点「港股」→ 全局市场切到 HK，高亮随之移动', async () => {
        store.dispatch(setMarket('CN'));
        renderSelector();

        await clickMarket('港股');

        expect(selectCurrentMarket(store.getState())).toBe('HK');
        expect(checkedLabel()).toBe('港股');
        // 是切换不是追加：项数不变
        expect(labels()).toHaveLength(5);
    });

    it('A股 / 港股 / 美股 三向互切，每次都落在 store 上', async () => {
        store.dispatch(setMarket('CN'));
        renderSelector();

        await clickMarket('美股');
        expect(selectCurrentMarket(store.getState())).toBe('US');
        expect(checkedLabel()).toBe('美股');

        await clickMarket('A股');
        expect(selectCurrentMarket(store.getState())).toBe('CN');
        expect(checkedLabel()).toBe('A股');

        await clickMarket('港股');
        expect(selectCurrentMarket(store.getState())).toBe('HK');
        expect(checkedLabel()).toBe('港股');
    });

    it('容器带可访问名「市场切换」（不靠颜色表达当前市场）', () => {
        renderSelector();

        expect(
            document.querySelector('[role="radiogroup"]')?.getAttribute('aria-label'),
        ).toBe('市场切换');
    });
});
