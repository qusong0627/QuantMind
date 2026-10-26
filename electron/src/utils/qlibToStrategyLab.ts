/**
 * qlib 回测结果 → StrategyLabRunResult 形状。
 *
 * 目的：策略编辑右栏只保留一套结果视图（净值/K线/月度/成交），
 * 不再混用 `QlibResultDisplay` 与 `StrategyLabResultPanel` 两套外观。
 * 只做字段映射，不改变任何数值口径。
 */
import type { BacktestResult } from '../services/backtestService';
import type { QlibBacktestResult } from '../types/backtest/qlib';
import type {
  StrategyLabEquityPoint,
  StrategyLabPositionSnapshot,
  StrategyLabRunResult,
  StrategyLabTradeRecord,
} from '../features/strategy-lab/types';

type Dict = Record<string, unknown>;

const num = (value: unknown, fallback = 0): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const text = (value: unknown, fallback = ''): string =>
  value === null || value === undefined ? fallback : String(value);

const asDict = (value: unknown): Dict =>
  value && typeof value === 'object' ? (value as Dict) : {};

function mapEquity(result: Dict): StrategyLabEquityPoint[] {
  const raw = Array.isArray(result.equity_curve) ? result.equity_curve : [];
  return raw
    .map((item) => {
      const row = asDict(item);
      return {
        date: text(row.date),
        value: num(row.value),
        benchmark: null,
      };
    })
    .filter((point) => point.date.length > 0);
}

function mapTrades(result: Dict): StrategyLabTradeRecord[] {
  const raw = Array.isArray(result.trades)
    ? result.trades
    : Array.isArray(result.trade_list)
      ? result.trade_list
      : [];
  return raw.map((item) => {
    const row = asDict(item);
    const action = text(row.action || row.side || row.direction).toUpperCase();
    return {
      date: text(row.date || row.datetime || row.executed_at),
      symbol: text(row.symbol || row.code),
      direction: action.includes('SELL') ? 'SELL' : 'BUY',
      price: num(row.price || row.executed_price),
      qty: num(row.quantity ?? row.qty ?? row.executed_size),
      reason: text(row.reason),
      detail: {
        commission: num(row.commission ?? row.executed_commission),
        amount: num(row.totalAmount ?? row.amount),
      },
      pnl: row.pnl === null || row.pnl === undefined ? null : num(row.pnl),
    };
  });
}

function mapPositions(result: Dict): StrategyLabPositionSnapshot[] {
  const raw = Array.isArray(result.positions) ? result.positions : [];
  return raw.map((item) => {
    const row = asDict(item);
    return {
      date: text(row.date),
      symbol: text(row.symbol || row.code),
      qty: num(row.qty ?? row.quantity ?? row.volume),
      cost: num(row.cost),
      market_value: num(row.market_value),
      pnl_pct: num(row.pnl_pct),
    };
  });
}

export function qlibResultToStrategyLabResult(
  result: BacktestResult | QlibBacktestResult,
): StrategyLabRunResult {
  const raw = result as unknown as Dict;
  const trades = mapTrades(raw);
  const status = text(raw.status).toLowerCase();
  return {
    run_id: text(raw.backtest_id, 'qlib'),
    status:
      status === 'completed'
        ? 'success'
        : status === 'failed'
          ? 'failed'
          : status === 'cancelled'
            ? 'cancelled'
            : 'running',
    metrics: {
      cum_return: num(raw.total_return ?? raw.cumulative_return),
      annual_return: num(raw.annual_return),
      sharpe: num(raw.sharpe_ratio),
      // StrategyLab 口径展示正回撤幅度；qlib 原始为负值
      max_drawdown: Math.abs(num(raw.max_drawdown)),
      win_rate: num(raw.win_rate),
      n_trades: num(raw.total_trades, trades.length),
      avg_position: 0,
    },
    equity: mapEquity(raw),
    trades,
    positions: mapPositions(raw),
    overlays: { lines: [], markers: [] },
    logs: [],
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map((w) => text(w)) : [],
    error: raw.error_message ? text(raw.error_message) : null,
    error_traceback: raw.full_error ? text(raw.full_error) : null,
    config: asDict(raw.config),
    script_sha: '',
    data_snapshot_at: null,
    elapsed_sec: num(raw.execution_time),
    started_at: 0,
    finished_at: 0,
  };
}
