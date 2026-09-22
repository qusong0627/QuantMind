/**
 * `RealTradingPage` 外壳的**接线**护栏。
 *
 * 为什么需要这一层：`utils/__tests__/consoleTabs.test.ts` 只覆盖纯函数，而「零 props
 * 与引入本机制前等价」「追加页签真的挂载 / 切走真的卸载」「固定模式定死实盘」这三条
 * 承诺是靠 JSX 分支与 effect 兑现的 —— 删掉内容区的追加分支、把回落的 deps 写成 `[]`、
 * 或让 `resolveConsoleTradingMode` 退回归一，那 16 个绿测一个都不会红。
 *
 * 子页面一律替换为轻量桩：本文件测的是外壳自己的分派逻辑，不测 DeskTodayPage 之类
 * 子页面的内部行为（它们各有自己的测试）。这样也避免渲染时打出真实网络请求。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/react';
import { Provider } from 'react-redux';
import React from 'react';
import { Activity } from 'lucide-react';

import store from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import { setTradingMode } from '../../../store/slices/uiSlice';
import RealTradingPage from '../RealTradingPage';
import type { RealTradingExtraTab, RealTradingTabContext } from '../RealTradingPage';
import { BASE_CONSOLE_TABS } from '../utils/consoleTabs';

// ── 子页面桩 ────────────────────────────────────────────────────────────────
// 每个桩带自己的 testid，用于断言「当前挂载的是哪一栏」。

vi.mock('../components/TopBar', () => ({
    // 把 tradingMode 暴露到 DOM：固定模式是否真的定死实盘，只能从这里观测
    default: ({ tradingMode }: { tradingMode: string }) => (
        <div data-testid="topbar" data-trading-mode={tradingMode} />
    ),
}));

vi.mock('../tabs/StrategyConsole/TopologyConsole', () => ({
    default: () => <div data-testid="pane-manage" />,
}));
vi.mock('../../../features/desk/DeskTodayPage', () => ({
    default: () => <div data-testid="pane-desk" />,
}));
vi.mock('../tabs/SignalsExplorerPage', () => ({
    default: () => <div data-testid="pane-signals" />,
}));
vi.mock('../../../features/eval-center/components/EvalCenterPanel', () => ({
    EvalCenterPanel: () => <div data-testid="pane-eval" />,
}));
vi.mock('../tabs/ManualTaskPage', () => ({
    default: () => <div data-testid="pane-manual-task" />,
}));
vi.mock('../tabs/PersonalCenter', () => ({
    default: () => <div data-testid="pane-personal" />,
}));
vi.mock('../tabs/PositionMonitor', () => ({
    default: () => <div data-testid="pane-position" />,
}));
vi.mock('../tabs/TradingHistory', () => ({
    default: () => <div data-testid="pane-history" />,
}));
vi.mock('../tabs/SettingsCenter', () => ({
    default: () => <div data-testid="pane-settings" />,
}));
vi.mock('../tabs/ReplayPage', () => ({
    default: () => <div data-testid="pane-replay" />,
}));
vi.mock('../components/LiveTradeConfigWizard', () => ({
    default: () => <div data-testid="pane-wizard" />,
}));

// 成交推送会开 WebSocket；无 token 时本页本就轮询早退，这里一并摘掉
vi.mock('../../../hooks/useTradeWebSocket', () => ({
    useTradeWebSocket: () => {},
}));

// 无 token → fetchData 在第一步就 return，不会去 dynamic import 真实服务
vi.mock('../../../features/auth/services/authService', () => ({
    authService: { getAccessToken: () => null },
}));

// ── 工具 ────────────────────────────────────────────────────────────────────

const renderPage = (props: React.ComponentProps<typeof RealTradingPage> = {}) =>
    render(
        <Provider store={store}>
            <RealTradingPage {...props} />
        </Provider>,
    );

/** 侧栏文案。锚在「功能导航」标题上，与本仓探针同口径，不按 class 猜结构。 */
const sidebarLabels = (): string[] => {
    const anchor = Array.from(document.querySelectorAll('span')).find(
        (s) => (s.textContent || '').trim() === '功能导航',
    );
    const container = anchor?.closest('div')?.parentElement;
    if (!container) return [];
    return Array.from(container.querySelectorAll('button')).map((b) =>
        (b.textContent || '').trim(),
    );
};

