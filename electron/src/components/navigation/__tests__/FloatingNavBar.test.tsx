/**
 * 底部导航「交易」栏目随交易模式改名。
 *
 * 用户原话：「模拟交易如果我点了实盘，下面栏目 也要变动成实盘交易。我选的模拟交易就
 * 模拟交易不动。」——底部导航是常驻的，用户据此确认自己现在在哪个账户里操作；
 * 切了实盘还写着「模拟交易」，就是让人以为真金白银的单只是模拟单。
 *
 * 文案不另起字面量：与交易页内所有模式文案同取 `modeCopy().full`（唯一事实源），
 * 所以这里断言的是「栏目名 == 该形态下应有的模式全称」，而不是把「实盘交易」再抄一遍。
 *
 * 2026-09-22 起这条规则分两种形态（用户原话：「模拟盘栏目，就搞模拟盘，实盘的都去掉吧、
 * 现在 2 个模块的。一个模拟、一个实盘。」）：公开树只有一栏，名字随模式走；本机两栏
 * 并存，交易栏目定死模拟盘、名字也随之钉住。所以断言写 `expectedTradingLabel(mode)`，
 * 由形态决定期望值——写死任何一个都会在另一种形态下变红。
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { Provider } from 'react-redux';
import React from 'react';
import store from '../../../store';
import { setTradingMode } from '../../../store/slices/uiSlice';
import { FloatingNavBar } from '../FloatingNavBar';
import { modeCopy, containsSimulationWording } from '../../../pages/trading/utils/tradingModeCopy';
import { isLocalLiveAvailable } from '../../../features/shared/localLive';

const renderNav = () =>
  render(
    <Provider store={store}>
      <FloatingNavBar current="trading" onChange={() => {}} />
    </Provider>,
  );

/** 只取底部导航里的栏目名，避免把页面别处的「模拟交易」一起捞进来 */
const dockLabels = (): string[] =>
  Array.from(document.querySelectorAll('.dock-label')).map((el) => (el.textContent || '').trim());

/**
 * 按**栏目身份**取文案，而不是按文案反查栏目。
 *
 * 起因：本机形态下底部栏会多出一个「实盘交易」栏目（实盘模式的交易栏目也叫这个名），
 * 凡是「全 dock 文案里不许出现某个词」或「总数应为 N」的写法都会被它误伤。
 * 这些断言真正说的是「**交易那一个栏目**随模式改名」，所以就该锚在 trading 上。
 */
const navItem = (id: string): HTMLElement | null =>
  document.querySelector<HTMLElement>(`[data-nav-id="${id}"]`);

const navItemLabel = (id: string): string =>
  (navItem(id)?.querySelector('.dock-label')?.textContent || '').trim();

/** 非交易类栏目数：本机的「实盘交易」栏目是额外一个，公开仓为 0 */
const EXTRA_LOCAL_ITEMS = isLocalLiveAvailable ? 1 : 0;

/**
 * 交易栏目名是否被定死成模拟盘。
 *
 * 本机有独立的「实盘交易」栏目，交易栏目恒为模拟盘（`resolveSimColumnForcedMode`
 * 同源的判断，源码里走 `isLocalLiveAvailable`）。栏目名再跟着全局模式走，底部栏
 * 就会出现**两个都叫「实盘交易」**的入口，点进去一个却是模拟盘 —— 所以这里也不再
 * 断言「栏目名 == 当前模式」，而是断言两种形态各自该有的名字。
 */
const SIM_COLUMN_PINNED = isLocalLiveAvailable;

/** 交易栏目此刻**应该**叫什么。公开树跟随模式，本机钉在模拟。 */
const expectedTradingLabel = (mode: 'simulation' | 'real'): string =>
  modeCopy(SIM_COLUMN_PINNED ? 'simulation' : mode).full;

describe('FloatingNavBar 交易栏目名', () => {
  beforeEach(() => {
    store.dispatch(setTradingMode('simulation'));
  });

  afterEach(() => {
    cleanup();
  });

  it('模拟模式下栏目名是「模拟交易」', () => {
    renderNav();

    expect(navItemLabel('trading')).toBe('模拟交易');
    // 交易栏目的实盘文案必须消失（其它栏目叫什么都不算数）
    expect(navItemLabel('trading')).not.toBe('实盘交易');
  });

  it('切到实盘后栏目名跟着模式走（本机：交易栏目钉死模拟，实盘文案在不远处另有一栏）', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    expect(navItemLabel('trading')).toBe(expectedTradingLabel('real'));
    if (SIM_COLUMN_PINNED) {
      // 默认那句「栏目名 == 模式全称」在本机不成立，但「实盘文案在底部栏可见」仍要成立
      expect(navItemLabel('trading')).toBe('模拟交易');
    }
  });

  it('实盘下底部导航不再出现「模拟」字样（本机：只剩被钉住的那一栏）', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    const offending = dockLabels().filter(containsSimulationWording);
    if (!SIM_COLUMN_PINNED) {
      expect(offending).toEqual([]);
      return;
    }
    // 本机这一栏本来就该叫「模拟交易」（它就是模拟盘入口），错配的是**实盘语境**：
    // 除它之外不许再有第二处模拟文案，且实盘文案必须真的在栏里。
    expect(offending).toEqual([modeCopy('simulation').full]);
    expect(dockLabels()).toContain(modeCopy('real').full);
  });

  it('实盘下「回测中心」等其它栏目名不受影响（改的只是交易栏目）', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    const labels = dockLabels();
    expect(labels).toContain('回测中心');
    expect(labels).toContain('模型训练');
    // 非管理员：12 个栏目，无「后台管理」。本机形态下多一个「实盘交易」栏目，
    // 走同一个垫片取偏移量——写死 13 会让公开仓变红，写死 12 会让本机变红。
    expect(labels).toHaveLength(12 + EXTRA_LOCAL_ITEMS);
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
    expect((active?.textContent || '').trim()).toBe(expectedTradingLabel('real'));
    expect(active?.closest('[aria-current="page"]')).not.toBeNull();
  });

  it('渲染时 title 属性与可见栏目名一致（悬停提示不能是旧名）', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    // 按身份取（不是 getByTitle('实盘交易')）：本机形态下同名的还有本机栏目，
    // 按文案取会命中两个元素直接抛错。
    const btn = navItem('trading');
    expect(btn?.getAttribute('title')).toBe(expectedTradingLabel('real'));
    expect(btn?.querySelector('.dock-label')?.textContent).toBe(expectedTradingLabel('real'));
  });

  // 本机形态专属：两个栏目并存时**不许重名**。公开仓没有第二个栏目，跳过而不是假装通过。
  it.skipIf(!SIM_COLUMN_PINNED)('本机：模拟栏目与实盘栏目各占一栏，且不重名', () => {
    store.dispatch(setTradingMode('real'));

    renderNav();

    expect(navItemLabel('trading')).toBe(modeCopy('simulation').full);
    expect(navItemLabel('live')).toBe(modeCopy('real').full);

    // 全 dock 文案唯一：两个都叫「实盘交易」正是这次要消除的形态
    const labels = dockLabels();
    expect(new Set(labels).size).toBe(labels.length);
  });
});
