/**
 * 组合选股 · 周期收益（固定持有期滚动桶）。
 *
 * 用户口径（2026-10-21）：
 * - 给建仓日与截止日（可拉到半年/自定义），用**已保存组合的模板参数**
 *   （每日新进 × 持有天数）滚动建仓；
 * - 初始资金按持仓上限等分，只改「资金 / 持仓上限 / 区间」；
 * - 收盘价建仓与退出，**不计手续费、印花税与滑点**；
 * - 输出总收益、月度拆解、权益曲线与交易明细，用于评估组合综合选股能力。
 *
 * 机构级统计（2026-10-21 追加）：已实现与浮动拆分、基准（本市场主基准，A股=沪深300）与超额、
 * 年化/波动/夏普/索提诺/卡玛、最大回撤窗口、跟踪误差与信息比率、盈利因子与盈亏比、
 * 换手与平均仓位；数据端口（pred 测试集 / 推理台账）与可用区间常驻展示。
 *
 * 展示面合规：不出现「买入/卖出」，统一说「建仓/退出/入选」；
 * 红涨绿跌（A 股口径）；缺失值一律 `—`，绝不显示成 0。
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  DatePicker,
  Empty,
  InputNumber,
  Spin,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { type Dayjs } from 'dayjs';
import { Calculator, RefreshCw } from 'lucide-react';
import type {
  BlendBounds,
  BlendPeriodMonth,
  BlendPeriodOpen,
  BlendPeriodQuery,
  BlendPeriodResponse,
  BlendPeriodTrade,
  BlendRecord,
} from '../../../../services/blendTypes';
import {
  formatAmount,
  formatPrice,
  formatReturnPct,
  returnToneClass,
} from './comboRankingModel';
import {
  PERIOD_QUICK_RANGES,
  buildBenchmarkEquitySeries,
  buildComparisonGeometry,
  formatPct,
  formatSignedAmount,
  formatWinRate,
  periodQuickQuery,
  periodRangeLabel,
  periodWarningLabel,
  type PeriodQuickRange,
} from './periodReturnModel';
import { DataSourceBadge, PortfolioMetricsStrip } from './PortfolioMetricsStrip';
import { buildPeriodMetricSections } from './periodMetricsSections';
import './comboTable.css';

const { RangePicker } = DatePicker;

const DEFAULT_CAPITAL_WAN = 1000;
const MAX_POSITIONS = 50;
const MIN_POSITIONS = 1;
const EQUITY_WIDTH = 640;
const EQUITY_HEIGHT = 160;

function clampPositions(value: number | null | undefined): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return MAX_POSITIONS;
  return Math.min(MAX_POSITIONS, Math.max(MIN_POSITIONS, Math.round(value)));
}

export interface PeriodReturnPanelProps {
  blend: BlendRecord | null;
  period: BlendPeriodResponse | null;
  loading: boolean;
  error: string | null;
  /** 可用评分日（降序），用于「全部」快捷区间兜底 */
  dates: readonly string[];
  /** 后端 `/bounds`：pred 测试集 / 推理台账的实际可用区间 */
  bounds?: BlendBounds | null;
  onCalculate: (query: BlendPeriodQuery) => void;
  onRetry: () => void;
}

