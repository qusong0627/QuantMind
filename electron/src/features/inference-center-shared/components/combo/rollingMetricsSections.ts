/**
 * 滚动建仓 · 每日批次口径的机构级指标条（纯数据，不含 JSX）。
 *
 * 口径：入选日收盘按等权预算建仓（受 ST/退市与成交额门槛约束，跳过不替补）
 * → 次一交易日收盘结算；最后一批只估值不结算（计入浮动）。
 * `realized + unrealized == final_equity − initial_capital`。
 * 胜率/盈亏比只统计**已结算批次**，未结算批次不得提前计入。
 */

import type {
  BlendRollingFilters,
  BlendRollingPerformance,
} from '../../../../services/blendTypes';
import { formatAmount, formatReturnPct, returnToneClass } from './comboRankingModel';
import { formatPct, formatSignedAmount, formatWinRate } from './periodReturnModel';
import { formatCount, formatDays, formatMultiple, formatRatio } from './portfolioMetricsModel';
import type { MetricItem, MetricSection } from './PortfolioMetricsStrip';

function benchmarkHint(
  performance: BlendRollingPerformance,
  benchmarkName?: string | null,
): string {
  const label = benchmarkName || '本市场基准';
  const coverage = performance.benchmark_coverage_pct;
  if (typeof coverage === 'number' && Number.isFinite(coverage)) {
    return `${label} 按日收益复利；区间覆盖率 ${coverage.toFixed(0)}%（<95% 时基准与超额置 —）`;
  }
  return benchmarkName
    ? `${label} 行情不可用：基准与超额一律显示 —，不写 0`
    : '本市场基准未接入：基准与超额一律显示 —，不写 0';
}

function amountWithPct(amount: number, pct: number | null | undefined): string {
  return `${formatSignedAmount(amount)} · ${formatPct(pct)}`;
}

/** 批次计价明细；「无建仓」只在新后端给了计数时追加（旧后端整句保持一致） */
function batchCountsHint(performance: BlendRollingPerformance): string {
  const parts = [
    `已计价 ${formatCount(performance.priced_batches)}`,
    `缺价 ${formatCount(performance.unpriced_batches)}`,
    `部分缺价 ${formatCount(performance.partial_batches)}`,
  ];
  if (
    typeof performance.no_position_batches === 'number' &&
    Number.isFinite(performance.no_position_batches)
  ) {
    parts.push(`无建仓 ${formatCount(performance.no_position_batches)} 批`);
  }
  return parts.join(' · ');
}

/** 成交额门槛（亿元）；缺失 → `—` */
function advYiLabel(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const digits = value >= 10 ? 0 : value >= 1 ? (Number.isInteger(value) ? 0 : 1) : 2;
  return `${value.toFixed(digits)} 亿`;
}

/** 参与率（比例 → 百分比）；缺失 → `—` */
function participationLabel(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const pct = value * 100;
  return `${pct.toFixed(pct >= 10 ? 0 : 1)}%`;
}

/** 该市场是否有统一的成交额/ST 口径（目前仅 A 股） */
function marketSupported(filters: BlendRollingFilters | undefined): boolean {
  return !filters?.market || filters.market === 'CN';
}

/**
 * 执行约束（可交易性）指标：建仓覆盖 / 容量约束 / ST 过滤 / 跳过明细。
 *
 * 旧后端不含这些字段时**整段省略**（不是渲染四个 `—`）；
 * 只在字段确实存在时展示，缺失的内部数值仍按 `—` 处理。
 * 非 A 股或显式关闭 ST 过滤时，「未启用」不等于数据源故障，文案必须说清。
 */
