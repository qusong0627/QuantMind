import type { AccountInfo, RealTradingStatus } from '../../../services/realTradingService';

export type TradingAccountMode = 'real' | 'simulation';
export type TradingRuntimeMode = 'REAL' | 'SHADOW' | 'SIMULATION' | 'UNKNOWN';
export type TradingAccountSource = 'postgresql' | 'simulation' | 'empty';

export interface TradingAccountSelection {
  mode: TradingAccountMode;
  runtimeMode: TradingRuntimeMode;
  source: TradingAccountSource;
  account: AccountInfo | null;
}

export interface TradingTopBarAccountInfo {
  total_asset: number;
  initial_equity: number;
  day_open_equity: number;
  month_open_equity: number;
  cash: number;
  market_value: number;
  frozen: number;
  daily_pnl: number;
  daily_pnl_percent: number;
  floating_pnl: number;
  floating_pnl_percent: number;
  total_pnl: number;
  total_pnl_percent: number;
  position_ratio: number;
  position_count: number;
}

const toFiniteNumber = (value: unknown, fallback = 0): number => {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
};

const pickPreferredRatio = (candidates: unknown[], derivedRatio: number): number => {
  for (const value of candidates) {
    if (value === null || value === undefined) continue;
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) continue;
    if (Math.abs(parsed) > 1e-8) {
      return parsed;
    }
    if (Math.abs(derivedRatio) <= 1e-8) {
      return 0;
    }
  }
  return derivedRatio;
};

const pickConsistentRatio = (
  candidates: unknown[],
  derivedRatio: number,
  options?: {
    requireDerived?: boolean;
    maxDiff?: number;
  },
): number => {
  const requireDerived = options?.requireDerived ?? false;
  const maxDiff = options?.maxDiff ?? 0.0005;
  const preferred = pickPreferredRatio(candidates, derivedRatio);
  if (!Number.isFinite(derivedRatio)) {
    return preferred;
  }
  if (requireDerived) {
    return derivedRatio;
  }
  if (!Number.isFinite(preferred)) {
    return derivedRatio;
  }
  return Math.abs(preferred - derivedRatio) > maxDiff ? derivedRatio : preferred;
};

const resolveRuntimeMode = (runtimeMode?: string | null): TradingRuntimeMode => {
  const normalized = String(runtimeMode || '').trim().toUpperCase();
  if (normalized === 'REAL' || normalized === 'SHADOW' || normalized === 'SIMULATION') {
    return normalized;
  }
  return 'UNKNOWN';
};

export const resolveTradingAccountMode = (
  runtimeMode?: string | null,
  preferredMode: TradingAccountMode = 'real',
): TradingAccountMode => {
  // 核心逻辑变更：优先尊重用户在界面上的手动选择，从而实现模拟账户数据的独立切换预览。
  // 不再因为后端正在运行模拟策略而强制锁定前端视图口径。
  return preferredMode;
};

export const selectTradingAccount = (params: {
  runtimeMode?: string | null;
  preferredMode?: TradingAccountMode;
  realAccount?: AccountInfo | null;
  simulationAccount?: AccountInfo | null;
}): TradingAccountSelection => {
  const runtimeMode = resolveRuntimeMode(params.runtimeMode);
  const mode = resolveTradingAccountMode(params.runtimeMode, params.preferredMode ?? 'real');
  const account = mode === 'simulation'
    ? (params.simulationAccount ?? null)
    : (params.realAccount ?? null);

  return {
    mode,
    runtimeMode,
    source: account ? (mode === 'simulation' ? 'simulation' : 'postgresql') : 'empty',
    account,
  };
};

const extractPositionCount = (accountInfo: AccountInfo | null): number => {
  if (!accountInfo) return 0;
  const directCount = toFiniteNumber((accountInfo as any)?.position_count, Number.NaN);
  if (Number.isFinite(directCount)) return directCount;
  const positions = accountInfo.positions;
  // dict 口径只计有量的有效持仓（与后端 position_count / 组合快照一致）
  if (positions && typeof positions === 'object' && !Array.isArray(positions)) {
    return Object.values(positions).filter((pos: any) => {
      if (!pos || typeof pos !== 'object') return false;
      const vol = pos.volume ?? pos.qty ?? pos.quantity ?? pos.total_volume;
      return vol === undefined || Number(vol) > 0;
    }).length;
  }
  return Array.isArray(positions)
    ? positions.length
    : Object.keys(positions || {}).length;
};

