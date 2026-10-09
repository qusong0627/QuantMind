/**
 * 「实盘交易」栏目 —— 2026-10-08 起**随仓分发**（此前为本机独有、被 .gitignore 排除）。
 *
 * 源码在仓（本目录）不等于 UI 可见：是否**渲染**由构建期 `VITE_ENABLE_REAL_TRADING`
 * 收敛——默认构建（.env.production 里为 false）里底部栏不出现「实盘交易」入口，
 * 深链 `#/live` 落到 `LiveDisabledPage` 说明页；后端端点另有 `ENABLE_REAL_TRADING`
 * 闸门（403 `real_trading_disabled`）。两端同时打开才完整启用，
 * 步骤见 `docs/实盘模块_启用与通道指南.md`。闸门测试：
 * `features/shared/__tests__/localLiveTracking.test.ts`。
 *
 * ## 与「模拟交易」的关系：同一个控制台，不是两套界面
 *
 * 用户明确要求：「实盘交易要和模拟交易的风格一样、功能要一致，只是增加实盘功能」。
 * 所以本页**不再自绘外壳**——它把 `RealTradingPage` 原样拿来跑，只做六件事：
 *
 * - `forcedTradingMode="real"`：模式定死实盘（不挂模式开关，见 `consoleTabs.ts` 的归一理由）；
 * - `topBarExtras`：顶栏右侧状态行挂全局市场切换器 A股/港股/美股（2026-10-08 用户要求）；
 * - `extraTabs=[...]`：把实盘专属面板挂到侧栏末尾；
 * - `manualTaskExtras` / `positionRailExtras`：把不单起栏的面板**并进宿主页**
 *   （推送下单 → 手动任务向导下方；风控止损 → 持仓监控右栏，2026-09-23 用户指定）；
 * - `banner`：账户不可用（未绑定/未上报/不存在）的跨页签提示，挂在顶栏下方；
 * - 开关兜底：构建期 `VITE_ENABLE_REAL_TRADING` 关闭时整页不渲染（见 `LiveDisabledPage`）。
 *
 * 于是基础的 9 栏（系统健康/候选信号/评估中心/策略管理/手动任务/持仓监控/交易记录/
 * 个人中心/设置）、顶栏资产概览、5s 轮询、成交 WebSocket 刷新全部**自动**与模拟交易
 * 一致；模拟交易那边以后加一栏，这边跟着出现，不需要改本文件。
 *
 * 上一版的实现是自绘顶栏 + 横向 pill 导航 + 6 个面板，与模拟交易完全两套观感。
 * 那条路已废弃（「尽量和模拟交易区别开」的要求已被用户撤回）；现在只保留其中
 * **实盘独有**的部分作为追加页签，重复的部分直接删掉，不再重复实现。
 *
 * ## 为什么「券商接入」「大 QMT 镜像」不在这里（2026-09-22 用户要求「别重复了」）
 *
 * 这两块**已经**在「设置」栏里（设置 → 券商实盘接入 / 大 QMT 真单镜像，同一对
 * `BrokerChannelCard`/`BrokerConfigCard` 与 `QmtMirrorCard`）。此前本页又各挂了一个
 * 页签，等于同一份配置有两处入口、两处渲染 —— 用户点名要去掉。**配置类面板的唯一
 * 落点是设置栏**；本页只留「跑起来才有意义」的几件，加上一条状态横幅：
 * 账户不可用是**跨页签**的状态，不是某一栏的内容。
 *
 * ## 2026-09-23：侧栏不再无限加栏，能并的就并进宿主页
 *
 * 用户：「推送下单和风控止损，可以推送下单，放手动任务、风控止损放持仓监控那。」
 * 侧栏每加一栏都是一次全局导航负担，而这两块本来各自属于某一页的语境
 * （推送下单=手动发起委托；风控止损=止损最终是平仓）。做法是走两个可选槽位把内容
 * 注入宿主页，**不是**新开一栏 —— 于是本页的追加页签只剩 QuantBot 与 arena 四栏。
 *
 * ## QuantBot 为什么是「设置」下面的一栏（2026-09-22 用户指定）
 *
 * 实盘节点形态没有底部导航（`FloatingNavBar` 返回 null），QuantBot 在那里不成一个
 * 「栏目」。第一版把它做成交易台**左侧的常驻栏**（`QuantBotDock`，400px 可收起）——
 * 用户否了：「不是放左侧、是设置下面、右边栏是显示的」。所以它现在就是一条普通的
 * 追加页签，**排在第一条**（正好落在侧栏「设置」下面），内容照常进右侧内容区，
 * 与其余八栏一个观感；QuantBotDock 已删。
 *
 * 唯一与其它栏不同的是 `keepMounted`：dsh 是 iframe 里的 SPA，切去「持仓监控」再
 * 切回来不该断线重来（普通追加栏仍是切走即卸载）。**这一栏在两种形态下都挂**，
 * 不按 `LIVE_NODE_ONLY` 判 —— 本机（全栏目形态）与节点看到的是同一份实盘栏目。
 *
 * 那条横幅（`banner`）代替的是原先挂在「券商接入」页签里的 `UnavailableAlert`——
 * 删页签会连带删掉它，于是它挪到顶栏下方，任何一栏都看得见，且不与设置栏重复
 * （设置栏管的是「怎么配」，横幅管的是「现在为什么用不了」）。
 *
 * ## 接线契约（`localLive.ts` 是硬约束，改这里必须同步改它）
 *
 * - 入口文件名必须是 `LiveTradingPage.tsx`；
 * - 必须 **default export** 且 **零 props**（`App.tsx` 的 `<LocalLivePage />` 不传参）。
 *
 * ## 备份
 *
 * 目录已入仓（git 是第一副本）。`bash scripts/backup_local_live.sh`（目标走
 * `LOCAL_LIVE_BACKUP_TARGET`）保留为 NAS 侧快照——兜的是「未提交的改动 + 误删工作区」
 * 这种 git 也救不了的场景。
 */

