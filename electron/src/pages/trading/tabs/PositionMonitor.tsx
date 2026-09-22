import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AccountInfo } from '../../../services/realTradingService';
import { marketDataService } from '../../../services/marketDataService';
import { websocketService, MessageType } from '../../../services/websocketService';
import { buildNormalizedHoldings, extractPositionCodes, getPositionSummary, NormalizedHolding } from '../utils/positionMetrics';
import { PositionVisualBoard, ExecutionStrip } from '../components/PositionVisualBoard';
import { PositionSourceBar } from '../components/PositionSourceBar';
import { HoldingAlertPanel } from '../../../features/holding-alerts/HoldingAlertPanel';
import { CopilotPanel } from '../../../features/desk/components/CopilotPanel';
import type { TradingAccountMode } from '../utils/accountAdapter';
import { isLiveTradingEnabled } from '../../../config/tradingFlags';

interface PositionMonitorProps {
    userId: string;
    isActive: boolean;
    accountInfo: AccountInfo | null;
    /** 当前看的是哪个账户（页面顶栏 模拟/实盘）。卖出预检的默认通道跟随它——
     *  看实盘持仓却默认发模拟腿，会因为「模拟账户没有这只票」逐笔被拦。 */
    accountMode?: TradingAccountMode;
}

/** stream 服务推送的实时行情消息（topic stock.{code}） */
interface LiveQuote {
    stock_code: string;
    data?: {
        price?: number | null;
        open?: number | null;
        high?: number | null;
        low?: number | null;
        is_stale?: boolean;
        timestamp?: string | number;
    };
}

/** 窄屏（不足 2xl 并排）时主卡的高度：主卡内部要有个确定高度才能自己滚动 */
const STACKED_BOARD_MIN_H = 'h-[560px]';

/** 持仓明细叠加实时价：现价/市值/盈亏全部按 live price 重算 */
const mergeLivePrices = (holdings: NormalizedHolding[], live: Record<string, number>): NormalizedHolding[] => {
    return holdings.map(h => {
        const price = live[h.code];
        if (price == null || !Number.isFinite(price) || price <= 0) return h;
        const value = h.shares * price;
        const profit = h.cost > 0 ? (price - h.cost) * h.shares : 0;
        const costValue = h.shares * h.cost;
        return {
            ...h,
            current: price,
            value,
            profit,
            profitPercent: costValue > 0 ? (profit / costValue) * 100 : 0,
            // 实时价到了就不再是缺价行 —— 不清这个标记会让拿到价的行继续显示 —
            priceMissing: false,
        };
    });
};