function tradabilityItems(
  performance: BlendRollingPerformance,
  filters?: BlendRollingFilters,
): MetricItem[] {
  const items: MetricItem[] = [];
  const evaluated = performance.evaluated_picks;
  const bought = performance.bought_positions;
  if (typeof evaluated === 'number' && Number.isFinite(evaluated)) {
    const boughtCount =
      typeof bought === 'number' && Number.isFinite(bought) ? bought : null;
    const coverage =
      boughtCount !== null && evaluated > 0 ? (boughtCount / evaluated) * 100 : null;
    items.push({
      key: 'coverage',
      label: '建仓覆盖',
      value:
        coverage === null
          ? '—'
          : `${formatCount(boughtCount)} / ${formatCount(evaluated)}（${coverage.toFixed(0)}%）`,
      hint: '各批次入选标的里实际建仓的只数 ÷ 评估只数；跳过的标的不替补，对应资金留作现金',
      testId: 'combo-rolling-coverage',
    });
  }

  if (typeof performance.capacity_applied === 'boolean') {
    const unsupported = !marketSupported(filters);
    items.push({
      key: 'capacity',
      label: '容量约束',
      value: performance.capacity_applied
        ? `${advYiLabel(performance.min_adv_yi)} · 参与率 ${participationLabel(
            performance.adv_participation,
          )}`
        : unsupported
          ? '不适用'
          : '未启用',
      hint: performance.capacity_applied
        ? `单只建仓金额 = min(等权预算, ADV${formatCount(
            performance.adv_lookback_days,
          )} × 参与率)；ADV 至少 ${formatCount(
            performance.min_adv_days,
          )} 个有效交易日才参与判定，成交额低于门槛的不建仓`
        : unsupported
          ? '该市场暂无统一成交额口径：本轮按等权预算估算，未施加容量约束'
          : '成交额数据不可用：本轮未校验容量，按等权预算估算',
      testId: 'combo-rolling-capacity',
    });
  }

  if (typeof performance.st_filter_applied === 'boolean') {
    const unsupported = !marketSupported(filters);
    const disabledByRequest = filters?.exclude_st === false;
    items.push({
      key: 'st',
      label: 'ST 过滤',
      value: performance.st_filter_applied
        ? '已启用'
        : unsupported
          ? '不适用'
          : disabledByRequest
            ? '已关闭'
            : '未启用',
      hint: performance.st_filter_applied
        ? '按当前证券主表的 ST/退市标记剔除；主表只有近期截面，历史重算属于静态口径（含前视偏差）'
        : unsupported
          ? '该市场暂无统一的 ST/退市标记：本轮未剔除，结果可能包含风险警示标的'
          : disabledByRequest
            ? '本轮请求 exclude_st=false（手动关闭）：未剔除 ST/退市标的'
            : 'ST/退市数据源不可用：本轮未剔除，结果可能包含风险警示标的',
      testId: 'combo-rolling-st-filter',
    });
  }

  const skipRows: Array<[string, number | null | undefined]> = [
    ['ST/退市', performance.skipped_st],
    ['成交额不足', performance.skipped_low_adv],
    ['缺成交额', performance.skipped_no_amount],
    ['缺入选价', performance.skipped_no_price],
  ];
  if (skipRows.some(([, value]) => typeof value === 'number' && Number.isFinite(value))) {
    const total = skipRows.reduce(
      (sum, [, value]) =>
        sum + (typeof value === 'number' && Number.isFinite(value) ? value : 0),
      0,
    );
    items.push({
      key: 'skips',
      label: '跳过',
      value: `${formatCount(total)} 只`,
      hint: skipRows.map(([label, value]) => `${label} ${formatCount(value)}`).join(' · '),
      testId: 'combo-rolling-skips',
    });
  }

  return items;
}