import React, { useMemo, useState } from 'react';
import { Bot, Send, ShieldCheck, AlertTriangle } from 'lucide-react';

import RealTradingPage from '../../pages/trading/RealTradingPage';
import type { RealTradingExtraTab, RealTradingTabContext } from '../../pages/trading/RealTradingPage';
import { MarketSelector } from '../../components/layout/MarketSelector';
import { isLiveTradingEnabled } from '../../config/tradingFlags';
import { QuantBotSurface, useQuantBotFrame } from '../quantbot/components/QuantBotFrame';

import LiveTradeConfigForm from '../../pages/trading/components/LiveTradeConfigForm';
import RiskLayer from '../../pages/trading/tabs/StrategyConsole/layers/RiskLayer';
import { validateLiveTradeConfig } from '../../pages/trading/utils/liveTradeConfigValidation';
import { PushConfirmPanel } from '../stock-terminal/components/PushConfirmPanel';
import { channelOptions } from '../stock-terminal/pushModel';
import type { PushChannel, PushSide } from '../stock-terminal-shared/types';
import type { ExecutionConfig, LiveTradeConfig } from '../../types/liveTrading';
import { ARENA_SETTINGS_PANELS, ARENA_TABS } from './agent-arena';

// ─────────────────────────────────────────────────────────────────────────────
// 常量
// ─────────────────────────────────────────────────────────────────────────────

/** T+1 口径下的 A 股时段占位默认值（仅推送面板初值，不代表实际交易时段） */
const DEFAULT_EXECUTION_CONFIG: ExecutionConfig = {
    max_buy_drop: -0.03,
    stop_loss: -0.08,
};

/** 与 LiveTradeConfigWizard 的 DEFAULT_LIVE_TRADE_CONFIG 同值：两处默认值必须一致 */
const DEFAULT_LIVE_TRADE_CONFIG: LiveTradeConfig = {
    rebalance_days: 3,
    schedule_type: 'interval',
    trade_weekdays: [],
    enabled_sessions: ['AM'],
    sell_time: '09:30',
    buy_time: '09:30',
    sell_first: true,
    order_type: 'MARKET',
    max_price_deviation: 0.02,
    max_orders_per_cycle: 20,
};

/** 账户不可用原因 → 可执行的中文提示（后端 `account_unavailable_reason` 枚举） */
const UNAVAILABLE_COPY: Record<string, string> = {
    // 配置凭证的唯一落点是「设置 → 券商实盘接入」（本页不再有这个页签，见文件头）
    unbound: '未绑定实盘账户 —— 去「设置 → 券商实盘接入」配置凭证',
    not_reported: '账户未上报 —— 检查交易端（QMT / 通达信桥）是否在跑',
    not_found: '账户不存在 —— 核对 userId 与账户绑定',
};