const PositionMonitor: React.FC<PositionMonitorProps> = ({ userId: _userId, isActive, accountInfo, accountMode = 'simulation' }) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const [stockNames, setStockNames] = useState<Record<string, string>>({});
    const [livePrices, setLivePrices] = useState<Record<string, number>>({});
    const livePricesRef = useRef<Record<string, number>>({});
    const subscribedRef = useRef<string[]>([]);

    React.useEffect(() => {
        if (!accountInfo || !accountInfo.positions) return;

        const codes = extractPositionCodes(accountInfo).filter(code => !stockNames[code]);
        if (codes.length === 0) return;

        const fetchNames = async () => {
            try {
                const results = await marketDataService.getStockDetailsBatch(codes, 10, 50);
                const newNames: Record<string, string> = {};
                results.forEach(({ code, result }) => {
                    if (result.success && result.data?.name) {
                        newNames[code] = result.data.name;
                    }
                });
                if (Object.keys(newNames).length > 0) {
                    (setStockNames as any)(prev => ({ ...prev, ...newNames }));
                }
            } catch (err) {
                console.error('Failed to fetch stock names in batch:', err);
            }
        };
        fetchNames();
    }, [accountInfo, stockNames]);

    // 订阅持仓股实时行情（topic stock.{code}，stream 服务 2s 推一次）
    useEffect(() => {
        if (!isActive) return;
        const codes = extractPositionCodes(accountInfo);
        if (codes.length === 0) return;
        const toSubscribe = codes.filter(c => !subscribedRef.current.includes(c));
        if (toSubscribe.length === 0) return;
        subscribedRef.current = [...subscribedRef.current, ...toSubscribe];
        websocketService.subscribe({ symbols: toSubscribe });
    }, [isActive, accountInfo]);

    useEffect(() => {
        if (!isActive) return;
        const handler = (data: unknown) => {
            const msg = data as LiveQuote;
            const code = String(msg?.stock_code || '').toUpperCase();
            const price = Number(msg?.data?.price);
            if (!code || !Number.isFinite(price) || price <= 0) return;
            const next = { ...livePricesRef.current, [code]: price };
            livePricesRef.current = next;
            setLivePrices(next);
        };
        websocketService.addMessageHandler('quote' as MessageType, handler);
        return () => {
            websocketService.removeMessageHandler('quote' as MessageType, handler);
        };
    }, [isActive]);

    // 退页时退订持仓行情
    useEffect(() => {
        if (isActive || subscribedRef.current.length === 0) return;
        websocketService.unsubscribe(subscribedRef.current);
        subscribedRef.current = [];
    }, [isActive]);

    const holdings = React.useMemo(() => {
        return mergeLivePrices(buildNormalizedHoldings(accountInfo, stockNames), livePrices);
    }, [accountInfo, stockNames, livePrices]);

    // 行情源采样的标的集 = 当前显示的持仓（换源后自动变成另一个账户的持仓）
    const positionCodes = React.useMemo(() => extractPositionCodes(accountInfo), [accountInfo]);

    const summary = React.useMemo(
        () => getPositionSummary(accountInfo, holdings),
        [accountInfo, holdings],
    );

    if (!isActive) return null;

    return (
        <div className="h-full p-2.5 pb-[50px] flex flex-col gap-2">
            {/* 来源条（2026-09-20 重做）：账户源 chips（看哪个券商账户，可切换视图/交易券商）
                + 行情源（按持仓标的采样 market:snapshot 的 source 字段，如实报主供数与并写源）。
                旧版这里读的是 TDX 持仓馈送的心跳（bridge_ok），与真正供数方无关 —— 桥与
                QMT 备源轮转时文案来回切，正是用户报的第二个现象。 */}
            <PositionSourceBar
                market={currentMarket}
                isActive={isActive}
                accountMode={accountMode}
                accountSource={accountInfo?.account_source}
                sourceDowngraded={accountInfo?.source_downgraded}
                sourceDowngradedReason={accountInfo?.source_downgraded_reason}
                positionCodes={positionCodes}
            />

            {/* 两栏（2026-09-20 用户要求「持仓监控放 2 栏」）：
                左 = 持仓本体（KPI/明细/图表 + 今日执行），右 = 风险与情报栏（哨兵 + 副驾驶）。
                这两块原本挂在「系统健康」页脚：它们讲的全是**持仓**的风险与盘中情报，
                跟持仓明细分在两页看，等于风险永远和平仓对不上号；且刻意不按市场门控——
                切到港股/美股页签时 A 股持仓的风险并不会消失。
                并排只在 ≥2xl 生效：主卡内部还有固定的图表列（430~470px），
                再窄并排会把明细列压到读不了；不足 2xl 时右栏落到下方、两卡并排。 */}
            <div className="flex-1 min-h-0 flex flex-col 2xl:flex-row gap-2 overflow-y-auto 2xl:overflow-hidden custom-scrollbar">
                <div className={`${STACKED_BOARD_MIN_H} shrink-0 min-w-0 flex flex-col gap-2 2xl:h-auto 2xl:min-h-0 2xl:flex-1 2xl:shrink`}>
                    {/* 持仓可视化主卡（2026-09-17 重设计：KPI + 市值占比条形列表，替代 分布饼图+宽表格 两块） */}
                    <PositionVisualBoard
                        holdings={holdings}
                        summary={summary}
                        defaultChannels={
                            // 实盘开关关闭时恒为模拟盘：这里是实盘关闭后最容易漏的一处——
                            // 账户态仍可能是 real（历史数据），默认通道若不跟着收敛，
                            // 一进持仓页就预勾了实盘镜像
                            accountMode === 'real' && isLiveTradingEnabled() ? ['sim', 'real'] : ['sim']
                        }
                    />
                    {/* 今日执行折叠条（并入同页，不再单起卡片） */}
                    <ExecutionStrip />
                </div>

                {/* 右栏：风险与情报。grid 让「不足 2xl」时两卡并排（4 列 → 2 列），
                    ≥2xl 收成单列窄栏并各自内部滚动（不撑破页面高度）。 */}
                <aside
                    data-testid="position-rail"
                    aria-label="持仓风险与情报"
                    className="shrink-0 min-h-0 grid content-start grid-cols-1 xl:grid-cols-2 2xl:grid-cols-1 gap-2 2xl:w-[380px] 2xl:overflow-y-auto custom-scrollbar"
                >
                    <HoldingAlertPanel />
                    <CopilotPanel />
                    {/* 悬浮 Dock 是覆盖层不占布局（memory: dock-overlay-bottom-clearance）：
                        窄屏时右栏铺满整行、底部会被 Dock 吞掉，滚动末端补实体占位块。 */}
                    <div
                        aria-hidden
                        className="h-[max(12px,calc(var(--dock-height)-12px))] shrink-0 2xl:hidden"
                    />
                </aside>
            </div>
        </div>
    );
};

export default PositionMonitor;