export const PeriodReturnPanel: React.FC<PeriodReturnPanelProps> = ({
  blend,
  period,
  loading,
  error,
  dates,
  bounds,
  onCalculate,
  onRetry,
}) => {
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [capitalWan, setCapitalWan] = useState<number | null>(DEFAULT_CAPITAL_WAN);
  const [positions, setPositions] = useState<number | null>(MAX_POSITIONS);

  // 换组合：表单回到模板默认值（后端默认 end=最新评分日、start=end-180 天）
  useEffect(() => {
    setRange(null);
    setCapitalWan(DEFAULT_CAPITAL_WAN);
    setPositions(clampPositions(blend?.target_positions));
  }, [blend?.blend_id, blend?.target_positions]);

  // 新结果落地：表单回显后端实际采用的参数（建仓日被夹取后也一致）
  useEffect(() => {
    if (!period) return;
    setRange([dayjs(period.requested.start), dayjs(period.requested.end)]);
    setCapitalWan(period.requested.capital / 10000);
    setPositions(period.requested.positions);
  }, [period]);

  const buildQuery = (): BlendPeriodQuery => {
    const query: BlendPeriodQuery = {};
    if (range?.[0]) query.start = range[0].format('YYYY-MM-DD');
    if (range?.[1]) query.end = range[1].format('YYYY-MM-DD');
    if (capitalWan && Number.isFinite(capitalWan) && capitalWan > 0) {
      query.capital = capitalWan * 10000;
    }
    if (positions) query.positions = clampPositions(positions);
    return query;
  };

  const handleCalculate = () => onCalculate(buildQuery());

  // 快捷区间锚点：优先 pred 测试集的实际区间（可回看到 2025），否则退回可用评分日
  const effectiveBounds = period?.bounds ?? bounds ?? null;
  const anchor = useMemo(
    () => ({
      start: effectiveBounds?.effective.start ?? null,
      end: effectiveBounds?.effective.end ?? null,
    }),
    [effectiveBounds],
  );

  const handleQuick = (kind: PeriodQuickRange) => {
    const query = periodQuickQuery(kind, dates, anchor);
    if (query.start && query.end) setRange([dayjs(query.start), dayjs(query.end)]);
    else if (query.end) setRange([null, dayjs(query.end)]);
    // 快捷区间只换日期，资金/持仓上限仍取当前表单值
    onCalculate({ ...buildQuery(), ...query });
  };

  const summary = period?.summary ?? null;
  const template = period?.template ?? null;
  const benchmarkSeries = useMemo(
    () =>
      period && summary
        ? buildBenchmarkEquitySeries(period.daily, summary.initial_capital)
        : [],
    [period, summary],
  );
  const geometry = useMemo(
    () =>
      period && summary
        ? buildComparisonGeometry(
            period.daily,
            benchmarkSeries,
            summary.initial_capital,
            EQUITY_WIDTH,
            EQUITY_HEIGHT,
          )
        : null,
    [period, summary, benchmarkSeries],
  );
  const metricSections = useMemo(
    () =>
      summary
        ? buildPeriodMetricSections(summary, period?.benchmark?.name)
        : [],
    [summary, period?.benchmark?.name],
  );

  const monthColumns: ColumnsType<BlendPeriodMonth> = [
    {
      title: '月份',
      dataIndex: 'month',
      width: 92,
      fixed: 'left',
      render: (value: string, row) => (
        <span className="flex items-center gap-1">
          <span className="font-mono text-[11px]">{value}</span>
          {row.partial && <Tag className="!m-0 !text-[9px] !leading-tight">不完整</Tag>}
        </span>
      ),
    },
    {
      title: '月收益',
      dataIndex: 'return_pct',
      width: 88,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono font-bold ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '基准',
      dataIndex: 'benchmark_return_pct',
      width: 80,
      align: 'right',
      render: (value: number | null) => (
        <span className="font-mono text-[11px] text-slate-600">
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '超额',
      dataIndex: 'excess_return_pct',
      width: 84,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono text-[11px] font-bold ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '收益金额',
      dataIndex: 'profit_amount',
      width: 104,
      align: 'right',
      render: (value: number) => (
        <span className={`font-mono ${returnToneClass(value)}`}>
          {formatSignedAmount(value)}
        </span>
      ),
    },
    {
      title: '已实现 / 浮动',
      key: 'pnl_split',
      width: 168,
      align: 'right',
      render: (_value, row) => (
        <span className="font-mono text-[10px] whitespace-nowrap">
          <span className={returnToneClass(row.realized_pnl_amount)}>
            {formatSignedAmount(row.realized_pnl_amount)}
          </span>
          <span className="px-0.5 text-slate-300">/</span>
          <span className={returnToneClass(row.unrealized_pnl_amount)}>
            {formatSignedAmount(row.unrealized_pnl_amount)}
          </span>
        </span>
      ),
    },
    {
      title: '区间',
      key: 'span',
      width: 160,
      render: (_value, row) => (
        <span className="font-mono text-[10px] text-slate-500">
          {row.start_date} ~ {row.end_date}
        </span>
      ),
    },
    {
      title: '建仓 / 了结',
      key: 'trades',
      width: 100,
      align: 'right',
      render: (_value, row) => (
        <span className="font-mono text-[11px]">
          {row.entries} / {row.closed_trades}
        </span>
      ),
    },
    {
      title: '月末在场',
      dataIndex: 'month_end_positions',
      width: 84,
      align: 'right',
      render: (value: number) => <span className="font-mono text-[11px]">{value}</span>,
    },
    {
      title: '胜率',
      dataIndex: 'win_rate_pct',
      width: 76,
      align: 'right',
      render: (value: number | null) => (
        <span className="font-mono text-[11px] text-slate-600">{formatWinRate(value)}</span>
      ),
    },
  ];

  const tradeColumns: ColumnsType<BlendPeriodTrade> = [
    {
      title: '代码',
      dataIndex: 'code',
      width: 88,
      render: (value: string, row) => value || row.symbol,
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 96,
      render: (value: string) => value || '—',
    },
    {
      title: '建仓日',
      dataIndex: 'entry_date',
      width: 100,
      render: (value: string, row) => (
        <span className="font-mono text-[10px]">
          {value}
          {row.deferred && (
            <Tooltip title="到期日缺价，顺延到下一个有效收盘价退出">
              <span className="ml-1 text-amber-600">顺延</span>
            </Tooltip>
          )}
        </span>
      ),
    },
    {
      title: '退出日',
      dataIndex: 'exit_date',
      width: 100,
      render: (value: string) => <span className="font-mono text-[10px]">{value}</span>,
    },
    {
      title: '建仓价',
      dataIndex: 'entry_price',
      width: 82,
      align: 'right',
      render: (value: number) => <span className="font-mono">{formatPrice(value)}</span>,
    },
    {
      title: '退出价',
      dataIndex: 'exit_price',
      width: 82,
      align: 'right',
      render: (value: number) => <span className="font-mono">{formatPrice(value)}</span>,
    },
    {
      title: '收益',
      dataIndex: 'return_pct',
      width: 88,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono font-bold ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '收益金额',
      dataIndex: 'pnl_amount',
      width: 104,
      align: 'right',
      render: (value: number) => (
        <span className={`font-mono ${returnToneClass(value)}`}>
          {formatSignedAmount(value)}
        </span>
      ),
    },
    {
      title: '持有',
      dataIndex: 'held_days',
      width: 64,
      align: 'right',
      render: (value: number) => <span className="font-mono text-[11px]">{value} 日</span>,
    },
  ];

  const openColumns: ColumnsType<BlendPeriodOpen> = [
    {
      title: '代码',
      dataIndex: 'code',
      width: 88,
      render: (value: string, row) => value || row.symbol,
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 96,
      render: (value: string) => value || '—',
    },
    {
      title: '建仓日',
      dataIndex: 'entry_date',
      width: 100,
      render: (value: string) => <span className="font-mono text-[10px]">{value}</span>,
    },
    {
      title: '估值日',
      dataIndex: 'mark_date',
      width: 100,
      render: (value: string, row) => (
        <span className="font-mono text-[10px]">
          {value}
          {row.stale && (
            <Tooltip title="期末缺最新收盘价，沿用上一有效价">
              <span className="ml-1 text-amber-600">沿用</span>
            </Tooltip>
          )}
        </span>
      ),
    },
    {
      title: '建仓价',
      dataIndex: 'entry_price',
      width: 82,
      align: 'right',
      render: (value: number) => <span className="font-mono">{formatPrice(value)}</span>,
    },
    {
      title: '最新价',
      dataIndex: 'mark_price',
      width: 82,
      align: 'right',
      render: (value: number) => <span className="font-mono">{formatPrice(value)}</span>,
    },
    {
      title: '浮动收益',
      dataIndex: 'return_pct',
      width: 92,
      align: 'right',
      render: (value: number | null) => (
        <span className={`font-mono font-bold ${returnToneClass(value)}`}>
          {formatReturnPct(value)}
        </span>
      ),
    },
    {
      title: '浮动金额',
      dataIndex: 'pnl_amount',
      width: 104,
      align: 'right',
      render: (value: number) => (
        <span className={`font-mono ${returnToneClass(value)}`}>
          {formatSignedAmount(value)}
        </span>
      ),
    },
    {
      title: '持有',
      dataIndex: 'held_days',
      width: 64,
      align: 'right',
      render: (value: number) => <span className="font-mono text-[11px]">{value} 日</span>,
    },
  ];

  if (error) {
    return (
      <div className="p-3" data-testid="combo-period-error">
        <Alert
          type="error"
          showIcon
          message="周期收益计算失败"
          description={<span className="break-all">{error}</span>}
          action={
            <Button size="small" loading={loading} onClick={onRetry}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  if (loading && !period) {
    return (
      <div className="h-full min-h-0 min-w-0 flex items-center justify-center bg-white border border-slate-200 rounded-xl">
        <Spin tip="按模板滚动计算周期收益…" />
      </div>
    );
  }

  return (
    <div
      className="h-full min-h-0 min-w-0 flex flex-col bg-white border border-slate-200 rounded-xl overflow-hidden"
      data-testid="combo-period-root"
    >
      <div className="shrink-0 flex flex-wrap items-center gap-2 px-3 py-2 border-b border-slate-100">
        <span className="text-[11px] text-slate-500">建仓日 → 截止日</span>
        <RangePicker
          size="small"
          className="!w-60"
          value={range}
          onChange={(value) => setRange(value as [Dayjs | null, Dayjs | null] | null)}
          allowEmpty={[true, true]}
          placeholder={['建仓日', '截止日']}
        />
        <span className="text-[11px] text-slate-500">初始资金（万）</span>
        <InputNumber
          size="small"
          className="!w-24"
          min={1}
          step={100}
          value={capitalWan}
          onChange={(value) => setCapitalWan(typeof value === 'number' ? value : null)}
          suffix="万"
          data-testid="combo-period-capital"
        />
        <span className="text-[11px] text-slate-500">持仓上限</span>
        <InputNumber
          size="small"
          className="!w-20"
          min={MIN_POSITIONS}
          max={MAX_POSITIONS}
          value={positions}
          onChange={(value) => setPositions(typeof value === 'number' ? value : null)}
          suffix="只"
          data-testid="combo-period-positions"
        />
        <Button
          size="small"
          type="primary"
          icon={<Calculator size={13} />}
          loading={loading}
          onClick={handleCalculate}
          data-testid="combo-period-calculate"
        >
          计算
        </Button>
        <div className="flex items-center gap-1">
          {PERIOD_QUICK_RANGES.map((item) => (
            <Button
              key={item.key}
              size="small"
              type="text"
              className="!px-1.5 !text-[11px]"
              disabled={loading}
              onClick={() => handleQuick(item.key)}
              data-testid={`combo-period-quick-${item.key}`}
            >
              {item.label}
            </Button>
          ))}
        </div>
        <span className="ml-auto">
          <DataSourceBadge
            source={period?.source ?? effectiveBounds?.source ?? null}
            bounds={effectiveBounds}
            benchmark={period?.benchmark ?? null}
            testId="combo-period-source"
          />
        </span>
        <Button
          size="small"
          icon={<RefreshCw size={13} />}
          loading={loading}
          onClick={onRetry}
        >
          刷新
        </Button>
      </div>

      {period && summary && (
        <PortfolioMetricsStrip sections={metricSections} testId="combo-period-summary" />
      )}

      {period && summary && (
        <div className="shrink-0 flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1 border-b border-slate-100 text-[10px] text-slate-400">
          <span data-testid="combo-period-range">
            {periodRangeLabel(period)} · 评分日 {summary.scoring_days}/
            {summary.trading_days} · 收盘价建仓/退出，不计手续费与滑点
          </span>
          <Tag
            color={summary.data_quality === 'complete' ? 'green' : 'orange'}
            className="!m-0 !text-[10px]"
            data-testid="combo-period-quality"
          >
            {summary.data_quality === 'complete'
              ? '数据完整'
              : `数据不完整 · 覆盖 ${summary.scoring_coverage_pct.toFixed(0)}%`}
          </Tag>
          {template && (
            <Tooltip title="每日新进 × 持有天数来自已保存组合模板；每仓目标金额 = 初始资金 ÷ 持仓上限">
              <span className="cursor-help">
                模板：每日新进 {template.entry_per_day} · 持有 {template.hold_days} 日 · 目标{' '}
                {template.target_positions} 只
              </span>
            </Tooltip>
          )}
        </div>
      )}

      {period && period.warnings.length > 0 && (
        <div className="shrink-0 px-3 pt-2" data-testid="combo-period-warnings">
          <Alert
            type="warning"
            showIcon
            message={period.warnings.map(periodWarningLabel).join('；')}
          />
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-auto overscroll-contain p-3 space-y-3">
        {!period || !summary ? (
          <div className="h-full flex items-center justify-center">
            <Empty description="选好区间后点「计算」，按模板滚动评估组合综合排名收益" />
          </div>
        ) : (
          <>
            {geometry && (
              <div
                className="rounded-lg border border-slate-200 bg-white px-2 py-1"
                data-testid="combo-period-chart"
              >
                <div className="flex items-center justify-between px-1 text-[10px] text-slate-400">
                  <span className="flex items-center gap-2">
                    <span>区间权益（初始 {formatAmount(summary.initial_capital)}）</span>
                    <span className="flex items-center gap-1">
                      <span className="inline-block h-0.5 w-3 bg-blue-600" />
                      组合
                    </span>
                    {geometry.benchmarkPath && (
                      <span className="flex items-center gap-1">
                        <span className="inline-block h-0.5 w-3 bg-amber-500" />
                        {period.benchmark?.name ?? '基准'}
                      </span>
                    )}
                  </span>
                  <span className="font-mono">
                    {formatAmount(geometry.equity.minEquity)} ~{' '}
                    {formatAmount(geometry.equity.maxEquity)}
                  </span>
                </div>
                <svg
                  viewBox={`0 0 ${EQUITY_WIDTH} ${EQUITY_HEIGHT}`}
                  preserveAspectRatio="none"
                  className="w-full h-40"
                  role="img"
                  aria-label="区间权益曲线与基准"
                >
                  <line
                    x1={0}
                    x2={EQUITY_WIDTH}
                    y1={geometry.equity.baselineY}
                    y2={geometry.equity.baselineY}
                    stroke="#cbd5e1"
                    strokeDasharray="4 4"
                    strokeWidth={1}
                  />
                  <path d={geometry.equity.areaPath} fill="#dbeafe" opacity={0.6} />
                  {geometry.benchmarkPath && (
                    <path
                      d={geometry.benchmarkPath}
                      fill="none"
                      stroke="#f59e0b"
                      strokeWidth={1.3}
                      strokeDasharray="3 2"
                      vectorEffect="non-scaling-stroke"
                    />
                  )}
                  <path
                    d={geometry.equity.linePath}
                    fill="none"
                    stroke="#2563eb"
                    strokeWidth={1.6}
                    vectorEffect="non-scaling-stroke"
                  />
                </svg>
              </div>
            )}

            <div data-testid="combo-period-months">
              <div className="mb-1 text-[11px] font-bold text-slate-600">月度拆解</div>
              <Table<BlendPeriodMonth>
                size="small"
                rowKey="month"
                columns={monthColumns}
                dataSource={period.months}
                pagination={false}
                scroll={{ x: 1080 }}
              />
            </div>

            <Collapse
              size="small"
              data-testid="combo-period-details"
              items={[
                {
                  key: 'trades',
                  label: `已了结明细（${period.trades.length} 笔）`,
                  children: (
                    <Table<BlendPeriodTrade>
                      size="small"
                      rowKey={(row) => `${row.symbol}-${row.entry_date}-${row.exit_date}`}
                      columns={tradeColumns}
                      dataSource={period.trades}
                      pagination={{ pageSize: 20, hideOnSinglePage: true }}
                      scroll={{ x: 820 }}
                      data-testid="combo-period-trades"
                    />
                  ),
                },
                {
                  key: 'open',
                  label: `期末在场（${period.open.length} 只）`,
                  children: (
                    <Table<BlendPeriodOpen>
                      size="small"
                      rowKey={(row) => `${row.symbol}-${row.entry_date}`}
                      columns={openColumns}
                      dataSource={period.open}
                      pagination={{ pageSize: 20, hideOnSinglePage: true }}
                      scroll={{ x: 820 }}
                      data-testid="combo-period-open"
                    />
                  ),
                },
              ]}
            />
          </>
        )}
      </div>

      <div className="shrink-0 px-3 py-1.5 border-t border-slate-100 text-[10px] text-slate-400">
        口径：评分日收盘价建仓，固定持有 H 个交易日后收盘价退出（停牌顺延），每仓目标金额 =
        初始资金 ÷ 持仓上限；期末未到期按最后有效收盘价估值。基准为
        {period?.benchmark?.name ?? '本市场基准（未接入）'}，按日收益复利并与
        组合同轴展示；覆盖率不足 95% 时基准与超额一律显示 —。不计手续费、印花税与滑点。
        结果用于评估组合综合排名能力，不构成任何投资建议。
      </div>
    </div>
  );
};

export default PeriodReturnPanel;
