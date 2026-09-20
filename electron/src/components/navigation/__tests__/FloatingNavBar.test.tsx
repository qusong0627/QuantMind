/**
 * 底部导航「交易」栏目随交易模式改名。
 *
 * 用户原话：「模拟交易如果我点了实盘，下面栏目 也要变动成实盘交易。我选的模拟交易就
 * 模拟交易不动。」——底部导航是常驻的，用户据此确认自己现在在哪个账户里操作；
 * 切了实盘还写着「模拟交易」，就是让人以为真金白银的单只是模拟单。
 *
 * 文案不另起字面量：与交易页内所有模式文案同取 `modeCopy().full`（唯一事实源），
 * 所以这里断言的是「栏目名 == 模式全称」，而不是把「实盘交易」再抄一遍。
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { Provider } from 'react-redux';
import React from 'react';
import store from '../../../store';
import { setTradingMode } from '../../../store/slices/uiSlice';
import { FloatingNavBar } from '../FloatingNavBar';
import { modeCopy, containsSimulationWording } from '../../../pages/trading/utils/tradingModeCopy';

const renderNav = () =>
  render(
    <Provider store={store}>
      <FloatingNavBar current="trading" onChange={() => {}} />
    </Provider>,
  );

/** 只取底部导航里的栏目名，避免把页面别处的「模拟交易」一起捞进来 */
const dockLabels = (): string[] =>
  Array.from(document.querySelectorAll('.dock-label')).map((el) => (el.textContent || '').trim());

describe('FloatingNavBar 交易栏目名', () => {
  beforeEach(() => {
    store.dispatch(setTradingMode('simulation'));
  });

  afterEach(() => {
    cleanup();
  });

  it('模拟模式下栏目名是「模拟交易」', () => {
    renderNav();

    expect(dockLabels()).toContain('模拟交易');
    expect(dockLabels()).not.toContain('实盘交易');
  });

  it('切到实盘后栏目名变成「实盘交易」', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    expect(dockLabels()).toContain('实盘交易');
  });

  it('实盘下底部导航不再出现「模拟」字样', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    const offending = dockLabels().filter(containsSimulationWording);
    expect(offending).toEqual([]);
  });

  it('实盘下「回测中心」等其它栏目名不受影响（改的只是交易栏目）', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    const labels = dockLabels();
    expect(labels).toContain('回测中心');
    expect(labels).toContain('模型训练');
    expect(labels).toHaveLength(12); // 非管理员：12 个栏目，无「后台管理」
  });

  it('栏目名与模式全称同源（防止有人再抄一份字面量）', () => {
    for (const mode of ['simulation', 'real'] as const) {
      store.dispatch(setTradingMode(mode));
      renderNav();

      expect(dockLabels()).toContain(modeCopy(mode).full);
      cleanup();
    }
  });

  it('当前栏目高亮仍然落在交易栏目上（改名不改行为）', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    const active = document.querySelector('.dock-item.active .dock-label');
    expect((active?.textContent || '').trim()).toBe('实盘交易');
    expect(active?.closest('[aria-current="page"]')).not.toBeNull();
  });

  it('渲染时 title 属性与可见栏目名一致（悬停提示不能是旧名）', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    const btn = screen.getByTitle('实盘交易');
    expect(btn.querySelector('.dock-label')?.textContent).toBe('实盘交易');
  });
});
