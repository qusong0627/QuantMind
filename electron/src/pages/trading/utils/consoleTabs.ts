/**
 * 交易控制台页签表 —— 基础 9 栏 + 「追加页签」机制的纯逻辑。
 *
 * 存在理由：本机独有的「实盘交易」栏目（`features/local-live/`，不入库）要求
 * **与模拟交易同一个控制台**，只多出实盘专属的几栏。做法不是把外壳复制一份
 * （复制出来的那一份会在模拟交易每次改动后漂移），而是把「页签表」和「固定模式」
 * 两个决策抽成纯函数，`RealTradingPage` 只是它的消费者。
 *
 * 抽出来的另一个好处是可测：页签清单是用户可见约定，改名/改序/被顶掉都必须被
 * 测试拦住（同 `activeTab.ts` 的抽取理由）。
 */

import {
    Award,
    BarChart3,
    ClipboardList,
    FileText,
    HeartPulse,
    LayoutDashboard,
    PieChart,
    Settings,
    User,
    type LucideIcon,
} from 'lucide-react';
import type { ActiveTab } from './activeTab';

/** 侧栏一栏。`id` 同时是内容区分发键。 */
export interface ConsoleTab {
    id: string;
    label: string;
    icon: LucideIcon;
}

/** 交易模式偏好。与 `useTradingModeSwitch` 的 `TradingModePref` 同构（此处不引 tsx，保持纯模块）。 */
export type ConsoleTradingMode = 'real' | 'simulation';

/**
 * 基础页签：顺序即侧栏顺序，**唯一事实源**。
 *
 * 顺序有含义：`desk` 首位 = 无深链时的落地页（`activeTab.ts::DEFAULT_ACTIVE_TAB`），
 * 进页第一眼先看平台能不能用。改序前先读那段注释。
 */
export const BASE_CONSOLE_TABS: readonly ConsoleTab[] = [
    // 系统健康（原「今日交易台」自底部栏迁入，2026-09-17 分栏归位后更名）
    { id: 'desk' satisfies ActiveTab, label: '系统健康', icon: HeartPulse },
    // 候选信号独立成栏（2026-09-17 自交易台拆出）
    { id: 'signals' satisfies ActiveTab, label: '候选信号', icon: BarChart3 },
    // 评估中心自因子研究迁入（2026-09-17），置于策略管理之前（评估与策略同组）
    { id: 'eval' satisfies ActiveTab, label: '评估中心', icon: Award },
    { id: 'manage' satisfies ActiveTab, label: '策略管理', icon: LayoutDashboard },
    // 时光回放功能尚存多处问题，暂时隐藏入口，完善后取消注释即可恢复（ReplayPage 渲染分支保留）
    // { id: 'replay', label: '时光回放', icon: Clock },
    { id: 'manual-task' satisfies ActiveTab, label: '手动任务', icon: ClipboardList },
    { id: 'position' satisfies ActiveTab, label: '持仓监控', icon: PieChart },
    { id: 'history' satisfies ActiveTab, label: '交易记录', icon: FileText },
    { id: 'personal' satisfies ActiveTab, label: '个人中心', icon: User },
    { id: 'settings' satisfies ActiveTab, label: '设置', icon: Settings },
];

/**
 * 基础页签 + 追加页签。追加项一律排在基础项之后，传入顺序即显示顺序。
 *
 * `id` 冲突**抛错而不是去重**：去重会静默少一栏，而「实盘栏目比模拟交易少了
 * 一栏」正是本次要消除的不一致；冲突是调用方的编程错误，就该在开发期炸出来。
 */
export function composeConsoleTabs(extra?: readonly ConsoleTab[]): ConsoleTab[] {
    const composed: ConsoleTab[] = [...BASE_CONSOLE_TABS];
    if (!extra || extra.length === 0) return composed;

    const seen = new Set(composed.map((tab) => tab.id));
    for (const tab of extra) {
        if (seen.has(tab.id)) {
            throw new Error(
                `[consoleTabs] 追加页签 id 与既有页签冲突：${tab.id}。` +
                    '冲突会让侧栏出现两个同 id 的按钮。' +
                    '基础页签的 id 钉在 BASE_CONSOLE_TABS 上，请换一个不重名的 id；' +
                    '该表是同一次渲染里算出来的，所以这个错会在挂载时必现。',
            );
        }
        seen.add(tab.id);
        composed.push(tab);
    }
    return composed;
}

/**
 * 当前页签在不在表里；不在就回落 `fallback`。
 *
 * 用途：切市场后原来那一栏可能已不存在（如停在美股下没有的「大 QMT 真单镜像」），
 * 不回落会渲染成整块空白。调用方按返回值决定要不要 setState。
 */
export function resolveConsoleTab(
    current: string,
    tabs: readonly ConsoleTab[],
    fallback: string,
): string {
    return tabs.some((tab) => tab.id === current) ? current : fallback;
}

/**
 * 生效模式：固定模式优先，未固定则跟随全局。
 *
 * **刻意绕开 `useTradingModeSwitch` 的归一**：那个 hook 在 `isLiveTradingEnabled()`
 * 为 false 时把 `real` 归一成 `simulation`（公开发行版的安全方向）。本机「实盘交易」
 * 栏目的入口本就不由该开关决定（`FloatingNavBar` 只认 `isLocalLiveAvailable`），
 * 若在这里跟着归一，页面标题写着实盘、账户却是模拟的 —— 比不显示更危险。
 */
export function resolveConsoleTradingMode(
    forced: ConsoleTradingMode | undefined,
    ambient: ConsoleTradingMode,
): ConsoleTradingMode {
    return forced ?? ambient;
}