const deriveFloatingPnl = (accountInfo: AccountInfo | null): number => {
  if (!accountInfo) return 0;
  const explicitFloating = toFiniteNumber((accountInfo as any)?.floating_pnl, Number.NaN);
  if (Number.isFinite(explicitFloating)) return explicitFloating;

  // 持仓有两种形态：实盘/组合快照是数组，模拟账户（/simulation/account）是
  // dict[symbol, position]。只认数组时，模拟盘持仓再多也会恒显示 0.00。
  const raw = (accountInfo as any)?.positions;
  const positions: any[] = Array.isArray(raw)
    ? raw
    : (raw && typeof raw === 'object' ? Object.values(raw) : []);
  if (positions.length === 0) return 0;

  return positions.reduce((sum: number, pos: any) => {
    if (!pos || typeof pos !== 'object') return sum;
    const volume = toFiniteNumber(pos.volume ?? pos.qty ?? pos.quantity ?? pos.total_volume, 0);
    let lastPrice = toFiniteNumber(pos.last_price ?? pos.price ?? pos.current_price, 0);
    const costPrice = toFiniteNumber(pos.cost_price ?? pos.avg_cost ?? pos.avg_price ?? pos.cost, 0);
    // 缺现价时用市值反推（模拟账户存量行可能只带 market_value）
    if (lastPrice <= 0) {
      const marketValue = toFiniteNumber(pos.market_value, 0);
      lastPrice = volume > 0 && marketValue > 0 ? marketValue / volume : 0;
    }
    if (volume <= 0 || lastPrice <= 0 || costPrice <= 0) return sum;
    const side = String(pos.side ?? 'long').trim().toLowerCase();
    const pnlPerShare = side === 'short' ? (costPrice - lastPrice) : (lastPrice - costPrice);
    return sum + (pnlPerShare * volume);
  }, 0);
};

