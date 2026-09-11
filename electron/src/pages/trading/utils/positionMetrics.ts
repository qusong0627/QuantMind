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
    const positionValue = holdings
        ? holdings.reduce((sum, h) => sum + toFiniteNumber(h.value, 0), 0)
        : toFiniteNumber(accountInfo?.market_value, 0);
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
            const value = toFiniteNumber(pos.market_value, 0);
            const derivedCurrent = shares > 0 ? value / shares : 0;
            const current = toPositiveNumber(
                pos.last_price ?? pos.current_price ?? pos.price,
                derivedCurrent,
            );

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

            const costValue = shares * cost;
            const profit = Number.isFinite(providedProfit) ? providedProfit : (value - costValue);
            const profitPercent = costValue > 0 ? (profit / costValue) * 100 : 0;

            return {
                code,
                name: resolveName(code, pos, stockNames),
                shares,
                cost,
                current,
                profit,
                profitPercent,
                value,
            };
        })
        .filter((item) => item.shares > 0 || item.value > 0)
        .sort((a, b) => b.value - a.value);
};