// 注：原先还有一个 `DisabledNotice`（单栏「实盘开关关闭」占位）。删掉「券商接入」
// 「大 QMT 镜像」两个页签后它没有消费者了 —— 剩下两栏各自内联了同一段文案，
// 而整页闸门在 `LiveDisabledPage`（构建期开关关闭时本页根本不渲染到栏位）。
/**
 * 构建期开关关闭时的**整页**兜底。
 *
 * 为什么必须在入口整页拦，而不是像各栏那样各自 DisabledNotice：
 * 本页把模式定死成实盘（`forcedTradingMode="real"`，见 `consoleTabs.ts` 的归一理由），
 * 于是外壳会照常渲染——顶栏标「实盘」，`GET /real-trading/account` 也**不**在后端闸门
 * 的拒绝表里（`live_trading_gate.py` 只拦 orders / history），**真实券商快照会渲进顶栏
 * 与持仓监控**。而这份构建的 flag 说的是「不显示实盘 UI」。
 *
 * 这正是 `config/tradingFlags.ts` 点名的危险方向：反向误判会让人以为真金白银在下单。
 * 各栏的 DisabledNotice 只盖得住自己那一栏，盖不住外壳。
 *
 * 触发条件是真实可达的：`electron/.env.production` 里 `VITE_ENABLE_REAL_TRADING=false`，
 * 而 `scripts/deploy_frontend.sh` 跑的是裸 `npm run build` —— 部署时忘了注入
 * `VITE_ENABLE_REAL_TRADING=true` 就是这个状态。此时 `/live` 路由照样存在
 * （`localLive.ts` 的 `isLocalLiveAvailable` 只看目录在不在，FloatingNavBar 亦刻意不接该 flag），
 * 所以这里必须自己把关。
 *
 * 下单路径另有后端点级闸门（preflight / orders / qmt-mirror 双闸，flag 关时一律 403），
 * 本兜底收的是**显示口径**这一面。
 */
const LiveDisabledPage: React.FC = () => (
    <div className="h-full w-full overflow-y-auto bg-[#f8fafc] p-6">
        <Card title="实盘控制台未启用">
            <div className="space-y-2 text-sm text-amber-800">
                <p>
                    本次构建的前端开关 <code>VITE_ENABLE_REAL_TRADING</code> 是关闭的，
                    所以整个实盘控制台不渲染 —— 包括顶栏的实盘标识与账户概览。
                </p>
                <p>
                    不这么做的话，外壳仍会标「实盘」并把**真实券商账户快照**显示出来，
                    与本次构建「不显示实盘 UI」的声明相反。
                </p>
                <p>
                    要启用需两端同时打开：后端 <code>ENABLE_REAL_TRADING=true</code>，
                    前端**构建时**注入 <code>VITE_ENABLE_REAL_TRADING=true</code>
                    （<code>VITE_ENABLE_REAL_TRADING=true bash scripts/deploy_frontend.sh --allow-local-live</code>）。
                </p>
            </div>
        </Card>
    </div>
);

// ─────────────────────────────────────────────────────────────────────────────
// 卡片外壳（只出内容，容器与滚动归宿主页）
//
// 类名与 StrategyConsole 五层、与 `PositionMonitor` 右栏的哨兵/副驾驶同款
// （`bg-white rounded-2xl border border-slate-200/80 p-4`），这样无论落在手动任务
// 的向导下方还是持仓监控的右栏里，观感都与邻居一致。
// ─────────────────────────────────────────────────────────────────────────────

const Card: React.FC<{
    title: React.ReactNode;
    extra?: React.ReactNode;
    children: React.ReactNode;
}> = ({ title, extra, children }) => (
    <section className="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold text-slate-800">{title}</h3>
            {extra}
        </div>
        {children}
    </section>
);

// ─────────────────────────────────────────────────────────────────────────────
// 顶栏横幅：账户不可用
//
// 跨页签的状态提示，不是某一栏的内容 —— 挂在顶栏下方，任何一栏都看得见。
// 「怎么配」在设置栏（券商实盘接入），这里只回答「现在为什么用不了」。
// ─────────────────────────────────────────────────────────────────────────────

/** 跳转到设置栏（走既有事件总线：`RealTradingPage` 监听后切到 settings 页签） */
const gotoSettings = () => window.dispatchEvent(new CustomEvent('goto-trading-settings'));

