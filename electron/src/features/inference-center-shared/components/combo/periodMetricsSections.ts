/**
 * 周期收益 · 机构级指标条（纯数据，不含 JSX）。
 *
 * 三组：收益（含已实现/浮动拆分与基准/超额）、风险、交易。
 * 缺失一律 `—`；基准覆盖率不足 95% 时后端已置空，这里不补 0。
 */

import type { BlendPeriodSummary } from '../../../../services/blendTypes';
import { formatAmount, formatReturnPct, returnToneClass } from './comboRankingModel';
import { formatPct, formatSignedAmount, formatWinRate } from './periodReturnModel';
import {
  drawdownWindowLabel,
  formatDays,
  formatMultiple,
  formatRatio,
} from './portfolioMetricsModel';
import type { MetricItem, MetricSection } from './PortfolioMetricsStrip';

function benchmarkHint(
  summary: BlendPeriodSummary,
  benchmarkName?: string | null,
): string {
  const label = benchmarkName || '本市场基准';
  const coverage = summary.benchmark_coverage_pct;
  if (typeof coverage === 'number' && Number.isFinite(coverage)) {
    return `${label} 按日收益复利；区间覆盖率 ${coverage.toFixed(0)}%（<95% 时基准与超额置 —）`;
  }
  return benchmarkName
    ? `${label} 行情不可用：基准与超额一律显示 —，不写 0`
    : '本市场基准未接入：基准与超额一律显示 —，不写 0';
}

/** 金额 + 收益率（%）；收益率缺失时只给金额 */
function amountWithPct(amount: number, pct: number | null | undefined): string {
  return `${formatSignedAmount(amount)} · ${formatPct(pct)}`;
}

export function buildPeriodMetricSections(
  summary: BlendPeriodSummary,
  benchmarkName?: string | null,
): MetricSection[] {
  const drawdown = drawdownWindowLabel(summary);
  const returns: MetricItem[] = [
    {
      key: 'total',
      label: '总收益',
      value: formatReturnPct(summary.total_return_pct),
      tone: returnToneClass(summary.total_return_pct),
      emphasis: 'hero',
      testId: 'combo-period-total-return',
      hint: '期末权益 ÷ 初始资金 − 1（含已实现与浮动）',
    },
    {
      key: 'profit',
      label: '收益金额',
      value: formatSignedAmount(summary.profit_amount),
      tone: returnToneClass(summary.profit_amount),
      testId: 'combo-period-profit',
    },
    {
      key: 'equity',
      label: '期末权益',
      value: formatAmount(summary.final_equity),
      hint: `初始资金 ${formatAmount(summary.initial_capital)}`,
    },
    {
      key: 'realized',
      label: '已实现',
      value: amountWithPct(summary.realized_pnl_amount, summary.realized_return_pct),
      tone: returnToneClass(summary.realized_pnl_amount),
      testId: 'combo-period-realized',
      hint: '已了结交易累计损益（退出日收盘价），收益率 = 损益 ÷ 初始资金',
    },
    {
      key: 'unrealized',
      label: '浮动',
      value: amountWithPct(summary.unrealized_pnl_amount, summary.unrealized_return_pct),
      tone: returnToneClass(summary.unrealized_pnl_amount),
      testId: 'combo-period-unrealized',
      hint: '期末在场持仓按最后有效收盘价估值；未到期不打折扣',
    },
    {
      key: 'benchmark',
      label: '基准',
      value: formatReturnPct(summary.benchmark_return_pct),
      tone: returnToneClass(summary.benchmark_return_pct),
      hint: benchmarkHint(summary, benchmarkName),
      testId: 'combo-period-benchmark',
    },
    {
      key: 'excess',
      label: '超额',
      value: formatReturnPct(summary.excess_return_pct),
      tone: returnToneClass(summary.excess_return_pct),
      hint: '总收益 − 基准收益；基准不可用时显示 —',
      testId: 'combo-period-excess',
    },
  ];

  const risk: MetricItem[] = [
    {
      key: 'annualized',
      label: '年化',
      value: formatPct(summary.annualized_return_pct),
      tone: returnToneClass(summary.annualized_return_pct),
      hint: '按 244 个交易日/年折算（区间不足一年时仅供横向参考）',
    },
    { key: 'volatility', label: '波动率', value: formatPct(summary.volatility_pct) },
    { key: 'sharpe', label: '夏普', value: formatRatio(summary.sharpe_ratio) },
    { key: 'sortino', label: '索提诺', value: formatRatio(summary.sortino_ratio) },
    { key: 'calmar', label: '卡玛', value: formatRatio(summary.calmar_ratio) },
    {
      key: 'drawdown',
      label: '最大回撤',
      value: formatPct(summary.max_drawdown_pct),
      tone: returnToneClass(summary.max_drawdown_pct),
      hint: drawdown ?? '权益曲线峰值到谷值的最大跌幅',
      testId: 'combo-period-drawdown',
    },
    {
      key: 'tracking',
      label: '跟踪误差',
      value: formatPct(summary.tracking_error_pct),
      hint: '组合与基准日超额收益的年化标准差；基准不可用时 —',
    },
    {
      key: 'ir',
      label: '信息比率',
      value: formatRatio(summary.information_ratio),
      hint: '年化超额收益 ÷ 跟踪误差；基准不可用时 —',
    },
  ];

  const trades: MetricItem[] = [
    {
      key: 'win',
      label: '胜率',
      value: formatWinRate(summary.win_rate_pct),
      hint: `盈 ${summary.wins} / 亏 ${summary.losses} / 平 ${summary.flats}（已了结交易）`,
    },
    {
      key: 'profit_factor',
      label: '盈利因子',
      value: formatRatio(summary.profit_factor),
      hint: `总盈利 ${formatSignedAmount(summary.gross_profit_amount)} ÷ |总亏损 ${formatSignedAmount(summary.gross_loss_amount)}|`,
    },
    {
      key: 'payoff',
      label: '盈亏比',
      value: formatRatio(summary.payoff_ratio),
      hint: `平均盈利 ${formatPct(summary.avg_win_pct)} ÷ |平均亏损 ${formatPct(summary.avg_loss_pct)}|`,
    },
    {
      key: 'holding',
      label: '平均持有',
      value: formatDays(summary.avg_holding_days),
    },
    {
      key: 'counts',
      label: '已了结/在场',
      value: `${summary.closed_trades} / ${summary.open_positions}`,
    },
    {
      key: 'turnover',
      label: '换手',
      value: formatMultiple(summary.turnover_x),
      hint: '建仓名义金额合计 ÷ 初始资金',
    },
    {
      key: 'exposure',
      label: '平均仓位',
      value: formatPct(summary.avg_exposure_pct),
      hint: '每日持仓市值 ÷ 权益的均值',
    },
    {
      key: 'cash',
      label: '期末现金',
      value: formatPct(summary.final_cash_pct),
      hint: `日均在场 ${summary.avg_positions.toFixed(1)} 只（持仓上限内留有余量）`,
    },
  ];

  return [
    { key: 'returns', title: '收益', items: returns },
    { key: 'risk', title: '风险', items: risk },
    { key: 'trades', title: '交易', items: trades },
  ];
}
