/**
 * arena（Quant Agent Trader）移植层 —— 本机实盘栏专用，整个 `agent-arena/` 目录不入库。
 *
 * 对外只有两样东西：
 *  - `ARENA_TABS`：追加到交易台侧栏的三栏（智能体交易 / 实况 / 行情回测）
 *  - `ARENA_SETTINGS_PANELS`：嵌进「设置」页的三块（总控 / 数据 / 关于）
 *
 * 页面代码来自 quant-Trader 的 arena 前端，由 `tools/port-from-arena.mjs` 搬运并
 * 作用域化（见同目录 PORTING.md）。要跟着 arena 更新，重跑那个脚本即可。
 */
import { Activity, Blocks, Bot, Info, LayoutDashboard, Database } from 'lucide-react';
import type { RealTradingExtraTab } from '../../../pages/trading/RealTradingPage';
import type { SettingsExtraPanel } from '../../../pages/trading/tabs/SettingsCenter';
import AgentLedgerTab from './tabs/AgentLedgerTab';
import LiveTab from './tabs/LiveTab';
import MarketLabTab from './tabs/MarketLabTab';
import AboutPanel from './settings/AboutPanel';
import ControlPanel from './settings/ControlPanel';
import DataPanel from './settings/DataPanel';

/**
 * 追加页签顺序 = 侧栏顺序。**插在「设置」之前**（`composeConsoleTabs` 把设置钉在最后，
 * 见 `utils/consoleTabs.ts::PINNED_LAST_TAB_ID`）——2026-09-23 用户口径「设置放最低」。
 *
 * 「关于」2026-09-23 移到设置页（用户「关于能放设置那里吗」），不再占侧栏一栏。
 */
export const ARENA_TABS: readonly RealTradingExtraTab[] = [
  {
    id: 'agent-ledger',
    label: '智能体交易',
    icon: Bot,
    // 台账 + 对话流都是轮询（无长连接），切走即卸载即可，不必常驻后台刷
    render: () => <AgentLedgerTab />,
  },
  { id: 'arena-live', label: '实况', icon: Activity, render: () => <LiveTab /> },
  { id: 'arena-market-lab', label: '行情回测', icon: Blocks, render: () => <MarketLabTab /> },
];

/** 设置页里的三块（顺序即设置页顶部按钮条上的顺序） */
export const ARENA_SETTINGS_PANELS: readonly SettingsExtraPanel[] = [
  { id: 'arena-control', label: '总控', icon: LayoutDashboard, render: () => <ControlPanel /> },
  { id: 'arena-data', label: '数据', icon: Database, render: () => <DataPanel /> },
  { id: 'arena-about', label: '关于', icon: Info, render: () => <AboutPanel /> },
];