const AccountNoticeBanner: React.FC<{ ctx: RealTradingTabContext }> = ({ ctx }) => {
    const reason = ctx.accountInfo?.account_unavailable_reason;
    if (!reason) return null;
    return (
        <div className="flex items-center gap-2 border-b border-amber-200 bg-amber-50 px-6 py-2 text-sm text-amber-800">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>{UNAVAILABLE_COPY[reason] ?? `账户不可用（${reason}）`}</span>
            {reason === 'unbound' && (
                <button
                    type="button"
                    onClick={gotoSettings}
                    className="ml-auto shrink-0 rounded-lg border border-amber-300 bg-white px-2.5 py-0.5 text-xs font-bold text-amber-800 hover:bg-amber-100"
                >
                    去设置
                </button>
            )}
        </div>
    );
};

// ─────────────────────────────────────────────────────────────────────────────
// 「推送下单」：手动任务页里的一块（2026-09-23 用户要求，不再是侧栏一栏）
//
// 「推送下单」在后端是一等公民（push-orders 的 preflight/execute 两个端点），
// 前端承载件是 PushConfirmPanel。这里补一个入口：手填后缀式代码 → 勾通道 →
// 走同一套预检与确认。滚动调仓配置复用 LiveTradeConfigForm，但**只做本地草稿**——
// 真正落库的路径是「策略管理 → 策略下发」（LiveTradeConfigWizard 的 onConfirm），
// 本页没有策略上下文，不伪造保存接口。
//
// 用户口径「推送下单放手动任务」：两条路都是「手工发起一笔委托」，分在两栏时
// 用户要点两个地方才能确认「我这单到底发出去没有」。外壳（页容器/内边距/滚动）
// 由宿主页 `ManualTaskPage` 提供，这里**不再自带 TabPane**，只出内容。
// ─────────────────────────────────────────────────────────────────────────────

