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
    // 把 liveConfigVisible / tradingMode 暴露到 DOM：公开树的默认值（true）与模拟栏的
    // 取值（false）只能从这里观测——真面板要挂 live 组件，桩不掉。
    default: ({
        liveConfigVisible,
        tradingMode,
    }: {
        liveConfigVisible?: boolean;
        tradingMode?: string;
    }) => (
        <div
            data-testid="pane-settings"
            data-live-config={String(liveConfigVisible)}
            data-trading-mode={tradingMode}
        />
    ),
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

    it('追加页签 keepMounted：开页即挂载，切走只隐藏不卸载', () => {
        // 这条是给「iframe 里跑着 dsh」的那一栏立的（QuantBot 栏）：卸载＝断流、重来一次。
        // 代价与收益各钉一半 —— 两侧任一被改回去，这里都要红。
        renderPage({ extraTabs: [{ ...makeExtraTab(), keepMounted: true }] });

        // 代价：还没点过它就挂上了（所以只有确实要保流的栏才配开这个开关）
        expect(pane('extra-probe')).not.toBeNull();

        clickTab('探针栏');
        // 包壳走 contents：不生成盒子，与裸渲染等价（改成 block 会把 h-full 撑坏）
        expect(pane('extra-probe')!.parentElement?.className).toContain('contents');

        // 收益：换到基础栏后它仍在 DOM 里，只是被隐藏
        clickTab('系统健康');
        expect(pane('desk')).not.toBeNull();
        const kept = pane('extra-probe');
        expect(kept).not.toBeNull();
        expect(kept!.parentElement?.className).toContain('hidden');
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

    it('设置栏：未固定模式时实盘配置照常可见（公开树的老路径）', () => {
        renderPage();
        clickTab('设置');

        expect(pane('settings')?.getAttribute('data-live-config')).toBe('true');
    });

    it('设置栏：定死模拟盘时实盘配置整块收起', () => {
        // 「模拟交易」栏目的最后一道闸：账户、下发、顶栏都已是模拟盘，设置里再留
        // 「券商实盘接入」就等于把实盘又搬回来了（改完凭证下一步就是下单）。
        renderPage({ forcedTradingMode: 'simulation' });
        clickTab('设置');

        expect(pane('settings')?.getAttribute('data-live-config')).toBe('false');
        // 标题取的是**生效模式**：全局偏好留在实盘，这一栏也仍是模拟盘设置
        expect(pane('settings')?.getAttribute('data-trading-mode')).toBe('simulation');
    });

    it('顶栏横幅缺省：不传 banner 就整段不渲染', () => {
        renderPage();

        expect(document.querySelector('[data-testid="banner-probe"]')).toBeNull();
        // 顶栏本身还在（说明不是把整块顶栏一起省掉了）
        expect(document.querySelector('[data-testid="topbar"]')).not.toBeNull();
    });

    it('顶栏横幅：拿到与追加页签同一份运行期上下文', () => {
        // 「同一份」是这条槽位的全部意义：横幅报的账户状态与页内看到的不可能有相位差。
        // 关键在 `toBe` —— 只要有人把 ctx 拆成两个 useMemo（哪怕字段一样），当场变红。
        let seenBanner: RealTradingTabContext | null = null;
        let seenTab: RealTradingTabContext | null = null;
        renderPage({
            banner: (ctx) => {
                seenBanner = ctx;
                return <div data-testid="banner-probe" />;
            },
            extraTabs: [makeExtraTab((ctx) => { seenTab = ctx; })],
        });

        clickTab('探针栏');

        expect(document.querySelector('[data-testid="banner-probe"]')).not.toBeNull();
        expect(seenBanner).not.toBeNull();
        expect(seenBanner).toBe(seenTab);
    });
});