export function buildRollingMetricSections(
  performance: BlendRollingPerformance,
  filters?: BlendRollingFilters,
  benchmarkName?: string | null,
): MetricSection[] {
  const returns: MetricItem[] = [
    {
      key: 'total',
      label: '窗口收益',
      value: formatReturnPct(performance.total_return_pct),
      tone: returnToneClass(performance.total_return_pct),
      emphasis: 'hero',
      testId: 'combo-rolling-total-return',
      hint: '期末权益 ÷ 初始资金 − 1（含已实现与浮动）；每日按等权预算建仓，受 ST/成交额门槛约束',
    },
    {
      key: 'profit',
      label: '收益金额',
      value: formatSignedAmount(performance.profit_amount),
      tone: returnToneClass(performance.profit_amount),
      testId: 'combo-rolling-profit',
    },
    {
      key: 'equity',
      label: '期末权益',
      value: formatAmount(performance.final_equity),
      hint: `初始资金 ${formatAmount(performance.initial_capital)}`,
    },
    {
      key: 'realized',
      label: '已实现',
      value: amountWithPct(performance.realized_pnl_amount, performance.realized_return_pct),
      tone: returnToneClass(performance.realized_pnl_amount),
      testId: 'combo-rolling-realized',
      hint: '除最后一批外，每日批次已在次一交易日收盘结算；收益率 = 损益 ÷ 初始资金',
    },
    {
      key: 'unrealized',
      label: '浮动',
      value: amountWithPct(
        performance.unrealized_pnl_amount,
        performance.unrealized_return_pct,
      ),
      tone: returnToneClass(performance.unrealized_pnl_amount),
      testId: 'combo-rolling-unrealized',
      hint: '最后一个批次只在末日收盘估值，未结算；末日入选不建仓',
    },
    {
      key: 'benchmark',
      label: '基准',
      value: formatReturnPct(performance.benchmark_return_pct),
      tone: returnToneClass(performance.benchmark_return_pct),
      hint: benchmarkHint(performance, benchmarkName),
      testId: 'combo-rolling-benchmark',
    },
    {
      key: 'excess',
      label: '超额',
      value: formatReturnPct(performance.excess_return_pct),
      tone: returnToneClass(performance.excess_return_pct),
      hint: '窗口收益 − 基准收益；基准不可用时显示 —',
      testId: 'combo-rolling-excess',
    },
  ];

  const risk: MetricItem[] = [
    {
      key: 'annualized',
      label: '年化',
      value: formatPct(performance.annualized_return_pct),
      tone: returnToneClass(performance.annualized_return_pct),
      hint: '按 244 个交易日/年折算；窗口较短时仅供横向参考',
    },
    { key: 'volatility', label: '波动率', value: formatPct(performance.volatility_pct) },
    { key: 'sharpe', label: '夏普', value: formatRatio(performance.sharpe_ratio) },
    { key: 'sortino', label: '索提诺', value: formatRatio(performance.sortino_ratio) },
    { key: 'calmar', label: '卡玛', value: formatRatio(performance.calmar_ratio) },
    {
      key: 'drawdown',
      label: '最大回撤',
      value: formatPct(performance.max_drawdown_pct),
      tone: returnToneClass(performance.max_drawdown_pct),
      hint: '逐日权益峰值到谷值的最大跌幅',
      testId: 'combo-rolling-drawdown',
    },
    {
      key: 'tracking',
      label: '跟踪误差',
      value: formatPct(performance.tracking_error_pct),
      hint: '组合与基准日超额收益的年化标准差；基准不可用时 —',
    },
    {
      key: 'ir',
      label: '信息比率',
      value: formatRatio(performance.information_ratio),
      hint: `年化超额收益 ÷ 跟踪误差；日均超额 ${formatPct(performance.avg_daily_excess_pct)}`,
    },
  ];

  const batches: MetricItem[] = [
    {
      key: 'counts',
      label: '批次',
      // 字段缺失（旧后端 / 半截载荷）一律 `—`，绝不把 undefined 渲染到界面
      value: `${formatCount(performance.batches)}（结算 ${formatCount(
        performance.settled_batches,
      )} · 浮动 ${formatCount(performance.open_batches)}）`,
      hint: batchCountsHint(performance),
      // 与明细表 `combo-rolling-batches` 区分：testid 重复会让探针/测试选择器歧义
      testId: 'combo-rolling-batch-counts',
    },
    {
      key: 'win',
      label: '批次胜率',
      value: formatWinRate(performance.win_rate_pct),
      hint: `盈 ${formatCount(performance.wins)} / 亏 ${formatCount(
        performance.losses,
      )} / 平 ${formatCount(performance.flats)}（仅已结算批次）`,
    },
    {
      key: 'profit_factor',
      label: '盈利因子',
      value: formatRatio(performance.profit_factor),
      hint: `总盈利 ${formatSignedAmount(performance.gross_profit_amount)} ÷ |总亏损 ${formatSignedAmount(performance.gross_loss_amount)}|`,
    },
    {
      key: 'payoff',
      label: '盈亏比',
      value: formatRatio(performance.payoff_ratio),
      hint: `平均盈利 ${formatPct(performance.avg_win_pct)} ÷ |平均亏损 ${formatPct(performance.avg_loss_pct)}|`,
    },
    { key: 'avg', label: '平均批次收益', value: formatReturnPct(performance.avg_return_pct) },
    {
      key: 'holding',
      label: '平均持有',
      value: formatDays(performance.avg_holding_days),
      hint: '每日换仓，批次持有 1 个交易日',
    },
    {
      key: 'turnover',
      label: '换手',
      value: formatMultiple(performance.turnover_x),
      hint: '每日建仓名义金额合计 ÷ 初始资金',
    },
    {
      key: 'exposure',
      label: '平均仓位',
      value: formatPct(performance.avg_exposure_pct),
      hint: '各批次建仓金额 ÷ 当日权益；跳过标的与成交额上限会让仓位低于 100%，余额留现金',
    },
    {
      key: 'cash',
      label: '期末现金',
      value: formatPct(performance.final_cash_pct),
    },
  ];

  const tradability = tradabilityItems(performance, filters);
  const sections: MetricSection[] = [
    { key: 'returns', title: '收益', items: returns },
    { key: 'risk', title: '风险', items: risk },
    { key: 'batches', title: '批次', items: batches },
  ];
  if (tradability.length > 0) {
    sections.push({ key: 'tradability', title: '执行', items: tradability });
  }
  return sections;
}
