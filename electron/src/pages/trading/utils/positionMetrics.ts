import { AccountInfo } from '../../../services/realTradingService';
import { 
    RawPosition, 
    StockNameMap, 
    normalizePositions, 
    resolveCode, 
    resolveName,
    toFiniteNumber, 
    toPositiveNumber 
} from '../../../utils/portfolioUtils';

export interface NormalizedHolding {
    code: string;
    name: string;
    shares: number;
    cost: number;
    current: number;
    profit: number;
    profitPercent: number;
    value: number;
    /** 有股数，但现价与市值都拿不到（上游补价失败 / 停牌 / 退市）。
     *
     *  这类行的盈亏是「不知道」，不是 0 —— 按 0 市值算会得出 −100%。渲染层遇到
     *  `priceMissing` 一律出 `—`，合计也不把它算进去。（2026-09-22 实盘持仓监控
     *  显示 −84,778.90 就是这里把缺价读成了亏光。） */
    priceMissing: boolean;
}

export interface PositionSummary {
    totalAsset: number;
    cashValue: number;
    positionValue: number;
    positionRatio: number;
    cashRatio: number;
}

export const extractPositionCodes = (accountInfo: AccountInfo | null): string[] => {
    const rows = normalizePositions(accountInfo);
    return Array.from(new Set(rows.map(({ key, pos }) => resolveCode(key, pos)).filter(Boolean)));
};

export const getPositionSummary = (
    accountInfo: AccountInfo | null,
    holdings?: NormalizedHolding[],
): PositionSummary => {
    const totalAsset = toFiniteNumber(accountInfo?.total_asset, 0);
    const cashValue = toFiniteNumber(
        (accountInfo as any)?.cash ?? (accountInfo as any)?.available_cash,
        0,
    );
    // 传入合并实时价后的持仓时，汇总跟随重算（与明细同口径），否则用账户旧市值
    const accountMarketValue = toFiniteNumber(accountInfo?.market_value, 0);
    const holdingsValue = holdings
        ? holdings.reduce((sum, h) => sum + toFiniteNumber(h.value, 0), 0)
        : 0;
    // 持仓行合计为 0 而账户口径有市值 → 是行内缺价，不是「没有持仓」。
    // 回落账户口径，否则总市值 KPI 会跟着显示成 0（同下一条 safeTotalAsset 的兜底思路）。
    const positionValue = holdings
        ? holdingsValue > 0 || accountMarketValue <= 0
            ? holdingsValue
            : accountMarketValue
        : accountMarketValue;
    const safeTotalAsset = totalAsset > 0 ? totalAsset : (cashValue + positionValue);
    const positionRatio = safeTotalAsset > 0 ? (positionValue / safeTotalAsset) * 100 : 0;
    const cashRatio = safeTotalAsset > 0 ? (cashValue / safeTotalAsset) * 100 : 0;

    return {
        totalAsset: safeTotalAsset,
        cashValue,
        positionValue,
        positionRatio,
        cashRatio,
    };
};

export const buildNormalizedHoldings = (
    accountInfo: AccountInfo | null,
    stockNames: StockNameMap = {},
): NormalizedHolding[] => {
    const rows = normalizePositions(accountInfo);
    return rows
        .map(({ key, pos }) => {
            const code = resolveCode(key, pos);
            const shares = toFiniteNumber(pos.volume ?? pos.qty ?? pos.quantity ?? pos.total_volume, 0);
            const rawValue = toFiniteNumber(pos.market_value, 0);
            const derivedCurrent = shares > 0 ? rawValue / shares : 0;
            const current = toPositiveNumber(
                pos.last_price ?? pos.current_price ?? pos.price,
                derivedCurrent,
            );
            // 只补了一半（有价、没市值）时按价折市值，免得把「市值缺失」读成「市值 0」
            const value = rawValue > 0 || !(current > 0) ? rawValue : shares * current;

            const providedCost = toPositiveNumber(
                pos.cost_price ?? pos.avg_cost ?? pos.avg_price ?? pos.cost,
                NaN,
            );
            const providedProfit = toFiniteNumber(
                pos.unrealized_pnl ?? pos.float_pnl ?? pos.pnl,
                NaN,
            );

            let cost = Number.isFinite(providedCost) ? providedCost : 0;
            if (cost <= 0 && Number.isFinite(providedProfit) && shares > 0) {
                cost = (value - providedProfit) / shares;
            }
            if (cost <= 0 && Number.isFinite(current) && current > 0 && shares > 0) {
                cost = current;
            }

            // 缺价：有股数却既没价也没市值。盈亏是「不知道」——
            // 按 0 市值算会得到 −100%，那正是把缺价画成亏光。
            const priceMissing = shares > 0 && value <= 0;

            const costValue = shares * cost;
            const profit = priceMissing
                ? 0
                : Number.isFinite(providedProfit)
                  ? providedProfit
                  : value - costValue;
            const profitPercent = priceMissing || costValue <= 0 ? 0 : (profit / costValue) * 100;

            return {
                code,
                name: resolveName(code, pos, stockNames),
                shares,
                cost,
                current,
                profit,
                profitPercent,
                value,
                priceMissing,
            };
        })
        .filter((item) => item.shares > 0 || item.value > 0)
        .sort((a, b) => b.value - a.value);
};
