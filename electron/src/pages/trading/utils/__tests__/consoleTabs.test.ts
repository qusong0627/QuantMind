/**
 * 交易控制台页签表的纯逻辑测试。
 *
 * 为什么这些断言值得写：本机「实盘交易」栏目靠 `extraTabs` 复用同一个控制台
 * （`RealTradingPage`）。一旦基础页签被改名/改序，或追加页签静默顶掉一个基础
 * 页签，实盘栏目就与模拟交易**不再一致**——而这类不一致在浏览器里表现为
 * 「少了一栏」，只有人眼盯着才发现。所以钉在纯函数上。
 */

import { describe, it, expect } from 'vitest';
import { Star } from 'lucide-react';
import {
    BASE_CONSOLE_TABS,
    composeConsoleTabs,
    resolveConsoleTab,
    resolveConsoleTradingMode,
    resolveSimColumnForcedMode,
    type ConsoleTab,
} from '../consoleTabs';

/**
 * 追加页签夹具**刻意用中性名**，不照抄任何真实调用方的 id/文案。
 * 本文件随公开仓发布，而真实的追加页签来自不入库的本机栏目——夹具跟着抄一遍，
 * 就等于把那个页面的内部清单泄进公开仓。这里只验证机制，不验证谁的清单。
 */
const EXTRA: ConsoleTab = { id: 'extra-one', label: '追加栏一', icon: Star };

describe('BASE_CONSOLE_TABS', () => {
    it('基础页签就是这 9 个，顺序即侧栏顺序', () => {
        expect(BASE_CONSOLE_TABS.map((t) => t.id)).toEqual([
            'desk',
            'signals',
            'eval',
            'manage',
            'manual-task',
            'position',
            'history',
            'personal',
            'settings',
        ]);
    });

    it('默认落地页签「系统健康」仍在首位（改序会让进页第一眼换内容）', () => {
        expect(BASE_CONSOLE_TABS[0].id).toBe('desk');
    });

    it('每个页签都有非空文案，且 id 不重复', () => {
        for (const tab of BASE_CONSOLE_TABS) {
            expect(tab.label.trim().length).toBeGreaterThan(0);
            expect(tab.icon).toBeTruthy();
        }
        const ids = BASE_CONSOLE_TABS.map((t) => t.id);
        expect(new Set(ids).size).toBe(ids.length);
    });
});

describe('composeConsoleTabs', () => {
    it('不传追加页签时与基础页签逐项相同', () => {
        expect(composeConsoleTabs()).toEqual([...BASE_CONSOLE_TABS]);
    });

    it('传入空数组时同样与基础页签相同', () => {
        expect(composeConsoleTabs([])).toEqual([...BASE_CONSOLE_TABS]);
    });

    it('追加页签排在基础页签之后，且保持传入顺序', () => {
        const second: ConsoleTab = { id: 'extra-two', label: '追加栏二', icon: Star };
        const ids = composeConsoleTabs([EXTRA, second]).map((t) => t.id);
        expect(ids).toEqual([...BASE_CONSOLE_TABS.map((t) => t.id), 'extra-one', 'extra-two']);
    });

    it('追加页签 id 与基础页签冲突时抛错，不静默合成一个页签', () => {
        expect(() => composeConsoleTabs([{ ...EXTRA, id: 'desk' }])).toThrow(/desk/);
    });

    it('追加页签之间 id 重复同样抛错', () => {
        expect(() => composeConsoleTabs([EXTRA, { ...EXTRA, label: '另一个' }])).toThrow(/extra-one/);
    });

    it('不修改传入的数组（不可变）', () => {
        const extra: ConsoleTab[] = [EXTRA];
        composeConsoleTabs(extra);
        expect(extra).toHaveLength(1);
        expect(BASE_CONSOLE_TABS).toHaveLength(9);
    });
});

describe('resolveConsoleTab', () => {
    const tabs = composeConsoleTabs([EXTRA]);

    it('页签仍存在时原样返回', () => {
        expect(resolveConsoleTab('position', tabs, 'desk')).toBe('position');
        expect(resolveConsoleTab('extra-one', tabs, 'desk')).toBe('extra-one');
    });

    it('页签已不存在时回落 fallback —— 切市场后停在已消失的页签', () => {
        // 实例：切到没有该栏的市场后，停在那页会渲染成整块空白
        expect(resolveConsoleTab('extra-absent', tabs, 'desk')).toBe('desk');
    });

    it('空字符串同样回落', () => {
        expect(resolveConsoleTab('', tabs, 'desk')).toBe('desk');
    });
});

describe('resolveConsoleTradingMode', () => {
    it('未固定时跟随全局模式', () => {
        expect(resolveConsoleTradingMode(undefined, 'real')).toBe('real');
        expect(resolveConsoleTradingMode(undefined, 'simulation')).toBe('simulation');
    });

    it('固定模式优先于全局模式（全局是模拟，固定实盘仍是实盘）', () => {
        expect(resolveConsoleTradingMode('real', 'simulation')).toBe('real');
    });

    it('固定模拟同样优先（只读/回放形态可复用本控制台）', () => {
        expect(resolveConsoleTradingMode('simulation', 'real')).toBe('simulation');
    });

    it('固定实盘不被「实盘开关关闭」降级 —— 降级会把实盘栏目显示成模拟账户', () => {
        // useTradingModeSwitch 在 isLiveTradingEnabled() 为 false 时把 real 归一成
        // simulation。固定模式**绕开**该归一：本机实盘栏目的入口本就不由该开关决定，
        // 悄悄换成模拟账户等于让人对着假数字看实盘。
        expect(resolveConsoleTradingMode('real', 'simulation')).toBe('real');
        expect(resolveConsoleTradingMode('real', 'real')).toBe('real');
    });
});

describe('resolveSimColumnForcedMode', () => {
    it('有独立实盘栏目时定死模拟盘 —— 两栏各管一边', () => {
        expect(resolveSimColumnForcedMode(true)).toBe('simulation');
    });

    it('没有独立实盘栏目时不固定，跟随全局模式', () => {
        // 公开树只有这一栏，模式开关是通往实盘的唯一入口：固定住就等于没有实盘了
        expect(resolveSimColumnForcedMode(false)).toBeUndefined();
    });

    it('固定后不被全局模式翻过去（全局停在实盘也一样）', () => {
        expect(resolveConsoleTradingMode(resolveSimColumnForcedMode(true), 'real')).toBe('simulation');
    });
});