const PushOrderPanel: React.FC = () => {
    const liveEnabled = isLiveTradingEnabled();
    const [rawCodes, setRawCodes] = useState('');
    const [side, setSide] = useState<PushSide>('buy');
    const [channels, setChannels] = useState<PushChannel[]>(() => (liveEnabled ? ['sim', 'real'] : ['sim']));
    const [panelOpen, setPanelOpen] = useState(false);
    const [lastDone, setLastDone] = useState<string | null>(null);

    // 后缀式归一：用户习惯贴 600036 或 SH600036，都收敛成 600036.SH（push-orders 契约）
    const symbols = useMemo(
        () =>
            rawCodes
                .split(/[\s,，;；]+/)
                .map((s) => s.trim().toUpperCase())
                .filter(Boolean)
                .map((s) => {
                    const sh = /^SH(\d{6})$/.exec(s);
                    if (sh) return `${sh[1]}.SH`;
                    const sz = /^SZ(\d{6})$/.exec(s);
                    if (sz) return `${sz[1]}.SZ`;
                    return s;
                }),
        [rawCodes],
    );

    const [executionConfig, setExecutionConfig] = useState<ExecutionConfig>(DEFAULT_EXECUTION_CONFIG);
    const [liveTradeConfig, setLiveTradeConfig] = useState<LiveTradeConfig>(DEFAULT_LIVE_TRADE_CONFIG);
    const issues = useMemo(() => validateLiveTradeConfig(liveTradeConfig), [liveTradeConfig]);

    const options = channelOptions();

    return (
        <>
            <Card
                title="推送下单"
                extra={
                    <span className="text-xs text-slate-500">
                        {lastDone ? `上次推送 ${lastDone}` : '走 push-orders 预检 → 确认 → 执行'}
                    </span>
                }
            >
                <div className="space-y-3">
                    <textarea
                        value={rawCodes}
                        onChange={(e) => setRawCodes(e.target.value)}
                        rows={3}
                        spellCheck={false}
                        placeholder="每行一个代码，如 600036 或 600036.SH（自动归一到后缀式）"
                        className="w-full rounded-xl border border-slate-200 p-3 font-mono text-sm outline-none focus:border-indigo-400"
                    />

                    <div className="flex flex-wrap items-center gap-3">
                        <div className="flex items-center gap-1">
                            {(['buy', 'sell'] as PushSide[]).map((s) => (
                                <button
                                    key={s}
                                    type="button"
                                    onClick={() => setSide(s)}
                                    className={`rounded-lg px-3 py-1.5 text-sm ${
                                        side === s
                                            ? 'bg-slate-900 text-white'
                                            : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
                                    }`}
                                >
                                    {s === 'buy' ? '买入' : '卖出'}
                                </button>
                            ))}
                        </div>

                        <div className="flex items-center gap-1">
                            {options.map((o) => {
                                const on = channels.includes(o.value);
                                return (
                                    <button
                                        key={o.value}
                                        type="button"
                                        title={o.hint}
                                        onClick={() =>
                                            // 不用函数式 setter（本仓 tsc 下必报错），
                                            // 直接读本次渲染的 channels
                                            setChannels(
                                                channels.includes(o.value)
                                                    ? channels.filter((c) => c !== o.value)
                                                    : [...channels, o.value],
                                            )
                                        }
                                        className={`rounded-lg px-3 py-1.5 text-sm ${
                                            on
                                                ? 'bg-rose-600 text-white'
                                                : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
                                        }`}
                                    >
                                        {o.label}
                                    </button>
                                );
                            })}
                        </div>

                        <button
                            type="button"
                            disabled={symbols.length === 0}
                            onClick={() => setPanelOpen(true)}
                            className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-1.5 text-sm text-white disabled:bg-slate-300"
                        >
                            <Send className="h-4 w-4" />
                            预检并推送（{symbols.length}）
                        </button>
                    </div>

                    {!liveEnabled && (
                        <p className="text-xs text-amber-700">
                            实盘开关关闭：通道只剩「仅模拟盘」。开启需两端同时打开——
                            后端 <code>ENABLE_REAL_TRADING=true</code>，前端构建时{' '}
                            <code>VITE_ENABLE_REAL_TRADING=true</code>。
                        </p>
                    )}
                </div>
            </Card>

            <Card
                title="滚动调仓配置"
                extra={<span className="text-xs text-slate-500">本地草稿 —— 落库走「策略管理 → 策略下发」</span>}
            >
                <LiveTradeConfigForm
                    market={undefined}
                    executionConfig={executionConfig}
                    liveTradeConfig={liveTradeConfig}
                    onExecutionConfigChange={setExecutionConfig}
                    onLiveTradeConfigChange={setLiveTradeConfig}
                    validationIssues={issues}
                />
            </Card>

            {/* modal 仅在打开时挂载：PushConfirmPanel 打开即发 preflight，
                常驻挂载会一直空跑请求（同 SettingsCenter 对券商卡的教训）。 */}
            {panelOpen && (
                <PushConfirmPanel
                    open={panelOpen}
                    symbols={symbols}
                    side={side}
                    channels={channels}
                    onChannelsChange={setChannels}
                    onClose={() => setPanelOpen(false)}
                    onDone={() => {
                        setLastDone(new Date().toLocaleTimeString('zh-CN'));
                        setPanelOpen(false);
                    }}
                />
            )}
        </>
    );
};

// ─────────────────────────────────────────────────────────────────────────────
// 追加页签：QuantBot（内嵌 dsh 智能体）
// ─────────────────────────────────────────────────────────────────────────────

/**
 * 与 `/quantbot` 整页**同一个 iframe、同一套加载/超时遮罩**（都在 QuantBotFrame）：
 * 这里只少一层页面级顶栏 —— 交易台的侧栏与顶栏已经在承担导航，再套一层就是两个头。
 * 连接状态不用另画：连不上时 QuantBotSurface 自己会盖「未响应 + 重新加载」的遮罩。
 */
const QuantBotTab: React.FC = () => {
    // hook 必须落在组件里：写进 extraTabs 的 render 回调等于把它挂到 RealTradingPage 上
    const frame = useQuantBotFrame();
    return (
        <QuantBotSurface
            frame={frame}
            className="relative h-full w-full overflow-hidden bg-white"
        />
    );
};

// ─────────────────────────────────────────────────────────────────────────────
// 「风控止损」：持仓监控右栏里的一块（2026-09-23 用户要求，不再是侧栏一栏）
//
// 用户口径「风控止损放持仓监控那」：这块答的是「现在的止损到底是多少、有没有被锁」，
// 而止损最终是**平掉持仓**的动作 —— 与持仓分开两页看，就永远对不上号（同 2026-09-20
// 把哨兵/副驾驶并进持仓监控的理由）。宿主页 `PositionMonitor` 提供右栏容器
// （380px 窄栏 / 窄屏整行网格），这里只出内容，**不再自带 TabPane**。
// ─────────────────────────────────────────────────────────────────────────────