export const buildTradingTopBarAccountInfo = (
  accountInfo: AccountInfo | null,
  status: RealTradingStatus | null,
): TradingTopBarAccountInfo => {
  // 检查账户是否未初始化（模拟盘未创建）
  const isNotInitialized = (accountInfo as any)?.account_not_initialized === true;

  // 如果账户未初始化，返回全零数据
  if (isNotInitialized) {
    return {
      total_asset: 0,
      initial_equity: 0,
      day_open_equity: 0,
      month_open_equity: 0,
      cash: 0,
      market_value: 0,
      frozen: 0,
      daily_pnl: 0,
      daily_pnl_percent: 0,
      floating_pnl: 0,
      floating_pnl_percent: 0,
      total_pnl: 0,
      total_pnl_percent: 0,
      position_ratio: 0,
      position_count: 0,
    };
  }

  const reportedTotalAsset = toFiniteNumber(accountInfo?.total_asset, 0);
  const cash = toFiniteNumber(accountInfo?.cash ?? (accountInfo as any)?.available_cash, 0);
  const marketValue = toFiniteNumber(accountInfo?.market_value, 0);
  // 单一口径：桥/客户端上报的 total_asset 与持仓市值来自不同刷新时点
  // （2026-11-23 实测：asset 930,295.74 vs 现金+市值 928,409.74，差 1,886 ≈
  // 601825 两分钱 × 94,300 股）。权益与市值必须同源，否则同一张卡上
  // 「账户权益」与「持仓市值」互相矛盾（差 1 分钱 × 持股数）。
  const cashPlusMarket = cash + marketValue;
  const equityMismatch =
    Math.abs(reportedTotalAsset - cashPlusMarket) > 1 &&
    (cash > 0 || marketValue > 0);
  const totalAsset = equityMismatch ? cashPlusMarket : reportedTotalAsset;
  const frozen = toFiniteNumber((accountInfo as any)?.frozen ?? (accountInfo as any)?.frozen_cash, 0);
  const baselineDayOpenEquity = toFiniteNumber((accountInfo as any)?.baseline?.day_open_equity, Number.NaN);
  const baselineMonthOpenEquity = toFiniteNumber((accountInfo as any)?.baseline?.month_open_equity, Number.NaN);
  const accountDayOpenEquity = toFiniteNumber((accountInfo as any)?.day_open_equity, Number.NaN);
  const accountMonthOpenEquity = toFiniteNumber((accountInfo as any)?.month_open_equity, Number.NaN);
  const statusInitialCapital = toFiniteNumber(status?.portfolio?.initial_capital, Number.NaN);

  const accountTodayPnl = toFiniteNumber((accountInfo as any)?.today_pnl, Number.NaN);
  const accountDailyPnl = toFiniteNumber((accountInfo as any)?.daily_pnl, Number.NaN);
  const accountTotalPnl = toFiniteNumber((accountInfo as any)?.total_pnl, Number.NaN);
  const accountFloatingPnl = toFiniteNumber((accountInfo as any)?.floating_pnl, Number.NaN);
  const accountDailyReturnRatio = toFiniteNumber((accountInfo as any)?.daily_return_ratio, Number.NaN);
  const accountDailyReturnPct = toFiniteNumber((accountInfo as any)?.daily_return_pct, Number.NaN);
  const accountDailyReturnLegacy = toFiniteNumber((accountInfo as any)?.daily_return, Number.NaN);
  const accountTotalReturnRatio = toFiniteNumber((accountInfo as any)?.total_return_ratio, Number.NaN);
  const accountTotalReturnPct = toFiniteNumber((accountInfo as any)?.total_return_pct, Number.NaN);
  const accountTotalReturnLegacy = toFiniteNumber((accountInfo as any)?.total_return, Number.NaN);
  const statusDailyPnl = toFiniteNumber(status?.daily_pnl ?? status?.portfolio?.daily_pnl, 0);
  const statusTotalPnl = toFiniteNumber(status?.portfolio?.total_pnl, 0);
  const rawDailyReturn = status?.portfolio?.daily_return != null
    ? status.portfolio.daily_return / 100
    : (status?.daily_return != null ? (status.daily_return as number) / 100 : Number.NaN);

  const serviceDailyPnl = Number.isFinite(accountDailyPnl)
    ? accountDailyPnl
    : (Number.isFinite(accountTodayPnl) ? accountTodayPnl : statusDailyPnl);
  const serviceTotalPnl = Number.isFinite(accountTotalPnl) ? accountTotalPnl : statusTotalPnl;
  const floatingPnl = Number.isFinite(accountFloatingPnl) ? accountFloatingPnl : deriveFloatingPnl(accountInfo);
  const positionCount = extractPositionCount(accountInfo) || toFiniteNumber(status?.portfolio?.position_count, 0);
  const explicitInitialEquity = toFiniteNumber((accountInfo as any)?.initial_equity, Number.NaN);
  const initialEquity = Number.isFinite(explicitInitialEquity)
    ? explicitInitialEquity
    : (Number.isFinite(totalAsset) && Number.isFinite(serviceTotalPnl)
      ? Math.max(0, totalAsset - serviceTotalPnl)
      : (Number.isFinite(statusInitialCapital) ? statusInitialCapital : 0));
  const dayOpenEquity = Number.isFinite(baselineDayOpenEquity)
    ? baselineDayOpenEquity
    : (Number.isFinite(accountDayOpenEquity)
      ? accountDayOpenEquity
      : Math.max(0, totalAsset - serviceDailyPnl));
  const monthOpenEquity = Number.isFinite(baselineMonthOpenEquity)
    ? baselineMonthOpenEquity
    : (Number.isFinite(accountMonthOpenEquity)
      ? accountMonthOpenEquity
      : (Number.isFinite(statusInitialCapital)
        ? statusInitialCapital
        : Math.max(0, totalAsset - serviceTotalPnl)));
  // 权益归一时盈亏同步重算，保证「权益 − 开盘权益 = 今日盈亏」在卡片内闭环；
  // 未归一时仍用服务端权威值，不改变原有语义。
  const dailyPnl = equityMismatch && dayOpenEquity > 0
    ? totalAsset - dayOpenEquity
    : serviceDailyPnl;
  const totalPnl = equityMismatch && initialEquity > 0
    ? totalAsset - initialEquity
    : serviceTotalPnl;
  const derivedDailyPnlPercent = dayOpenEquity > 0
    ? (dailyPnl / dayOpenEquity)
    : (totalAsset > 0 ? (dailyPnl / totalAsset) : 0);
  const dailyPnlPercent = pickConsistentRatio(
    [
      accountDailyReturnRatio,
      Number.isFinite(accountDailyReturnPct) ? accountDailyReturnPct / 100 : Number.NaN,
      Number.isFinite(accountDailyReturnLegacy) ? accountDailyReturnLegacy / 100 : Number.NaN,
      Number.isFinite(rawDailyReturn) ? rawDailyReturn : Number.NaN,
    ],
    derivedDailyPnlPercent,
    {
      requireDerived: dayOpenEquity > 0 && Number.isFinite(dailyPnl),
    },
  );
  const floatingPnlPercent = marketValue > 0
    ? (floatingPnl / marketValue)
    : (totalAsset > 0 ? (floatingPnl / totalAsset) : 0);
  const derivedTotalPnlPercent = initialEquity > 0
    ? (totalPnl / initialEquity)
    : (totalAsset > 0 ? (totalPnl / totalAsset) : 0);
  const totalPnlPercent = pickConsistentRatio(
    [
      accountTotalReturnRatio,
      Number.isFinite(accountTotalReturnPct) ? accountTotalReturnPct / 100 : Number.NaN,
      Number.isFinite(accountTotalReturnLegacy) ? accountTotalReturnLegacy / 100 : Number.NaN,
    ],
    derivedTotalPnlPercent,
    {
      requireDerived: initialEquity > 0 && Number.isFinite(totalPnl),
    },
  );
  const positionRatio = totalAsset > 0 ? (marketValue / totalAsset) : 0;

  return {
    total_asset: totalAsset,
    initial_equity: initialEquity,
    day_open_equity: dayOpenEquity,
    month_open_equity: monthOpenEquity,
    cash,
    market_value: marketValue,
    frozen,
    daily_pnl: dailyPnl,
    daily_pnl_percent: dailyPnlPercent,
    floating_pnl: floatingPnl,
    floating_pnl_percent: floatingPnlPercent,
    total_pnl: totalPnl,
    total_pnl_percent: totalPnlPercent,
    position_ratio: positionRatio,
    position_count: positionCount,
  };
};