const clickTab = (label: string) => {
    const btn = Array.from(document.querySelectorAll('button')).find(
        (b) => (b.textContent || '').trim() === label,
    );
    if (!btn) throw new Error(`侧栏没有「${label}」这一栏`);
    fireEvent.click(btn);
};

const pane = (id: string) => document.querySelector(`[data-testid="pane-${id}"]`);

/** 追加页签（id 取探针前缀，与基础 9 栏的 id 不会撞） */
const makeExtraTab = (onRender?: (ctx: RealTradingTabContext) => void): RealTradingExtraTab => ({
    id: 'extra-probe',
    label: '探针栏',
    icon: Activity,
    render: (ctx) => {
        onRender?.(ctx);
        return <div data-testid="pane-extra-probe" />;
    },
});

const BASE_LABELS = BASE_CONSOLE_TABS.map((t) => t.label);

describe('RealTradingPage 外壳接线', () => {
    beforeEach(() => {
        store.dispatch(setTradingMode('simulation'));
    });

    afterEach(() => {
        cleanup();
    });

    it('零 props：侧栏就是基础页签，一条不多一条不少', () => {
        renderPage();

        expect(sidebarLabels()).toEqual(BASE_LABELS);
    });

    it('零 props：默认落在系统健康', () => {
        renderPage();

        expect(pane('desk')).not.toBeNull();
    });

    it('零 props：模式随全局走，顶栏报模拟盘', () => {
        renderPage();

        expect(document.querySelector('[data-testid="topbar"]')?.getAttribute('data-trading-mode')).toBe(
            'simulation',
        );
    });

    it('固定实盘：顶栏报实盘，且不因全局是模拟而回落', () => {
        // 全局偏好刻意留 simulation + 构建期 flag 关闭（vitest 下默认为关）——
        // 两者都拦不住 forced：本机实盘栏目要的就是定死，理由见 consoleTabs.ts
        renderPage({ forcedTradingMode: 'real' });

        expect(document.querySelector('[data-testid="topbar"]')?.getAttribute('data-trading-mode')).toBe(
            'real',
        );
    });

    it('追加页签：排在基础 9 栏之后，点击挂载，切走卸载', () => {
        renderPage({ extraTabs: [makeExtraTab()] });

        // 追加在末尾，基础 9 栏原样在前
        expect(sidebarLabels()).toEqual([...BASE_LABELS, '探针栏']);

        clickTab('探针栏');
        expect(pane('extra-probe')).not.toBeNull();

        // 基础栏仍可切回，且追加栏的内容真的卸载（不是被盖住）
        clickTab('系统健康');
        expect(pane('desk')).not.toBeNull();
        expect(pane('extra-probe')).toBeNull();
    });

    it('追加页签：render 拿到当前市场与可调用的 refresh', () => {
        let seen: RealTradingTabContext | null = null;
        renderPage({
            extraTabs: [
                {
                    ...makeExtraTab(),
                    render: (ctx) => {
                        seen = ctx;
                        return <div data-testid="pane-extra-probe" />;
                    },
                },
            ],
        });

        clickTab('探针栏');

        expect(seen).not.toBeNull();
        expect(seen!.market).toBe(selectCurrentMarket(store.getState()));
        expect(typeof seen!.refresh).toBe('function');
        expect(() => seen!.refresh()).not.toThrow();
    });

    it('追加页签为空数组：等同于不追加', () => {
        renderPage({ extraTabs: [] });

        expect(sidebarLabels()).toEqual(BASE_LABELS);
    });
});