const RiskPanel: React.FC<{ ctx: RealTradingTabContext }> = ({ ctx }) => (
    <div className="space-y-2">
        <div className="flex items-center gap-2 text-[11px] text-slate-500">
            <ShieldCheck className="h-3.5 w-3.5 shrink-0" />
            阈值口径与后端 /risk-status 同源；只读，不改风控参数（改参数走「策略管理」）。
        </div>
        {/* 不再传 compact（2026-10-09）：宿主右栏已随用户「一半一半」要求改为与左板
            1:1（≥2xl ~2×380px），紧凑两列列宽反而稀疏；四指标恢复 lg:grid-cols-4。
            窄栏场景在现布局下已不存在——堆叠态的右栏卡也 ≥490px。 */}
        <RiskLayer status={ctx.status} enabled />
    </div>
);

// ─────────────────────────────────────────────────────────────────────────────
// 页面：把实盘专属栏挂到与模拟交易同一个控制台上
// ─────────────────────────────────────────────────────────────────────────────

const LiveTradingPage: React.FC = () => {
    // 取一次快照：构建期常量（`VITE_ENABLE_REAL_TRADING`），但不在下面直接内联调用
    const liveEnabled = isLiveTradingEnabled();

    // 追加页签 id 必须与基础 9 栏不重名，否则 composeConsoleTabs 抛错（有意为之）。
    // 「券商接入」「大 QMT 镜像」不在此列：它们已经在设置栏里，用户明确要求别重复。
    //
    // QuantBot 排第一条＝侧栏里正好落在「设置」下面（2026-09-22 用户要求：不做左侧
    // 常驻栏，做成侧栏一栏、内容照常走右边内容区）。
    const extraTabs = useMemo<RealTradingExtraTab[]>(
        () => [
            {
                id: 'quantbot',
                label: 'QuantBot',
                icon: Bot,
                // 只隐藏不卸载：dsh 在 iframe 里靠自己的 WebSocket 推流，
                // 切到持仓/委托看一眼再切回来不该断线重来。
                keepMounted: true,
                render: () => <QuantBotTab />,
            },
            // 「推送下单」「风控止损」**不在这里**（2026-09-23 用户要求「推送下单放手动
            // 任务、风控止损放持仓监控那」）：它们各自并进宿主页，走下面的
            // `manualTaskExtras` / `positionRailExtras` 两个槽位，侧栏不再占位。
            // arena（Quant Agent Trader）整棵移植过来的四栏：智能体台账 / 盘中实况 /
            // 行情回测 / 关于。顺序按用户 2026-09-22 点名的顺序，关于排在最后。
            // 总控与数据不在这里 —— 它们是设置页里的两块（见下面 settingsPanels）。
            ...ARENA_TABS,
        ],
        [],
    );

    // hooks 全部调用完再判，避免条件式 hook（该值在单次构建内是常量，不会来回切）
    if (!liveEnabled) return <LiveDisabledPage />;

    return (
        <RealTradingPage
            forcedTradingMode="real"
            // 顶栏右侧的市场切换（用户 2026-10-08：「实盘交易顶部增加 A 股、港股、美股的切换」）。
            // 复用全局切换器（`components/layout/MarketSelector`）＝同一份 marketFlags 过滤与
            // 同一份 redux 状态：这里切一下，宿主页的账户/持仓/交易记录/设置里的券商卡全部跟着走
            // （RealTradingPage 的取数 deps 本来就含 currentMarket），不需要另写一套。
            topBarExtras={<MarketSelector />}
            extraTabs={extraTabs}
            banner={(ctx) => <AccountNoticeBanner ctx={ctx} />}
            // 设置页里的两块：总控 / 数据（用户口径「总控放设置里面、数据也放设置里面」）
            settingsPanels={ARENA_SETTINGS_PANELS}
            // 并进宿主页的两块（2026-09-23 用户要求）：
            // 「推送下单」→ 手动任务页向导下方；「风控止损」→ 持仓监控右栏。
            // 回调只返回元素、不调 hook（它在本页渲染里被调用，hook 会挂到本页上）。
            manualTaskExtras={() => <PushOrderPanel />}
            positionRailExtras={(ctx) => <RiskPanel ctx={ctx} />}
        />
    );
};

export default LiveTradingPage;
